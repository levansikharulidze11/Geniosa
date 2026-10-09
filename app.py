
# ============================================================
# GENIOSA 5.6
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + Google Search Grounding
#
# Additive migrations only.
# No DROP, TRUNCATE or mass DELETE.
# ============================================================

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone

import requests
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI

APP_VERSION = "5.6"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-3.5-flash-lite"
).strip()
PORT = int(os.getenv("PORT", "8000"))

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN else ""
)
GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

POLL_TIMEOUT = 25
POLL_RETRY_DELAY = 5
MAX_HISTORY_MESSAGES = 20
MAX_WEB_RESULTS = 8
POLLING_LOCK_ID = 9173552026
TELEGRAM_MESSAGE_LIMIT = 3900

app = FastAPI(title="GENIOSA", version=APP_VERSION)

DB_POOL = None
DB_POOL_LOCK = threading.Lock()
polling_thread = None
polling_stop = threading.Event()

telegram_status = {
    "ok": False,
    "polling": False,
    "lock_acquired": False,
    "bot_username": None,
    "last_error": None,
    "last_update": None,
}
gemini_status = {
    "ok": False,
    "last_error": None,
    "last_success": None,
}
web_status = {
    "available": bool(GEMINI_API_KEY),
    "enabled": False,
    "last_error": None,
    "last_search": None,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(f"[GENIOSA {utc_now()}] {message}", flush=True)


def log_error(prefix, exc):
    log(f"{prefix}: {type(exc).__name__}: {exc}")


# ============================================================
# DATABASE
# ============================================================

def init_db_pool():
    global DB_POOL

    with DB_POOL_LOCK:
        if DB_POOL is not None:
            return

        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")

        DB_POOL = ThreadedConnectionPool(
            1, 8, DATABASE_URL,
            connect_timeout=10,
            application_name="GENIOSA"
        )
        log("PostgreSQL connection pool initialized")


class DBConnection:
    def __enter__(self):
        if DB_POOL is None:
            init_db_pool()

        self.conn = DB_POOL.getconn()
        self.conn.autocommit = False
        return self.conn

    def __exit__(self, exc_type, exc_value, exc_tb):
        if self.conn is None:
            return

        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        except Exception as exc:
            log_error("database transaction", exc)
            try:
                self.conn.rollback()
            except Exception:
                pass

        try:
            if DB_POOL is not None:
                DB_POOL.putconn(self.conn)
            else:
                self.conn.close()
        except Exception as exc:
            log_error("return database connection", exc)


def db_execute(sql, params=None, fetch=False, fetchone=False):
    with DBConnection() as conn:
        cur = conn.cursor(
            cursor_factory=RealDictCursor if (fetch or fetchone) else None
        )
        try:
            cur.execute(sql, params or ())
            if fetchone:
                return cur.fetchone()
            if fetch:
                return cur.fetchall()
            return True
        finally:
            cur.close()


def table_exists(table):
    row = db_execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
        fetchone=True
    )
    return bool(row)


