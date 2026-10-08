# ============================================================
# GENIOSA 5.5
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + Google Search Grounding
#
# IMPORTANT:
#   Existing PostgreSQL data is preserved.
#   No DROP.
#   No TRUNCATE.
#   No mass DELETE.
#   Migrations are additive.
#
# NEW IN 5.5:
#   - Google Search Grounding
#   - Web research detection
#   - Web source extraction
#   - Research history
#   - Web information is NOT automatically a confirmed fact
#   - /web, web:, search: explicit web commands
#   - /health reports web search status
# ============================================================

import os
import re
import json
import time
import threading
import traceback
from datetime import datetime

import requests
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI


# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "5.5"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN
    else ""
)

GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
    f"?key={GEMINI_API_KEY}"
    if GEMINI_API_KEY
    else ""
)

PORT = int(os.getenv("PORT", "8000"))

POLL_TIMEOUT = 30
POLL_RETRY_DELAY = 5

MAX_HISTORY_MESSAGES = 20
MAX_WEB_RESULTS = 8

app = FastAPI(
    title="GENIOSA",
    version=APP_VERSION
)


# ============================================================
# GLOBAL STATE
# ============================================================

DB_POOL = None

polling_thread = None
polling_stop = threading.Event()

telegram_status = {
    "ok": False,
    "last_error": None,
    "last_update": None,
}

gemini_status = {
    "ok": False,
    "last_error": None,
}

web_status = {
    "enabled": False,
    "last_error": None,
    "last_search": None,
}


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[GENIOSA {datetime.utcnow().isoformat()}] {message}",
        flush=True
    )


def log_error(prefix, exc):
    log(
        f"{prefix}: {type(exc).__name__}: {exc}"
    )


# ============================================================
# DATABASE
# ============================================================

def init_db_pool():
    global DB_POOL

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    if DB_POOL is not None:
        return

    DB_POOL = ThreadedConnectionPool(
        1,
        5,
        DATABASE_URL,
        connect_timeout=10
    )

    log("PostgreSQL connection pool initialized")


class DBConnection:
    def __init__(self):
        self.conn = None

    def __enter__(self):
        if DB_POOL is None:
            init_db_pool()

        self.conn = DB_POOL.getconn()

        try:
            self.conn.autocommit = False
        except Exception:
            pass

        return self.conn

    def __exit__(self, exc_type, exc_value, exc_tb):
        if self.conn is None:
            return

        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass

        try:
            DB_POOL.putconn(self.conn)
        except Exception:
            pass

        self.conn = None


def db_connection():
    return DBConnection()


def db_execute(
    sql,
    params=None,
    fetch=False,
    fetchone=False,
    commit=True
):
    with db_connection() as conn:
        cursor_factory = RealDictCursor if (fetch or fetchone) else None

        cur = conn.cursor(
            cursor_factory=cursor_factory
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


# ============================================================
# SAFE TABLE / COLUMN HELPERS
# ============================================================

def table_columns(table):
    rows = db_execute(
        """
        SELECT column_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
        fetch=True
    )

    return rows or []


def column_names(table):
    return {
        row["column_name"]
        for row in table_columns(table)
    }


def add_column_if_missing(
    table,
    column,
    definition
):
    cols = column_names(table)

    if column not in cols:
        db_execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )

        log(
            f"Added column {table}.{column}"
        )


# ============================================================
# MIGRATIONS
# ============================================================

def run_migrations():
    log("Running additive migrations")

    # --------------------------------------------------------
    # messages
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            role TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column_if_missing(
        "messages",
        "chat_id",
        "BIGINT"
    )

    add_column_if_missing(
        "messages",
        "role",
        "TEXT"
    )

    add_column_if_missing(
        "messages",
        "message",
        "TEXT"
    )

    add_column_if_missing(
        "messages",
        "text",
        "TEXT"
    )

    add_column_if_missing(
        "messages",
        "content",
        "TEXT"
    )

    add_column_if_missing(
        "messages",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # memories
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            memory_type TEXT,
            importance INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column_if_missing(
        "memories",
        "chat_id",
        "BIGINT"
    )

    add_column_if_missing(
        "memories",
        "content",
        "TEXT"
    )

    add_column_if_missing(
        "memories",
        "text",
        "TEXT"
    )

    add_column_if_missing(
        "memories",
        "memory",
        "TEXT"
    )

    add_column_if_missing(
        "memories",
        "memory_type",
        "TEXT"
    )

    add_column_if_missing(
        "memories",
        "importance",
        "INTEGER DEFAULT 5"
    )

    add_column_if_missing(
        "memories",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # memory_events
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # facts
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            fact_key TEXT PRIMARY KEY,
            fact_value TEXT
        )
        """
    )

    add_column_if_missing(
        "facts",
        "fact_key",
        "TEXT"
    )

    add_column_if_missing(
        "facts",
        "fact_value",
        "TEXT"
    )

    add_column_if_missing(
        "facts",
        "content",
        "TEXT"
    )

    add_column_if_missing(
        "facts",
        "value",
        "TEXT"
    )

    add_column_if_missing(
        "facts",
        "text",
        "TEXT"
    )

    add_column_if_missing(
        "facts",
        "body",
        "TEXT"
    )

    # --------------------------------------------------------
    # fact_history
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id BIGSERIAL PRIMARY KEY,
            fact_key TEXT,
            old_value TEXT,
            new_value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # decisions
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # tasks
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # projects
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
            name TEXT,
            description TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # research
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS research (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            query TEXT,
            answer TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column_if_missing(
        "research",
        "chat_id",
        "BIGINT"
    )

    add_column_if_missing(
        "research",
        "query",
        "TEXT"
    )

    add_column_if_missing(
        "research",
        "answer",
        "TEXT"
    )

    add_column_if_missing(
        "research",
        "sources",
        "TEXT"
    )

    add_column_if_missing(
        "research",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # financial_models
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id BIGSERIAL PRIMARY KEY,
            project_name TEXT,
            data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # bot_projects
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            id BIGSERIAL PRIMARY KEY,
            project_name TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # bot_files
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_files (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            filename TEXT,
            filepath TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # system_seed_meta
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS system_seed_meta (
            seed_key TEXT,
            seed_version TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    log("Migrations completed")


# ============================================================
# DYNAMIC INSERT
# ============================================================

def dynamic_insert(
    table,
    values,
    required_defaults=None
):
    required_defaults = required_defaults or {}

    cols = column_names(table)

    usable = {
        key: value
        for key, value in values.items()
        if key in cols
    }

    # Fill required NOT NULL columns where possible
    metadata = table_columns(table)

    for row in metadata:
        col = row["column_name"]

        if (
            row["is_nullable"] == "NO"
            and row["column_default"] is None
            and col not in usable
        ):
            if col in required_defaults:
                usable[col] = required_defaults[col]

    if not usable:
        raise RuntimeError(
            f"No usable columns found for table {table}"
        )

    columns = list(usable.keys())

    quoted_columns = ", ".join(
        f'"{c}"'
        for c in columns
    )

    placeholders = ", ".join(
        ["%s"] * len(columns)
    )

    sql = (
        f'INSERT INTO "{table}" '
        f'({quoted_columns}) '
        f'VALUES ({placeholders})'
    )

    return db_execute(
        sql,
        tuple(usable[c] for c in columns)
    )


# ============================================================
# FACTS
# ============================================================

def create_fact(
    fact_key,
    fact_value
):
    value = str(fact_value)

    try:
        existing = db_execute(
            """
            SELECT *
            FROM facts
            WHERE fact_key = %s
            LIMIT 1
            """,
            (fact_key,),
            fetchone=True
        )

        if existing:
            old_value = (
                existing.get("fact_value")
                or existing.get("content")
                or existing.get("value")
                or existing.get("text")
                or existing.get("body")
            )

            if str(old_value) != value:
                try:
                    db_execute(
                        """
                        INSERT INTO fact_history
                        (fact_key, old_value, new_value)
                        VALUES (%s, %s, %s)
                        """,
                        (
                            fact_key,
                            old_value,
                            value
                        )
                    )
                except Exception as exc:
                    log_error(
                        "fact_history insert",
                        exc
                    )

                # Update all existing aliases safely
                cols = column_names("facts")

                updates = []

                params = []

                for alias in (
                    "fact_value",
                    "content",
                    "value",
                    "text",
                    "body"
                ):
                    if alias in cols:
                        updates.append(
                            f'"{alias}" = %s'
                        )
                        params.append(value)

                if updates:
                    params.append(fact_key)

                    db_execute(
                        f"""
                        UPDATE facts
                        SET {", ".join(updates)}
                        WHERE fact_key = %s
                        """,
                        tuple(params)
                    )

            return

        values = {
            "fact_key": fact_key,
            "fact_value": value,
            "content": value,
            "value": value,
            "text": value,
            "body": value
        }

        dynamic_insert(
            "facts",
            values,
            required_defaults={
                "fact_value": value,
                "content": value,
                "value": value,
                "text": value,
                "body": value
            }
        )

    except Exception as exc:
        log_error(
            f"create_fact({fact_key})",
            exc
        )


def get_facts(limit=100):
    try:
        rows = db_execute(
            """
            SELECT *
            FROM facts
            LIMIT %s
            """,
            (limit,),
            fetch=True
        )

        result = []

        for row in rows or []:
            key = row.get("fact_key")

            value = (
                row.get("fact_value")
                or row.get("content")
                or row.get("value")
                or row.get("text")
                or row.get("body")
            )

            if key and value:
                result.append(
                    {
                        "key": key,
                        "value": value
                    }
                )

        return result

    except Exception as exc:
        log_error("get_facts", exc)
        return []


# ============================================================
# SEED
# ============================================================

def seed_once():
    seed_version = "5.5-v1"

    try:
        existing = db_execute(
            """
            SELECT 1
            FROM system_seed_meta
            WHERE seed_key = %s
              AND seed_version = %s
            LIMIT 1
            """,
            (
                "geniosa",
                seed_version
            ),
            fetchone=True
        )

        if existing:
            log("System seed already applied")
            return

        facts = {
            "company.name":
                "SAMTISI CONSTRUCTION LLC",

            "company.id":
                "406391202",

            "company.type":
                "Construction & Development Company",

            "company.founded":
                "December 5, 2022",

            "company.headquarters":
                "Tbilisi, Georgia",

            "project.nikkea12.location":
                "Nikkea 12, Kutaisi, Georgia",

            "project.nikkea12.land_area":
                "3,070 m²",

            "project.nikkea12.saleable_area":
                "10,854 m²",

            "project.nikkea12.hotel_rooms_area":
                "7,212 m²",

            "project.samgori.location":
                "Giorgi Naderishvili Street, Samgori, Tbilisi",

            "project.samgori.land_area":
                "7,390 m²",

            "project.samgori.build_area":
                "46,131 m²",

            "project.samgori.saleable_area":
                "30,540 m²",

            "project.goldenlake.land":
                "Approximately 46 hectares",

            "geniosa.rule":
                "Never fabricate facts, figures, sources or URLs."
        }

        for key, value in facts.items():
            create_fact(key, value)

        db_execute(
            """
            INSERT INTO system_seed_meta
            (seed_key, seed_version)
            VALUES (%s, %s)
            """,
            (
                "geniosa",
                seed_version
            )
        )

        log("System seed applied")

    except Exception as exc:
        log_error("seed_once", exc)


# ============================================================
# MEMORY
# ============================================================

def save_memory(
    chat_id,
    content,
    memory_type="general",
    importance=5
):
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
            required_defaults={
                "content": content,
                "text": content,
                "memory": content
            }
        )

    except Exception as exc:
        log_error("save_memory", exc)


def get_memories(
    chat_id,
    limit=20
):
    try:
        rows = db_execute(
            """
            SELECT *
            FROM memories
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        )

        result = []

        for row in rows or []:
            content = (
                row.get("content")
                or row.get("text")
                or row.get("memory")
            )

            if content:
                result.append(content)

        return list(reversed(result))

    except Exception as exc:
        log_error("get_memories", exc)
        return []


# ============================================================
# MESSAGES
# ============================================================

def save_message(
    chat_id,
    role,
    message
):
    if message is None:
        message = ""

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
            required_defaults={
                "message": message,
                "text": message,
                "content": message
            }
        )

    except Exception as exc:
        log_error("save_message", exc)


def get_history(
    chat_id,
    limit=MAX_HISTORY_MESSAGES
):
    try:
        rows = db_execute(
            """
            SELECT *
            FROM messages
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        )

        rows = list(reversed(rows or []))

        history = []

        for row in rows:
            role = row.get("role") or "user"

            content = (
                row.get("message")
                or row.get("text")
                or row.get("content")
                or ""
            )

            if content:
                history.append(
                    {
                        "role": role,
                        "content": content
                    }
                )

        return history

    except Exception as exc:
        log_error("get_history", exc)
        return []


# ============================================================
# PROJECT CONTEXT
# ============================================================

def get_project_context():
    facts = get_facts()

    lines = []

    for item in facts:
        lines.append(
            f"- {item['key']}: {item['value']}"
        )

    return "\n".join(lines)


# ============================================================
# WEB SEARCH DETECTION
# ============================================================

def is_web_request(text):
    if not text:
        return False

    t = text.lower().strip()

    explicit = (
        t.startswith("/web")
        or t.startswith("web:")
        or t.startswith("search:")
        or t.startswith("/search")
    )

    if explicit:
        return True

    keywords = [
        "ინტერნეტში მოძებნე",
        "ინტერნეტში მოიძიე",
        "ინტერნეტში ნახე",
        "ვებში მოძებნე",
        "ვებ ძიება",
        "google-ში მოძებნე",
        "ონლაინ მოძებნე",
        "მიმდინარე ინფორმაცია",
        "უახლესი ინფორმაცია",
        "დღევანდელი ინფორმაცია",
        "ახლანდელი ფასი",
        "დღევანდელი ფასი",
        "latest",
        "current",
        "today",
        "recent",
        "search the web",
        "search online",
        "look up",
        "find online",
        "web search"
    ]

    return any(
        keyword in t
        for keyword in keywords
    )


def clean_web_command(text):
    if not text:
        return ""

    t = text.strip()

    prefixes = [
        "/web",
        "/search",
        "web:",
        "search:"
    ]

    for prefix in prefixes:
        if t.lower().startswith(prefix):
            return t[len(prefix):].strip()

    return t


# ============================================================
# GEMINI SYSTEM INSTRUCTION
# ============================================================

GENIOSA_CONSTITUTION = """
You are GENIOSA, a personal business advisor and economic assistant.

CORE IDENTITY:
You advise the founder and management of SAMTISI CONSTRUCTION LLC.

PERMANENT RULES:

1. NEVER invent facts.
2. NEVER invent numbers.
3. NEVER invent URLs.
4. NEVER pretend that an assumption is a confirmed fact.
5. Clearly distinguish:
   - CONFIRMED INTERNAL FACT
   - WEB RESEARCH
   - ASSUMPTION
   - ESTIMATE
   - UNKNOWN
6. If information is missing, say that it is unknown.
7. If a calculation depends on an assumption, state the assumption.
8. Do not silently change project data.
9. Existing company/project facts from PostgreSQL are internal confirmed facts.
10. Web information is research evidence, NOT automatically a confirmed internal fact.
11. When using web information, prefer primary and authoritative sources.
12. When sources disagree, explicitly mention the disagreement.
13. Never claim to have searched the web unless the web search tool was actually used.
14. Never create fake citations.
15. For financial analysis, show formulas and assumptions when useful.
16. For investment decisions, explain risks as well as upside.
17. For legal/regulatory topics, do not pretend to be a lawyer.
18. For current information, use web research when available and necessary.
19. Preserve the user's projects and decisions as persistent context.
20. Be practical, direct and business-oriented.

IMPORTANT:
Internal PostgreSQL facts and Web Research must remain conceptually separate.
Do not automatically save web research as a confirmed fact.
"""


# ============================================================
# GEMINI REQUEST
# ============================================================

def build_gemini_contents(
    chat_id,
    user_text
):
    history = get_history(
        chat_id,
        MAX_HISTORY_MESSAGES
    )

    project_context = get_project_context()

    contents = []

    for item in history:
        role = item["role"]

        if role == "assistant":
            gemini_role = "model"
        else:
            gemini_role = "user"

        contents.append(
            {
                "role": gemini_role,
                "parts": [
                    {
                        "text": item["content"]
                    }
                ]
            }
        )

    if not contents or contents[-1]["role"] != "user":
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "text": user_text
                    }
                ]
            }
        )

    # If last message is somehow different from current input,
    # explicitly append the current request.
    elif contents[-1]["parts"][0]["text"] != user_text:
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "text": user_text
                    }
                ]
            }
        )

    system_text = (
        GENIOSA_CONSTITUTION
        + "\n\nCURRENT INTERNAL COMPANY / PROJECT FACTS:\n"
        + project_context
    )

    return system_text, contents