def table_columns(table):
    if not table_exists(table):
        return []

    return db_execute(
        """
        SELECT column_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
        fetch=True
    ) or []


def column_names(table):
    return {row["column_name"] for row in table_columns(table)}


def add_column_if_missing(table, column, definition):
    if column not in column_names(table):
        db_execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )
        log(f"Added column {table}.{column}")


def ensure_schema():
    # messages: support common legacy field names
    db_execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            role TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for name, definition in [
        ("chat_id", "BIGINT"),
        ("role", "TEXT"),
        ("message", "TEXT"),
        ("text", "TEXT"),
        ("content", "TEXT"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]:
        add_column_if_missing("messages", name, definition)

    db_execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            memory_type TEXT,
            importance INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for name, definition in [
        ("chat_id", "BIGINT"),
        ("content", "TEXT"),
        ("text", "TEXT"),
        ("memory", "TEXT"),
        ("memory_type", "TEXT"),
        ("importance", "INTEGER DEFAULT 5"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]:
        add_column_if_missing("memories", name, definition)

    db_execute("""
        CREATE TABLE IF NOT EXISTS facts (
            fact_key TEXT PRIMARY KEY,
            fact_value TEXT
        )
    """)

    for name in ("fact_key", "fact_value", "content", "value", "text", "body"):
        add_column_if_missing("facts", name, "TEXT")

    db_execute("""
        CREATE TABLE IF NOT EXISTS fact_history (
            id BIGSERIAL PRIMARY KEY,
            fact_key TEXT,
            old_value TEXT,
            new_value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS memory_events (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
            name TEXT,
            description TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS research (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            query TEXT,
            answer TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for name, definition in [
        ("chat_id", "BIGINT"),
        ("query", "TEXT"),
        ("answer", "TEXT"),
        ("sources", "TEXT"),
        ("created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]:
        add_column_if_missing("research", name, definition)

    db_execute("""
        CREATE TABLE IF NOT EXISTS financial_models (
            id BIGSERIAL PRIMARY KEY,
            project_name TEXT,
            data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS bot_projects (
            id BIGSERIAL PRIMARY KEY,
            project_name TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS bot_files (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            filename TEXT,
            filepath TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # A new table avoids relying on a possibly incompatible legacy schema.
    db_execute("""
        CREATE TABLE IF NOT EXISTS geniosa_seed_meta_v56 (
            seed_key TEXT NOT NULL,
            seed_version TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (seed_key, seed_version)
        )
    """)

    log("Database schema checked; additive migrations complete")


def dynamic_insert(table, values, defaults=None):
    defaults = defaults or {}
    metadata = table_columns(table)

    if not metadata:
        raise RuntimeError(f"Table {table} is unavailable")

    existing = {row["column_name"] for row in metadata}
    usable = {k: v for k, v in values.items() if k in existing}

    for row in metadata:
        name = row["column_name"]
        if (
            row["is_nullable"] == "NO"
            and row["column_default"] is None
            and name not in usable
        ):
            if name not in defaults:
                raise RuntimeError(
                    f"Required legacy column {table}.{name} "
                    "has no value or default"
                )
            usable[name] = defaults[name]

    if not usable:
        raise RuntimeError(f"No compatible columns for {table}")

    cols = list(usable.keys())
    quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))

    db_execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        tuple(usable[c] for c in cols)
    )


# ============================================================
# FACTS AND MEMORY
# ============================================================

def fact_value_from_row(row):
    for name in ("fact_value", "content", "value", "text", "body"):
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def create_fact(key, value):
    value = str(value)

    try:
        row = db_execute(
            "SELECT * FROM facts WHERE fact_key = %s LIMIT 1",
            (key,),
            fetchone=True
        )

        if row:
            old = fact_value_from_row(row)

            if old != value:
                try:
                    dynamic_insert(
                        "fact_history",
                        {
                            "fact_key": key,
                            "old_value": old,
                            "new_value": value
                        }
                    )
                except Exception as exc:
                    log_error("fact history", exc)

                cols = column_names("facts")
                updates = []
                params = []

                for name in ("fact_value", "content", "value", "text", "body"):
                    if name in cols:
                        updates.append(f'"{name}" = %s')
                        params.append(value)

                if updates:
                    params.append(key)
                    db_execute(
                        f'UPDATE facts SET {", ".join(updates)} '
                        "WHERE fact_key = %s",
                        tuple(params)
                    )
            return

        dynamic_insert(
            "facts",
            {
                "fact_key": key,
                "fact_value": value,
                "content": value,
                "value": value,
                "text": value,
                "body": value
            },
            defaults={
                "fact_value": value,
                "content": value,
                "value": value,
                "text": value,
                "body": value
            }
        )

    except Exception as exc:
        log_error(f"create_fact({key})", exc)


def get_facts(limit=150):
    try:
        rows = db_execute(
            "SELECT * FROM facts LIMIT %s",
            (limit,),
            fetch=True
        ) or []

        result = []
        for row in rows:
            key = row.get("fact_key")
            value = fact_value_from_row(row)

            if key and value:
                result.append({"key": str(key), "value": value})

        return result

    except Exception as exc:
        log_error("get_facts", exc)
        return []


def save_memory(chat_id, content, memory_type="general", importance=5):
    try:
        dynamic_insert(
            "memories",
            {
                "chat_id": chat_id,
                "content": content,
                "text": content,
                "memory": content,
                "memory_type": memory_type,
                "importance": importance
            },
            defaults={
                "content": content,
                "text": content,
                "memory": content
            }
        )
    except Exception as exc:
        log_error("save_memory", exc)


def get_memories(chat_id, limit=20):
    try:
        rows = db_execute(
            """
            SELECT * FROM memories
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True
        ) or []

        result = []
        for row in reversed(rows):
            content = row.get("content") or row.get("text") or row.get("memory")
            if content:
                result.append(str(content))
        return result

    except Exception as exc:
        log_error("get_memories", exc)
        return []


def save_message(chat_id, role, message):
    message = "" if message is None else str(message)

    try:
        dynamic_insert(
            "messages",
            {
                "chat_id": chat_id,
                "role": role,
                "message": message,
                "text": message,
                "content": message
            },
            defaults={
                "message": message,
                "text": message,
                "content": message
            }
        )
    except Exception as exc:
        log_error("save_message", exc)


def get_history(chat_id, limit=MAX_HISTORY_MESSAGES):
    try:
        cols = column_names("messages")
        order_by = "created_at" if "created_at" in cols else (
            "id" if "id" in cols else None
        )

        sql = "SELECT * FROM messages WHERE chat_id = %s"
        if order_by:
            sql += f' ORDER BY "{order_by}" DESC'
        sql += " LIMIT %s"

        rows = db_execute(
            sql,
            (chat_id, limit),
            fetch=True
        ) or []

        history = []

        for row in reversed(rows):
            role = (row.get("role") or "user").lower()
            content = row.get("message") or row.get("text") or row.get("content")

            if not content:
                continue

            history.append({
                "role": "model" if role in ("assistant", "model", "bot") else "user",
                "content": str(content)
            })

        return history

    except Exception as exc:
        log_error("get_history", exc)
        return []


def save_research(chat_id, query, answer, sources):
    try:
        dynamic_insert(
            "research",
            {
                "chat_id": chat_id,
                "query": query,
                "answer": answer,
                "sources": json.dumps(sources, ensure_ascii=False)
            }
        )
    except Exception as exc:
        log_error("save_research", exc)


# ============================================================
# INITIAL FACTS
# ============================================================

def seed_once():
    seed_key = "geniosa"
    seed_version = "5.6-v1"

    row = db_execute(
        """
        SELECT 1 FROM geniosa_seed_meta_v56
        WHERE seed_key = %s AND seed_version = %s
        LIMIT 1
        """,
        (seed_key, seed_version),
        fetchone=True
    )

    if row:
        log("Seed already applied")
        return

    initial_facts = {
        "company.name": "SAMTISI CONSTRUCTION LLC",
        "company.id": "406391202",
        "company.type": "Construction & Development Company",
        "company.founded": "December 5, 2022",
        "company.headquarters": "Tbilisi, Georgia",
        "project.nikkea12.location": "Nikkea 12, Kutaisi, Georgia",
        "project.nikkea12.land_area": "3,070 m²",
        "project.nikkea12.saleable_area": "10,854 m²",
        "project.nikkea12.hotel_rooms_area": "7,212 m²",
        "project.samgori.location": "Giorgi Naderishvili Street, Samgori, Tbilisi",
        "project.samgori.land_area": "7,390 m²",
        "project.samgori.build_area": "46,131 m²",
        "project.samgori.saleable_area": "30,540 m²",
        "project.goldenlake.land": "Approximately 46 hectares",
        "geniosa.rule": "Never fabricate facts, numbers, sources or URLs."
    }

    for key, value in initial_facts.items():
        create_fact(key, value)

    db_execute(
        """
        INSERT INTO geniosa_seed_meta_v56 (seed_key, seed_version)
        VALUES (%s, %s)
        ON CONFLICT (seed_key, seed_version) DO NOTHING
        """,
        (seed_key, seed_version)
    )

    log("Initial facts seeded")


# ============================================================
# GEMINI
# ============================================================

GENIOSA_CONSTITUTION = """
You are GENIOSA, the user's personal business advisor and economic assistant.

NON-NEGOTIABLE RULES:
1. Never invent facts, numbers, URLs, citations, or sources.
2. Distinguish CONFIRMED INTERNAL FACTS, WEB RESEARCH, ASSUMPTIONS,
   ESTIMATES, and UNKNOWN information.
3. Clearly say when information is unknown.
4. Explain assumptions and formulas for financial calculations.
5. Never silently change project data.
6. Web information is not automatically a confirmed internal fact.
7. Never claim to have searched the web unless Google Search grounding
   was actually requested and returned.
8. Prefer primary and authoritative sources.
9. Mention material risks in investment analysis.
10. Reply in the user's language, normally Georgian.
11. Do not automatically save web research as a confirmed fact.
12. Be practical, direct, and business-oriented.
"""


def get_project_context():
    return "\n".join(
        f"- {item['key']}: {item['value']}"
        for item in get_facts()
    )


def is_web_request(text):
    value = (text or "").strip().lower()

    if value.startswith(("/web", "/search", "web:", "search:")):
        return True

    keywords = [
        "ინტერნეტში მოძებნე",
        "ინტერნეტში მოიძიე",
        "ინტერნეტში ნახე",
        "ვებში მოძებნე",
        "google-ში მოძებნე",
        "ონლაინ მოძებნე",
        "უახლესი ინფორმაცია",
        "მიმდინარე ინფორმაცია",
        "დღევანდელი ფასი",
        "ახლანდელი ფასი",
        "search the web",
        "search online",
        "web search",
        "latest information",
        "current price"
    ]

    return any(keyword in value for keyword in keywords)


def clean_web_command(text):
    value = (text or "").strip()

    for prefix in ("/web", "/search", "web:", "search:"):
        if value.lower().startswith(prefix):
            return value[len(prefix):].strip()

    return value


def extract_gemini_text(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return ""

    parts = candidates[0].get("content", {}).get("parts", [])
    return "\n".join(
        part.get("text", "")
        for part in parts
        if part.get("text")
    ).strip()


def extract_grounding_sources(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return []

    metadata = candidates[0].get("groundingMetadata") or {}
    chunks = metadata.get("groundingChunks") or []

    result = []
    seen = set()

    for chunk in chunks:
        web = chunk.get("web") or {}
        url = web.get("uri")
        title = web.get("title") or url

        if url and url not in seen:
            seen.add(url)
            result.append({"title": title, "url": url})

    return result[:MAX_WEB_RESULTS]


def ask_gemini(chat_id, user_text, use_web=False):
    if not GEMINI_API_KEY:
        return "შეცდომა: GEMINI_API_KEY არ არის დაყენებული Render-ის Environment-ში."

    history = get_history(chat_id, MAX_HISTORY_MESSAGES)

    # The current user message has already been saved.
    contents = []
    for item in history:
        contents.append({
            "role": item["role"],
            "parts": [{"text": item["content"]}]
        })

    if not contents or contents[-1]["role"] != "user":
        contents.append({
            "role": "user",
            "parts": [{"text": user_text}]
        })
    elif contents[-1]["parts"][0]["text"] != user_text:
        contents.append({
            "role": "user",
            "parts": [{"text": user_text}]
        })

    system_text = (
        GENIOSA_CONSTITUTION
        + "\n\nCONFIRMED INTERNAL COMPANY / PROJECT FACTS:\n"
        + get_project_context()
    )

    payload = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8192
        }
    }

    if use_web:
        payload["tools"] = [{"google_search": {}}]

    try:
        response = requests.post(
            GEMINI_API,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=120
        )

        if response.status_code != 200:
            message = (
                f"Gemini HTTP {response.status_code}: "
                f"{response.text[:700]}"
            )
            gemini_status["ok"] = False
            gemini_status["last_error"] = message

            if use_web:
                web_status["last_error"] = message

            log(message)

            return (
                "Gemini API-მ შეცდომა დააბრუნა.\n"
                f"HTTP კოდი: {response.status_code}\n"
                "დეტალები გადაამოწმე Render-ის Logs-ში."
            )

        data = response.json()
        answer = extract_gemini_text(data)

        if not answer:
            gemini_status["ok"] = False
            gemini_status["last_error"] = "Gemini returned no text"
            return "Gemini-მ ცარიელი პასუხი დააბრუნა. სცადე ხელახლა."

        gemini_status["ok"] = True
        gemini_status["last_error"] = None
        gemini_status["last_success"] = utc_now()

        if use_web:
            sources = extract_grounding_sources(data)
            web_status["enabled"] = True
            web_status["last_error"] = None
            web_status["last_search"] = utc_now()

            if sources:
                answer += "\n\n🌐 WEB SOURCES\n"
                for index, source in enumerate(sources, start=1):
                    answer += (
                        f"\n{index}. {source['title']}\n"
                        f"{source['url']}"
                    )

                answer += (
                    "\n\nშენიშვნა: ვებ-წყაროები კვლევის მასალაა და "
                    "ავტომატურად არ ითვლება დადასტურებულ შიდა ფაქტად."
                )
                save_research(chat_id, user_text, answer, sources)
            else:
                answer += (
                    "\n\nშენიშვნა: პასუხთან ერთად წყაროები არ დაბრუნებულა. "
                    "მნიშვნელოვანი ინფორმაცია დამოუკიდებლად გადაამოწმე."
                )

        return answer

    except requests.RequestException as exc:
        gemini_status["ok"] = False
        gemini_status["last_error"] = str(exc)
        if use_web:
            web_status["last_error"] = str(exc)
        log_error("Gemini request", exc)
        return "Gemini-სთან დაკავშირებისას შეცდომა მოხდა. სცადე მოგვიანებით."

    except Exception as exc:
        gemini_status["ok"] = False
        gemini_status["last_error"] = str(exc)
        log_error("ask_gemini", exc)
        log(traceback.format_exc())
        return "პასუხის დამუშავებისას ტექნიკური შეცდომა მოხდა."


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, payload=None, timeout=60):
    if not TELEGRAM_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    return requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=timeout
    )


def telegram_get_me():
    response = telegram_request("getMe", timeout=20)

    if response.status_code != 200:
        log(f"getMe HTTP {response.status_code}: {response.text[:500]}")
        return None

    data = response.json()
    if not data.get("ok"):
        log(f"getMe API error: {data}")
        return None

    return data.get("result") or {}


def telegram_delete_webhook():
    response = telegram_request(
        "deleteWebhook",
        {"drop_pending_updates": False},
        timeout=20
    )

    if response.status_code != 200:
        log(f"deleteWebhook error: {response.text[:500]}")


def send_message(chat_id, text):
    if not text:
        return False

    chunks = [
        text[index:index + TELEGRAM_MESSAGE_LIMIT]
        for index in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ]

    try:
        for chunk in chunks:
            response = telegram_request(
                "sendMessage",
                {"chat_id": chat_id, "text": chunk},
                timeout=30
            )

            if response.status_code != 200:
                log(f"sendMessage failed: {response.text[:700]}")
                return False

            data = response.json()
            if not data.get("ok"):
                log(f"sendMessage API error: {data}")
                return False

        return True

    except Exception as exc:
        log_error("send_message", exc)
        return False


def is_allowed_chat(chat_id):
    if not OWNER_ID:
        return True
    return str(chat_id) == str(OWNER_ID)


def handle_command(chat_id, text):
    command = text.split()[0].lower().split("@")[0]

    if command == "/start":
        return (
            f"გამარჯობა! მე ვარ GENIOSA {APP_VERSION} — "
            "შენი პირადი ბიზნეს მრჩეველი.\n\n"
            "ბრძანებები:\n"
            "/help — დახმარება\n"
            "/facts — შენახული ფაქტები\n"
            "/memory — შენახული მეხსიერება\n"
            "/summary — მოკლე შეჯამება\n"
            "/web კითხვა — ინტერნეტში ძიება\n"
            "/health — სისტემის სტატუსი"
        )

    if command == "/help":
        return (
            "GENIOSA ბრძანებები:\n"
            "/start — დაწყება\n"
            "/help — დახმარება\n"
            "/facts — დადასტურებული შიდა ფაქტები\n"
            "/memory — შენახული მეხსიერება\n"
            "/summary — შეჯამება\n"
            "/web კითხვა — Google Search-ით კვლევა\n"
            "/health — სისტემის სტატუსი\n\n"
            "ასევე შეგიძლია დაწერო: web: შენი კითხვა"
        )

    if command == "/facts":
        facts = get_facts()
        if not facts:
            return "შენახული ფაქტები ვერ მოიძებნა."

        lines = ["📌 დადასტურებული შიდა ფაქტები:"]
        for item in facts[:70]:
            lines.append(f"• {item['key']}: {item['value']}")
        return "\n".join(lines)

    if command == "/memory":
        memories = get_memories(chat_id, 30)
        if not memories:
            return "ამ ჩატისთვის შენახული მეხსიერება არ მოიძებნა."
        return "🧠 შენახული მეხსიერება:\n\n" + "\n".join(
            f"• {item}" for item in memories
        )

    if command == "/summary":
        facts = get_facts()
        project_count = sum(
            1 for item in facts
            if item["key"].startswith("project.")
        )
        return (
            "📊 GENIOSA შეჯამება\n\n"
            f"შენახული ფაქტები: {len(facts)}\n"
            f"პროექტის ფაქტები: {project_count}\n"
            f"Gemini API Key: {'კონფიგურირებულია' if GEMINI_API_KEY else 'არ არის'}\n"
            f"Telegram Token: {'კონფიგურირებულია' if TELEGRAM_BOT_TOKEN else 'არ არის'}"
        )

    if command == "/health":
        return (
            f"GENIOSA {APP_VERSION}\n"
            f"Telegram: {'OK' if telegram_status['ok'] else 'შესამოწმებელია'}\n"
            f"Polling: {'აქტიურია' if telegram_status['polling'] else 'არააქტიურია'}\n"
            f"Gemini: {'OK' if gemini_status['ok'] else 'ჯერ არ დადასტურებულა/შეცდომაა'}\n"
            f"Web Search: {'გამოყენებულია' if web_status['enabled'] else 'ჯერ არ გამოყენებულა'}\n"
            f"ბოლო შეცდომა: {telegram_status.get('last_error') or gemini_status.get('last_error') or 'არ არის'}"
        )

    return None


def process_update(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")

    if chat_id is None or not isinstance(text, str):
        return

    if not is_allowed_chat(chat_id):
        send_message(chat_id, "ეს GENIOSA-ს პირადი ბიზნეს ასისტენტია.")
        return

    text = text.strip()
    if not text:
        return

    telegram_status["last_message_processed"] = utc_now()
    save_message(chat_id, "user", text)

    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

    if command in ("/web", "/search"):
        query = clean_web_command(text)
        if not query:
            answer = "გამოყენება: /web ჩაწერე კითხვა, რომლის მოძიებაც გინდა."
        else:
            answer = ask_gemini(chat_id, query, use_web=True)

        save_message(chat_id, "assistant", answer)
        send_message(chat_id, answer)
        return

    if command:
        answer = handle_command(chat_id, text)
        if answer is not None:
            save_message(chat_id, "assistant", answer)
            send_message(chat_id, answer)
            return

    use_web = is_web_request(text)
    query = clean_web_command(text) if use_web else text
    answer = ask_gemini(chat_id, query, use_web=use_web)

    save_message(chat_id, "assistant", answer)
    send_message(chat_id, answer)


# ============================================================
# SINGLE POLLING INSTANCE LOCK
# ============================================================

def acquire_polling_lock():
    if DB_POOL is None:
        init_db_pool()

    conn = DB_POOL.getconn()

    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (POLLING_LOCK_ID,)
        )
        acquired = bool(cur.fetchone()[0])
        cur.close()

        if not acquired:
            DB_POOL.putconn(conn)
            log("Polling lock not acquired; another instance may be active")
            return None

        log("PostgreSQL polling lock acquired")
        return conn

    except Exception:
        try:
            DB_POOL.putconn(conn)
        except Exception:
            pass
        raise


def release_polling_lock(conn):
    if conn is None:
        return

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_unlock(%s)",
            (POLLING_LOCK_ID,)
        )
        cur.close()
    except Exception as exc:
        log_error("release polling lock", exc)
    finally:
        try:
            if DB_POOL is not None:
                DB_POOL.putconn(conn)
            else:
                conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def polling_loop():
    lock_conn = None
    telegram_status["polling"] = False
    telegram_status["lock_acquired"] = False

    try:
        if not TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        lock_conn = acquire_polling_lock()
        if lock_conn is None:
            telegram_status["last_error"] = "Another instance owns the polling lock"
            return

        telegram_status["lock_acquired"] = True

        me = telegram_get_me()
        if not me:
            telegram_status["last_error"] = "Telegram getMe failed"
            return

        telegram_status["bot_username"] = me.get("username")
        telegram_delete_webhook()

        telegram_status["ok"] = True
        telegram_status["polling"] = True
        telegram_status["last_error"] = None

        log(f"Telegram polling started for @{me.get('username', 'unknown')}")

        offset = 0

        while not polling_stop.is_set():
            try:
                response = telegram_request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": ["message", "edited_message"]
                    },
                    timeout=POLL_TIMEOUT + 10
                )

                if response.status_code != 200:
                    telegram_status["ok"] = False
                    telegram_status["last_error"] = (
                        f"HTTP {response.status_code}: {response.text[:500]}"
                    )
                    log(telegram_status["last_error"])
                    time.sleep(POLL_RETRY_DELAY)
                    continue

                data = response.json()

                if not data.get("ok"):
                    telegram_status["ok"] = False
                    telegram_status["last_error"] = str(data)[:500]
                    log(f"getUpdates API error: {data}")
                    time.sleep(POLL_RETRY_DELAY)
                    continue

                telegram_status["ok"] = True
                telegram_status["last_error"] = None

                for update in data.get("result", []):
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = max(offset, update_id + 1)

                    telegram_status["last_update"] = utc_now()

                    try:
                        process_update(update)
                    except Exception as exc:
                        log_error("process_update", exc)
                        log(traceback.format_exc())

            except requests.RequestException as exc:
                telegram_status["ok"] = False
                telegram_status["last_error"] = str(exc)
                log_error("Telegram polling request", exc)
                time.sleep(POLL_RETRY_DELAY)

            except Exception as exc:
                telegram_status["ok"] = False
                telegram_status["last_error"] = str(exc)
                log_error("Telegram polling loop", exc)
                log(traceback.format_exc())
                time.sleep(POLL_RETRY_DELAY)

    except Exception as exc:
        telegram_status["ok"] = False
        telegram_status["last_error"] = str(exc)
        log_error("polling startup", exc)
        log(traceback.format_exc())

    finally:
        telegram_status["polling"] = False
        telegram_status["lock_acquired"] = False
        release_polling_lock(lock_conn)
        log("Telegram polling loop ended")


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
def root():
    return {
        "app": "GENIOSA",
        "version": APP_VERSION,
        "status": "online"
    }


@app.get("/health")
def health():
    database_ok = False
    database_error = None

    try:
        db_execute("SELECT 1")
        database_ok = True
    except Exception as exc:
        database_error = str(exc)

    return {
        "app": "GENIOSA",
        "version": APP_VERSION,
        "status": "online",
        "database": {
            "ok": database_ok,
            "error": database_error
        },
        "telegram": telegram_status,
        "gemini": gemini_status,
        "web_search": web_status,
        "model": GEMINI_MODEL
    }


@app.on_event("startup")
def startup():
    global polling_thread

    log(f"Starting GENIOSA {APP_VERSION}")

    init_db_pool()
    ensure_schema()
    seed_once()

    polling_stop.clear()
    polling_thread = threading.Thread(
        target=polling_loop,
        daemon=True,
        name="geniosa-telegram-polling"
    )
    polling_thread.start()

    log("Startup completed")


@app.on_event("shutdown")
def shutdown():
    global DB_POOL

    log("Shutdown requested")
    polling_stop.set()

    if polling_thread is not None:
        polling_thread.join(timeout=POLL_TIMEOUT + 15)

    # Do not close the pool if the polling thread is still using it.
    if polling_thread is not None and polling_thread.is_alive():
        log("Polling thread still active; database pool kept open")
        return

    if DB_POOL is not None:
        try:
            DB_POOL.closeall()
        except Exception as exc:
            log_error("close database pool", exc)
        DB_POOL = None

    log("Shutdown completed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT)