def extract_grounding(response_json):
    sources = []

    candidates = response_json.get(
        "candidates",
        []
    )

    if not candidates:
        return sources

    candidate = candidates[0]

    metadata = candidate.get(
        "groundingMetadata",
        {}
    )

    # Search queries
    queries = metadata.get(
        "webSearchQueries",
        []
    )

    # Grounding chunks
    chunks = metadata.get(
        "groundingChunks",
        []
    )

    for chunk in chunks:
        web = chunk.get("web")

        if not web:
            continue

        uri = web.get("uri")
        title = web.get("title")

        if uri:
            sources.append(
                {
                    "title": title or uri,
                    "url": uri
                }
            )

    # Deduplicate
    unique = []

    seen = set()

    for source in sources:
        url = source["url"]

        if url in seen:
            continue

        seen.add(url)
        unique.append(source)

    return unique[:MAX_WEB_RESULTS]


def extract_gemini_text(response_json):
    candidates = response_json.get(
        "candidates",
        []
    )

    if not candidates:
        return ""

    candidate = candidates[0]

    parts = (
        candidate
        .get("content", {})
        .get("parts", [])
    )

    texts = []

    for part in parts:
        text = part.get("text")

        if text:
            texts.append(text)

    return "\n".join(texts).strip()


def format_sources(sources):
    if not sources:
        return ""

    lines = [
        "",
        "",
        "🌐 WEB SOURCES",
        "----------------"
    ]

    for index, source in enumerate(
        sources,
        start=1
    ):
        title = source.get(
            "title",
            "Source"
        )

        url = source.get(
            "url",
            ""
        )

        lines.append(
            f"{index}. {title}"
        )

        if url:
            lines.append(url)

    lines.extend(
        [
            "",
            "ℹ️ ეს წყაროები გამოყენებულია Web Research-ისთვის. "
            "მათი ინფორმაცია ავტომატურად არ ითვლება GENIOSA-ს "
            "დადასტურებულ შიდა ფაქტად."
        ]
    )

    return "\n".join(lines)


def ask_gemini(
    chat_id,
    user_text,
    use_web=False
):
    global gemini_status
    global web_status

    if not GEMINI_API_KEY:
        return (
            "Gemini API Key არ არის კონფიგურირებული."
        )

    system_text, contents = build_gemini_contents(
        chat_id,
        user_text
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": system_text
                }
            ]
        },
        "contents": contents
    }

    # --------------------------------------------------------
    # GOOGLE SEARCH GROUNDING
    # --------------------------------------------------------

    if use_web:
        payload["tools"] = [
            {
                "google_search": {}
            }
        ]

    try:
        response = requests.post(
            GEMINI_API,
            json=payload,
            timeout=120
        )

        if response.status_code != 200:
            gemini_status["ok"] = False
            gemini_status["last_error"] = (
                f"HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )

            if use_web:
                web_status["enabled"] = False
                web_status["last_error"] = (
                    gemini_status["last_error"]
                )

            log(
                f"Gemini error {response.status_code}: "
                f"{response.text[:1000]}"
            )

            return (
                "Gemini-სთან დაკავშირებისას ტექნიკური "
                "შეცდომა მოხდა.\n\n"
                f"კოდი: {response.status_code}"
            )

        data = response.json()

        gemini_status["ok"] = True
        gemini_status["last_error"] = None

        text = extract_gemini_text(data)

        if not text:
            return (
                "Gemini-მ ცარიელი პასუხი დააბრუნა."
            )

        sources = []

        if use_web:
            sources = extract_grounding(data)

            web_status["enabled"] = True
            web_status["last_error"] = None
            web_status["last_search"] = datetime.utcnow().isoformat()

        if sources:
            text += format_sources(sources)

            save_research(
                chat_id,
                user_text,
                text,
                sources
            )

        return text

    except requests.RequestException as exc:
        gemini_status["ok"] = False
        gemini_status["last_error"] = str(exc)

        if use_web:
            web_status["enabled"] = False
            web_status["last_error"] = str(exc)

        log_error("Gemini request", exc)

        return (
            "Gemini-სთან კავშირისას შეცდომა მოხდა."
        )

    except Exception as exc:
        gemini_status["ok"] = False
        gemini_status["last_error"] = str(exc)

        if use_web:
            web_status["enabled"] = False
            web_status["last_error"] = str(exc)

        log_error("ask_gemini", exc)

        return (
            "პასუხის დამუშავებისას ტექნიკური შეცდომა მოხდა."
        )


# ============================================================
# RESEARCH STORAGE
# ============================================================

def save_research(
    chat_id,
    query,
    answer,
    sources
):
    try:
        sources_json = json.dumps(
            sources,
            ensure_ascii=False
        )

        dynamic_insert(
            "research",
            {
                "chat_id": chat_id,
                "query": query,
                "answer": answer,
                "sources": sources_json
            }
        )

    except Exception as exc:
        log_error(
            "save_research",
            exc
        )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(
    method,
    payload=None,
    timeout=60
):
    if not TELEGRAM_API:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    url = f"{TELEGRAM_API}/{method}"

    response = requests.post(
        url,
        json=payload or {},
        timeout=timeout
    )

    return response


def telegram_get_me():
    response = telegram_request(
        "getMe",
        timeout=20
    )

    if response.status_code != 200:
        return False

    data = response.json()

    return bool(
        data.get("ok")
    )


def telegram_delete_webhook():
    try:
        response = telegram_request(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            },
            timeout=20
        )

        if response.status_code == 200:
            log("Telegram webhook deleted")

    except Exception as exc:
        log_error(
            "deleteWebhook",
            exc
        )


def send_message(
    chat_id,
    text
):
    if not text:
        return False

    try:
        # Telegram message limit
        chunks = [
            text[i:i + 3900]
            for i in range(
                0,
                len(text),
                3900
            )
        ]

        for chunk in chunks:
            response = telegram_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk
                },
                timeout=30
            )

            if response.status_code != 200:
                log(
                    "Telegram sendMessage error: "
                    f"{response.text[:1000]}"
                )

                return False

        return True

    except Exception as exc:
        log_error(
            "send_message",
            exc
        )

        return False


# ============================================================
# OWNER PROTECTION
# ============================================================

def is_allowed_chat(chat_id):
    if not OWNER_ID:
        return True

    return str(chat_id) == str(OWNER_ID)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def handle_command(
    chat_id,
    text
):
    command = text.split()[0].lower()

    if command == "/start":
        return (
            "გამარჯობა. მე ვარ GENIOSA 5.5 — "
            "შენი პირადი ბიზნეს მრჩეველი.\n\n"
            "ახლა უკვე შემიძლია საჭიროების შემთხვევაში "
            "Google Search-ის საშუალებით მიმდინარე "
            "ინფორმაციის მოძიებაც.\n\n"
            "ძირითადი ბრძანებები:\n"
            "/help — დახმარება\n"
            "/facts — დადასტურებული ფაქტები\n"
            "/memory — მეხსიერება\n"
            "/summary — მიმდინარე კონტექსტი\n"
            "/web — ინტერნეტში ძიება"
        )

    if command == "/help":
        return (
            "GENIOSA 5.5\n\n"
            "/facts — კომპანიისა და პროექტების ფაქტები\n"
            "/memory — შენახული მეხსიერება\n"
            "/summary — მოკლე კონტექსტი\n"
            "/web კითხვა — პირდაპირი Web Search\n\n"
            "ასევე შეგიძლია დაწერო:\n"
            "web: კითხვა\n"
            "search: კითხვა\n\n"
            "Web Research-ის ინფორმაცია "
            "ავტომატურად არ ხდება დადასტურებული ფაქტი."
        )

    if command == "/facts":
        facts = get_facts()

        if not facts:
            return "დადასტურებული ფაქტები ჯერ არ მოიძებნა."

        lines = [
            "📌 CONFIRMED INTERNAL FACTS",
            "----------------------------"
        ]

        for item in facts[:60]:
            lines.append(
                f"{item['key']}: {item['value']}"
            )

        return "\n".join(lines)

    if command == "/memory":
        memories = get_memories(
            chat_id,
            30
        )

        if not memories:
            return "ამ ჩატისთვის შენახული მეხსიერება არ არის."

        lines = [
            "🧠 MEMORY",
            "------------"
        ]

        for memory in memories:
            lines.append(
                f"• {memory}"
            )

        return "\n".join(lines)

    if command == "/summary":
        facts = get_facts()

        projects = [
            x
            for x in facts
            if x["key"].startswith("project.")
        ]

        return (
            "📊 GENIOSA SUMMARY\n\n"
            f"კომპანიის ფაქტები: {len(facts)}\n"
            f"პროექტთან დაკავშირებული ფაქტები: "
            f"{len(projects)}\n"
            f"Web Search: "
            f"{'ON' if GEMINI_API_KEY else 'OFF'}"
        )

    return None


# ============================================================
# TELEGRAM UPDATE HANDLER
# ============================================================

def process_update(update):
    if not update:
        return

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat", {})

    chat_id = chat.get("id")

    if chat_id is None:
        return

    text = message.get("text")

    if not text:
        return

    if not is_allowed_chat(chat_id):
        send_message(
            chat_id,
            "ეს GENIOSA-ს პირადი ბიზნეს ასისტენტია."
        )
        return

    text = text.strip()

    save_message(
        chat_id,
        "user",
        text
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    if text.startswith("/"):
        command_response = handle_command(
            chat_id,
            text
        )

        if command_response:
            save_message(
                chat_id,
                "assistant",
                command_response
            )

            send_message(
                chat_id,
                command_response
            )

            return

    # --------------------------------------------------------
    # Explicit web command
    # --------------------------------------------------------

    web_request = is_web_request(text)

    query = clean_web_command(text)

    # --------------------------------------------------------
    # Ask Gemini
    # --------------------------------------------------------

    answer = ask_gemini(
        chat_id,
        query,
        use_web=web_request
    )

    save_message(
        chat_id,
        "assistant",
        answer
    )

    send_message(
        chat_id,
        answer
    )


# ============================================================
# TELEGRAM POLLING LOCK
# ============================================================

def acquire_polling_lock():
    """
    Uses a dedicated PostgreSQL connection.
    The connection MUST remain open while polling.
    """

    if DB_POOL is None:
        init_db_pool()

    conn = DB_POOL.getconn()

    try:
        conn.autocommit = True

        cur = conn.cursor()

        cur.execute(
            """
            SELECT pg_try_advisory_lock(%s)
            """,
            (
                9173552026,
            )
        )

        row = cur.fetchone()

        acquired = bool(
            row and row[0]
        )

        cur.close()

        if not acquired:
            DB_POOL.putconn(conn)
            return None

        log(
            "PostgreSQL Telegram polling lock acquired"
        )

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
            """
            SELECT pg_advisory_unlock(%s)
            """,
            (
                9173552026,
            )
        )

        cur.close()

    except Exception as exc:
        log_error(
            "release polling lock",
            exc
        )

    finally:
        try:
            DB_POOL.putconn(conn)
        except Exception:
            pass


# ============================================================
# TELEGRAM POLLING
# ============================================================

def polling_loop():
    telegram_status["ok"] = False

    lock_conn = None

    try:
        telegram_delete_webhook()

        if not telegram_get_me():
            telegram_status["last_error"] = (
                "Telegram getMe failed"
            )

            log(
                "Telegram getMe failed"
            )

            return

        telegram_status["ok"] = True

        lock_conn = acquire_polling_lock()

        if lock_conn is None:
            telegram_status["last_error"] = (
                "Another GENIOSA polling instance "
                "already owns the PostgreSQL lock"
            )

            log(
                "Polling lock NOT acquired"
            )

            return

        log("Telegram polling started")

        offset = 0

        while not polling_stop.is_set():

            try:
                response = telegram_request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": [
                            "message"
                        ]
                    },
                    timeout=POLL_TIMEOUT + 10
                )

                if response.status_code != 200:
                    telegram_status["ok"] = False
                    telegram_status["last_error"] = (
                        response.text[:1000]
                    )

                    log(
                        "getUpdates error: "
                        f"{response.text[:1000]}"
                    )

                    time.sleep(
                        POLL_RETRY_DELAY
                    )

                    continue

                data = response.json()

                if not data.get("ok"):
                    telegram_status["ok"] = False

                    time.sleep(
                        POLL_RETRY_DELAY
                    )

                    continue

                telegram_status["ok"] = True
                telegram_status["last_error"] = None

                updates = data.get(
                    "result",
                    []
                )

                for update in updates:
                    try:
                        update_id = update.get(
                            "update_id"
                        )

                        if update_id is not None:
                            offset = update_id + 1

                        telegram_status[
                            "last_update"
                        ] = datetime.utcnow().isoformat()

                        process_update(
                            update
                        )

                    except Exception as exc:
                        log_error(
                            "process_update",
                            exc
                        )

            except requests.RequestException as exc:
                telegram_status["ok"] = False
                telegram_status["last_error"] = str(exc)

                log_error(
                    "Telegram polling request",
                    exc
                )

                time.sleep(
                    POLL_RETRY_DELAY
                )

            except Exception as exc:
                telegram_status["ok"] = False
                telegram_status["last_error"] = str(exc)

                log_error(
                    "Telegram polling loop",
                    exc
                )

                time.sleep(
                    POLL_RETRY_DELAY
                )

    except Exception as exc:
        telegram_status["ok"] = False
        telegram_status["last_error"] = str(exc)

        log_error(
            "polling startup",
            exc
        )

    finally:
        release_polling_lock(
            lock_conn
        )

        log(
            "Telegram polling stopped"
        )


# ============================================================
# HEALTH
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
        db_execute(
            "SELECT 1"
        )

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
        "web_search": {
            "available": bool(GEMINI_API_KEY),
            "active": web_status["enabled"],
            "last_search": web_status["last_search"],
            "last_error": web_status["last_error"]
        },
        "model": GEMINI_MODEL
    }


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():
    global polling_thread

    log(
        f"Starting GENIOSA {APP_VERSION}"
    )

    try:
        init_db_pool()

        run_migrations()

        seed_once()

        log(
            "Database initialization complete"
        )

    except Exception as exc:
        log_error(
            "startup database initialization",
            exc
        )

        raise

    polling_stop.clear()

    polling_thread = threading.Thread(
        target=polling_loop,
        daemon=True,
        name="geniosa-telegram-polling"
    )

    polling_thread.start()

    log(
        "GENIOSA startup completed"
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
def shutdown():
    global DB_POOL

    log(
        "GENIOSA shutdown requested"
    )

    polling_stop.set()

    if polling_thread is not None:
        try:
            polling_thread.join(
                timeout=10
            )
        except Exception:
            pass

    if DB_POOL is not None:
        try:
            DB_POOL.closeall()
        except Exception as exc:
            log_error(
                "close DB pool",
                exc
            )

        DB_POOL = None

    log(
        "GENIOSA shutdown completed"
    )


# ============================================================
# LOCAL EXECUTION
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT
    )
