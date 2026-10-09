# GENIOSA 5.9 — Telegram + Gemini + PostgreSQL
# Additive migrations only. Never drops/truncates/deletes existing records.
import os, json, time, threading, traceback
from datetime import datetime, timezone
import requests
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI

APP_VERSION = "5.9"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
PORT = int(os.getenv("PORT", "8000"))
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
GEMINI_API = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
POLL_TIMEOUT, POLL_RETRY_DELAY, MAX_HISTORY_MESSAGES = 25, 5, 16
POLLING_LOCK_ID, TELEGRAM_MESSAGE_LIMIT = 9173552026, 3900

app = FastAPI(title="GENIOSA", version=APP_VERSION)
DB_POOL = None
DB_POOL_LOCK = threading.Lock()
polling_thread = None
polling_stop = threading.Event()
http = requests.Session()

telegram_status = {
    "ok": False, "polling": False, "lock_acquired": False,
    "bot_username": None, "last_error": None, "last_update": None
}
gemini_status = {"ok": False, "last_error": None, "last_success": None}
web_status = {"enabled": False, "last_error": None, "last_search": None}

CONSTITUTION = """You are GENIOSA, a careful personal business advisor. Never invent facts, figures, sources, URLs, or completed actions. Distinguish user-provided saved facts, web research, assumptions, estimates, and unknowns. If figures conflict, show each value and do not silently choose one. Explain financial formulas and assumptions. Web research is not automatically a confirmed internal fact. Never claim a database write succeeded unless verified. Answer in the user's language, normally Georgian."""

INITIAL_FACTS = {
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
    "geniosa.rule": "Never fabricate facts, figures, sources, or URLs."
}


def log(msg):
    print(f"[GENIOSA {datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def log_error(where, exc):
    log(f"{where}: {type(exc).__name__}: {exc}")


def init_db_pool():
    global DB_POOL
    with DB_POOL_LOCK:
        if DB_POOL is None:
            if not DATABASE_URL:
                raise RuntimeError("DATABASE_URL is not configured")
            DB_POOL = ThreadedConnectionPool(
                1, 8, DATABASE_URL,
                connect_timeout=15,
                application_name="GENIOSA"
            )
    return DB_POOL


class DBConnection:
    def __enter__(self):
        self.pool = init_db_pool()
        self.conn = self.pool.getconn()
        try:
            self.conn.rollback()
            self.conn.autocommit = False
        except Exception:
            self.pool.putconn(self.conn, close=True)
            raise
        return self.conn

    def __exit__(self, typ, val, tb):
        try:
            self.conn.rollback() if typ else self.conn.commit()
        except Exception as exc:
            log_error("transaction", exc)
            try:
                self.conn.rollback()
            except Exception:
                pass
        try:
            self.pool.putconn(self.conn)
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
        return False


def db_execute(sql, params=None, fetch=None):
    with DBConnection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(sql, params or ())
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount
        finally:
            cur.close()


def columns(table):
    rows = db_execute(
        "SELECT column_name,is_nullable,column_default "
        "FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s "
        "ORDER BY ordinal_position",
        (table,), "all"
    )
    return {r["column_name"]: r for r in (rows or [])}


def add_col(table, name, definition):
    if name not in columns(table):
        db_execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def ensure_schema():
    # Existing tables remain intact; only create missing tables and add missing columns.
    ddl = [
        "CREATE TABLE IF NOT EXISTS messages (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, role TEXT, message TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS memories (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, content TEXT, memory_type TEXT, importance INTEGER DEFAULT 5, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS facts (fact_key TEXT PRIMARY KEY, fact_value TEXT)",
        "CREATE TABLE IF NOT EXISTS fact_history (id BIGSERIAL PRIMARY KEY, fact_key TEXT, old_value TEXT, new_value TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS memory_events (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, event_type TEXT, content TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS decisions (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, content TEXT, status TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS tasks (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, content TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS projects (id BIGSERIAL PRIMARY KEY, name TEXT, description TEXT, status TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS research (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, query TEXT, answer TEXT, sources TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS financial_models (id BIGSERIAL PRIMARY KEY, project_name TEXT, data TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS bot_projects (id BIGSERIAL PRIMARY KEY, project_name TEXT, description TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS bot_files (id BIGSERIAL PRIMARY KEY, chat_id BIGINT, filename TEXT, filepath TEXT, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS geniosa_seed_meta_v58 (seed_key TEXT NOT NULL, seed_version TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY(seed_key,seed_version))",
        "CREATE TABLE IF NOT EXISTS geniosa_runtime_v58 (runtime_key TEXT PRIMARY KEY, runtime_value TEXT, updated_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS financial_conflicts (id BIGSERIAL PRIMARY KEY, project_name TEXT NOT NULL, conflict_key TEXT NOT NULL, description TEXT NOT NULL, figures JSONB NOT NULL DEFAULT '[]'::jsonb, status TEXT NOT NULL DEFAULT 'unresolved', resolution TEXT, created_by BIGINT, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW())"
    ]

    for q in ddl:
        db_execute(q)

    for name, defn in [
        ("chat_id", "BIGINT"), ("role", "TEXT"),
        ("message", "TEXT"), ("text", "TEXT"),
        ("content", "TEXT"), ("created_at", "TIMESTAMPTZ DEFAULT NOW()")
    ]:
        add_col("messages", name, defn)

    for name, defn in [
        ("chat_id", "BIGINT"), ("content", "TEXT"),
        ("text", "TEXT"), ("memory", "TEXT"),
        ("memory_type", "TEXT"), ("importance", "INTEGER DEFAULT 5"),
        ("created_at", "TIMESTAMPTZ DEFAULT NOW()")
    ]:
        add_col("memories", name, defn)

    for name, defn in [
        ("fact_key", "TEXT"), ("fact_value", "TEXT"),
        ("content", "TEXT"), ("value", "TEXT"),
        ("text", "TEXT"), ("body", "TEXT"), ("chat_id", "BIGINT")
    ]:
        add_col("facts", name, defn)

    for name, defn in [
        ("fact_key", "TEXT"), ("old_value", "TEXT"),
        ("new_value", "TEXT"), ("created_at", "TIMESTAMPTZ DEFAULT NOW()")
    ]:
        add_col("fact_history", name, defn)

    for name, defn in [
        ("chat_id", "BIGINT"), ("query", "TEXT"),
        ("answer", "TEXT"), ("sources", "TEXT"),
        ("created_at", "TIMESTAMPTZ DEFAULT NOW()")
    ]:
        add_col("research", name, defn)

    db_execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id,created_at DESC)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_memories_chat_created ON memories(chat_id,created_at DESC)")
    db_execute("CREATE INDEX IF NOT EXISTS idx_conflicts_status ON financial_conflicts(status,updated_at DESC)")
    log("Schema migration completed (additive only)")


def runtime_get(key, default=None):
    row = db_execute(
        "SELECT runtime_value FROM geniosa_runtime_v58 WHERE runtime_key=%s",
        (key,), "one"
    )
    return row["runtime_value"] if row else default


def runtime_set(key, value):
    db_execute(
        "INSERT INTO geniosa_runtime_v58(runtime_key,runtime_value,updated_at) "
        "VALUES(%s,%s,NOW()) ON CONFLICT(runtime_key) DO UPDATE "
        "SET runtime_value=EXCLUDED.runtime_value,updated_at=NOW()",
        (key, str(value))
    )


def dynamic_insert(table, values):
    meta = columns(table)
    if not meta:
        raise RuntimeError(f"Table {table} unavailable")

    usable = {k: v for k, v in values.items() if k in meta}

    for name, col in meta.items():
        if (
            col["is_nullable"] == "NO"
            and col["column_default"] is None
            and name not in usable
        ):
            raise RuntimeError(
                f"Required legacy column {table}.{name} has no supplied value"
            )

    if not usable:
        raise RuntimeError(f"No compatible columns for {table}")

    names = list(usable)
    db_execute(
        f'INSERT INTO "{table}" '
        f'({", ".join(chr(34)+n+chr(34) for n in names)}) '
        f'VALUES ({", ".join(["%s"] * len(names))})',
        tuple(usable[n] for n in names)
    )


def fact_val(row):
    for name in ("fact_value", "content", "value", "text", "body"):
        if row.get(name) not in (None, ""):
            return str(row[name])
    return ""


def read_fact(key):
    row = db_execute(
        "SELECT * FROM facts WHERE fact_key=%s LIMIT 1",
        (key,), "one"
    )
    return fact_val(row) if row else None


def create_fact(key, value):
    value = str(value)
    cols = columns("facts")
    oldrow = db_execute(
        "SELECT * FROM facts WHERE fact_key=%s LIMIT 1",
        (key,), "one"
    )

    try:
        if oldrow:
            old = fact_val(oldrow)
            if old == value:
                return True

            try:
                dynamic_insert(
                    "fact_history",
                    {"fact_key": key, "old_value": old, "new_value": value}
                )
            except Exception as exc:
                log_error("fact history", exc)

            assignments = []
            params = []
            for name in ("fact_value", "content", "value", "text", "body"):
                if name in cols:
                    assignments.append(f'"{name}"=%s')
                    params.append(value)

            if not assignments:
                raise RuntimeError("No writable fact value column")

            params.append(key)
            db_execute(
                f'UPDATE facts SET {", ".join(assignments)} WHERE fact_key=%s',
                tuple(params)
            )
        else:
            values = {
                "fact_key": key, "fact_value": value,
                "content": value, "value": value, "text": value,
                "body": value, "chat_id": 0
            }
            dynamic_insert("facts", values)

        return read_fact(key) == value

    except Exception as exc:
        log_error(f"create_fact {key}", exc)
        return False


def get_facts(limit=120):
    rows = db_execute("SELECT * FROM facts LIMIT %s", (limit,), "all") or []
    out = []
    for row in rows:
        if row.get("fact_key") and fact_val(row):
            out.append({
                "key": str(row["fact_key"]),
                "value": fact_val(row)
            })
    return out


def save_message(chat_id, role, message):
    text = str(message or "")
    try:
        dynamic_insert(
            "messages",
            {"chat_id": chat_id, "role": role, "message": text,
             "text": text, "content": text}
        )
        return True
    except Exception as exc:
        log_error("save_message", exc)
        return False


def get_history(chat_id, limit=MAX_HISTORY_MESSAGES):
    cols = columns("messages")
    value_col = next((c for c in ("message", "text", "content") if c in cols), None)
    if not value_col or "chat_id" not in cols:
        return []

    order = '"created_at" DESC' if "created_at" in cols else (
        '"id" DESC' if "id" in cols else "1"
    )

    rows = db_execute(
        f'SELECT * FROM messages WHERE chat_id=%s ORDER BY {order} LIMIT %s',
        (chat_id, limit), "all"
    ) or []

    out = []
    for row in reversed(rows):
        content = row.get("message") or row.get("text") or row.get("content")
        if content:
            out.append({
                "role": "model" if (row.get("role") or "user").lower()
                in ("assistant", "model", "bot") else "user",
                "content": str(content)
            })
    return out


def save_memory(chat_id, content, memory_type="general", importance=5):
    try:
        dynamic_insert(
            "memories",
            {"chat_id": chat_id, "content": content, "text": content,
             "memory": content, "memory_type": memory_type,
             "importance": importance}
        )
        db_execute(
            "INSERT INTO memory_events(chat_id,event_type,content) VALUES(%s,%s,%s)",
            (chat_id, "memory_saved", str(content)[:4000])
        )
        return True
    except Exception as exc:
        log_error("save_memory", exc)
        return False


def get_memories(chat_id, limit=25):
    cols = columns("memories")
    value_col = next((c for c in ("content", "text", "memory") if c in cols), None)
    if not value_col or "chat_id" not in cols:
        return []

    order = '"created_at" DESC' if "created_at" in cols else (
        '"id" DESC' if "id" in cols else "1"
    )

    rows = db_execute(
        f'SELECT * FROM messages WHERE chat_id=%s ORDER BY {order} LIMIT %s'
        if False else
        f'SELECT * FROM memories WHERE chat_id=%s ORDER BY {order} LIMIT %s',
        (chat_id, limit), "all"
    ) or []

    return [str(r[value_col]) for r in reversed(rows) if r.get(value_col)]


def add_conflict(project, key, description, figures, created_by=None):
    if isinstance(figures, str):
        figures = [
            {"figure": v.strip(), "source": "user-provided"}
            for v in figures.split(";") if v.strip()
        ]

    row = db_execute(
        "INSERT INTO financial_conflicts "
        "(project_name,conflict_key,description,figures,status,created_by) "
        "VALUES(%s,%s,%s,%s::jsonb,'unresolved',%s) RETURNING id",
        (project, key, description, json.dumps(figures, ensure_ascii=False), created_by),
        "one"
    )
    return row["id"]


def get_conflicts(status="unresolved", limit=25):
    return db_execute(
        "SELECT * FROM financial_conflicts WHERE status=%s "
        "ORDER BY updated_at DESC LIMIT %s",
        (status, limit), "all"
    ) or []


def conflict_context():
    rows = get_conflicts()
    if not rows:
        return "No unresolved financial conflicts are currently registered."

    return "\n".join(
        f"ID {r['id']} | Project: {r['project_name']} | "
        f"Key: {r['conflict_key']} | {r['description']} | "
        f"Figures: {json.dumps(r['figures'], ensure_ascii=False, default=str)}"
        for r in rows
    )


def get_context(chat_id):
    facts = "\n".join(
        f"- {f['key']}: {f['value']} (user-provided; not independently verified)"
        for f in get_facts()
    ) or "No saved facts."

    memories = "\n".join(
        "- " + m for m in get_memories(chat_id)
    ) or "No saved memories for this chat."

    return (
        f"SAVED COMPANY FACTS:\n{facts}\n\n"
        f"CHAT MEMORIES:\n{memories}\n\n"
        f"UNRESOLVED FINANCIAL CONFLICTS:\n{conflict_context()}"
    )


def save_research(chat_id, query, answer, sources):
    try:
        dynamic_insert(
            "research",
            {"chat_id": chat_id, "query": query, "answer": answer,
             "sources": json.dumps(sources, ensure_ascii=False)}
        )
    except Exception as exc:
        log_error("save_research", exc)


def gemini_text(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    return "\n".join(
        p.get("text", "")
        for p in candidates[0].get("content", {}).get("parts", [])
        if p.get("text")
    ).strip()


def grounding_sources(data):
    candidates = data.get("candidates") or []
    meta = candidates[0].get("groundingMetadata", {}) if candidates else {}
    out = []
    seen = set()

    for chunk in meta.get("groundingChunks", []) or []:
        web = chunk.get("web", {})
        url = web.get("uri")
        if url and url not in seen:
            seen.add(url)
            out.append({"title": web.get("title") or url, "url": url})

    return out[:8]


def ask_gemini(chat_id, text, use_web=False):
    if not GEMINI_API_KEY:
        return "შეცდომა: GEMINI_API_KEY არ არის Render Environment-ში დაყენებული."

    history = get_history(chat_id)
    if (
        history
        and history[-1]["role"] == "user"
        and history[-1]["content"].strip() == text.strip()
    ):
        history = history[:-1]

    contents = [
        {"role": h["role"], "parts": [{"text": h["content"][:8000]}]}
        for h in history
    ]
    contents.append({"role": "user", "parts": [{"text": text[:12000]}]})

    payload = {
        "systemInstruction": {
            "parts": [{
                "text": CONSTITUTION + "\n\nBUSINESS CONTEXT:\n" + get_context(chat_id)
            }]
        },
        "contents": contents,
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": 4096}
    }

    if use_web:
        payload["tools"] = [{"google_search": {}}]

    try:
        response = http.post(
            GEMINI_API,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=100
        )

        if response.status_code >= 400:
            detail = response.text[:700]
            gemini_status.update(
                ok=False,
                last_error=f"HTTP {response.status_code}: {detail}"
            )
            if use_web:
                web_status["last_error"] = detail
            log(gemini_status["last_error"])
            return (
                f"Gemini API შეცდომა (HTTP {response.status_code}). "
                "გადაამოწმე Render Logs და GEMINI_MODEL."
            )

        data = response.json()
        answer = gemini_text(data)
        if not answer:
            raise RuntimeError("Gemini returned no answer text")

        gemini_status.update(
            ok=True, last_error=None,
            last_success=datetime.now(timezone.utc).isoformat()
        )

        if use_web:
            sources = grounding_sources(data)
            web_status.update(
                enabled=bool(sources), last_error=None,
                last_search=datetime.now(timezone.utc).isoformat()
            )

            if sources:
                answer += "\n\n🌐 წყაროები:\n" + "\n".join(
                    f"{i}. {s['title']}\n{s['url']}"
                    for i, s in enumerate(sources, 1)
                )
                answer += (
                    "\n\nვებ-კვლევა ავტომატურად არ ითვლება "
                    "დადასტურებულ შიდა ფაქტად."
                )
                save_research(chat_id, text, answer, sources)
            else:
                answer += (
                    "\n\nშენიშვნა: Google Search-ის წყაროები პასუხში "
                    "არ დაბრუნებულა; მნიშვნელოვანი ინფორმაცია გადაამოწმე."
                )

        return answer

    except Exception as exc:
        gemini_status.update(ok=False, last_error=str(exc))
        log_error("ask_gemini", exc)
        return (
            "Gemini-სთან დაკავშირებისას ან პასუხის დამუშავებისას "
            "შეცდომა მოხდა. გადაამოწმე Render Logs."
        )


def tg_request(method, payload=None, timeout=50):
    if not TELEGRAM_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return http.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=timeout
    )


def send_message(chat_id, text):
    text = str(text or "")
    for start in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        r = tg_request(
            "sendMessage",
            {"chat_id": chat_id, "text": text[start:start + TELEGRAM_MESSAGE_LIMIT]},
            30
        )
        if r.status_code != 200 or not r.json().get("ok"):
            log(f"sendMessage error: {r.text[:500]}")
            return False
    return True


def memory_test():
    key = "GENIOSA_MEMORY_TEST_731"
    expected = "BLUE_ORCHID_2026"
    if not create_fact(key, expected):
        return False
    return read_fact(key) == expected


def handle_command(chat_id, text, user_id):
    cmd = text.split()[0].lower().split("@")[0]

    if cmd == "/start":
        return (
            f"გამარჯობა! მე ვარ GENIOSA {APP_VERSION}.\n"
            "/help — ბრძანებები\n"
            "/facts — ფაქტები\n"
            "/memory — მეხსიერება\n"
            "/memory_test — ბაზის ტესტი\n"
            "/conflicts — ფინანსური წინააღმდეგობები\n"
            "/conflicts_test — წინააღმდეგობის ტესტი\n"
            "/summary — შეჯამება\n"
            "/web კითხვა — ინტერნეტში ძიება\n"
            "/health — სტატუსი\n\n"
            "მეხსიერების შესანახად: დაიმახსოვრე: ტექსტი\n"
            "წინააღმდეგობის შესანახად: წინააღმდეგობა: პროექტი | მაჩვენებელი | "
            "თანხა A; თანხა B | განმარტება"
        )

    if cmd == "/help":
        return (
            "ბრძანებები: /start, /help, /facts, /memory, /memory_test, "
            "/conflicts, /conflicts_test, /summary, /web კითხვა, /health. "
            "მეხსიერება: დაიმახსოვრე: ტექსტი. "
            "წინააღმდეგობა: პროექტი | მაჩვენებელი | თანხა A; თანხა B | განმარტება"
        )

    if cmd == "/facts":
        facts = get_facts()
        return (
            "შენახული ფაქტები:\n" +
            "\n".join(f"• {x['key']}: {x['value']}" for x in facts[:70])
            if facts else "ფაქტები ვერ მოიძებნა."
        )

    if cmd == "/memory":
        mem = get_memories(chat_id)
        return (
            "შენახული მეხსიერება:\n" +
            "\n".join("• " + m for m in mem)
            if mem else "ამ ჩატისთვის მეხსიერება ვერ მოიძებნა."
        )

    if cmd == "/memory_test":
        return (
            "✅ PostgreSQL memory test successful: ჩაწერა და ხელახლა წაკითხვა დადასტურდა."
            if memory_test()
            else "❌ Memory test ვერ დადასტურდა. გადაამოწმე Render Logs."
        )

    if cmd == "/conflicts":
        rows = get_conflicts()
        return (
            "გადაუჭრელი ფინანსური წინააღმდეგობები:\n\n" +
            "\n\n".join(
                f"#{r['id']} {r['project_name']} / {r['conflict_key']}\n"
                f"{r['description']}\n"
                f"მონაცემები: {json.dumps(r['figures'], ensure_ascii=False, default=str)}"
                for r in rows
            )
            if rows else "გადაუჭრელი ფინანსური წინააღმდეგობები არ არის შენახული."
        )

    if cmd == "/conflicts_test":
        row = db_execute(
            "SELECT id FROM financial_conflicts "
            "WHERE project_name=%s AND conflict_key=%s AND status='unresolved' LIMIT 1",
            ("GENIOSA TEST", "TEST_59"), "one"
        )
        if row:
            return f"Financial conflict test OK; test record already exists, ID {row['id']}."

        cid = add_conflict(
            "GENIOSA TEST", "TEST_59",
            "სატესტო ჩანაწერი, რეალური პროექტის მონაცემი არ არის.",
            [
                {"figure": "100", "source": "test"},
                {"figure": "120", "source": "test"}
            ],
            user_id
        )
        check = db_execute(
            "SELECT id FROM financial_conflicts WHERE id=%s",
            (cid,), "one"
        )
        return (
            f"✅ Financial conflict test successful; record ID {check['id']}. ეს სატესტო ჩანაწერია."
            if check else "❌ ტესტი ვერ დადასტურდა."
        )

    if cmd == "/summary":
        return (
            f"GENIOSA {APP_VERSION}\n"
            f"შენახული ფაქტები: {len(get_facts())}\n"
            f"გადაუჭრელი ფინანსური წინააღმდეგობები: {len(get_conflicts())}\n"
            "შენახული მონაცემები უმეტესად მომხმარებლისგანაა და "
            "დამოუკიდებლად დადასტურებული არ არის."
        )

    if cmd == "/health":
        return (
            f"GENIOSA {APP_VERSION}\n"
            f"Telegram polling: {telegram_status['polling']}\n"
            f"Gemini OK: {gemini_status['ok']}\n"
            f"Web Search წყაროები მიღებულია: {web_status['enabled']}\n"
            f"ბოლო შეცდომა: {telegram_status.get('last_error') or gemini_status.get('last_error') or 'არ არის'}"
        )

    return None


def process_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = (msg.get("chat") or {}).get("id")
    user_id = (msg.get("from") or {}).get("id")
    text = msg.get("text")

    if chat_id is None or not isinstance(text, str):
        return

    if OWNER_ID and str(user_id) != OWNER_ID and str(chat_id) != OWNER_ID:
        send_message(chat_id, "ეს GENIOSA-ს პირადი ბიზნეს ასისტენტია.")
        return

    text = text.strip()
    if not text:
        return

    save_message(chat_id, "user", text)

    if text.startswith("/"):
        cmd = text.split()[0].lower().split("@")[0]

        if cmd in ("/web", "/search"):
            parts = text.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ""
            answer = (
                ask_gemini(chat_id, query, True)
                if query else "გამოყენება: /web ჩაწერე საძიებო კითხვა."
            )
            save_message(chat_id, "assistant", answer)
            send_message(chat_id, answer)
            return

        answer = handle_command(chat_id, text, user_id)
        if answer is not None:
            save_message(chat_id, "assistant", answer)
            send_message(chat_id, answer)
            return

    import re

    m = re.match(
        r"^\s*(დაიმახსოვრე|შეინახე მეხსიერებაში|remember)\s*:\s*(.+)$",
        text, re.I | re.S
    )
    if m:
        ok = save_memory(chat_id, m.group(2).strip())
        answer = (
            "მეხსიერება შენახულია."
            if ok else "მეხსიერების შენახვა ვერ დადასტურდა. გადაამოწმე Logs."
        )
        save_message(chat_id, "assistant", answer)
        send_message(chat_id, answer)
        return

    m = re.match(r"^\s*წინააღმდეგობა\s*:\s*(.+)$", text, re.I | re.S)
    if m:
        parts = [p.strip() for p in m.group(1).split("|", 3)]
        if len(parts) < 4:
            answer = (
                "ფორმატი: წინააღმდეგობა: პროექტი | მაჩვენებელი | "
                "თანხა A; თანხა B | განმარტება"
            )
        else:
            try:
                cid = add_conflict(parts[0], parts[1], parts[3], parts[2], user_id)
                answer = (
                    f"ფინანსური წინააღმდეგობა შენახულია. ID: {cid}. "
                    "ეს არ ნიშნავს, რომ რომელიმე თანხა სწორია."
                )
            except Exception as exc:
                log_error("save conflict", exc)
                answer = "წინააღმდეგობის შენახვა ვერ დადასტურდა. გადაამოწმე Render Logs."

        save_message(chat_id, "assistant", answer)
        send_message(chat_id, answer)
        return

    low = text.lower()
    web_terms = (
        "ინტერნეტში მოძებნე", "ინტერნეტში მოიძიე",
        "უახლესი ინფორმაცია", "მიმდინარე ინფორმაცია",
        "search the web", "search online", "web search"
    )
    use_web = any(k in low for k in web_terms)
    query = text

    answer = ask_gemini(chat_id, query, use_web)
    save_message(chat_id, "assistant", answer)
    send_message(chat_id, answer)


def acquire_polling_lock():
    pool = init_db_pool()
    conn = pool.getconn()
    try:
        conn.rollback()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (POLLING_LOCK_ID,))
        ok = bool(cur.fetchone()[0])
        cur.close()

        if not ok:
            pool.putconn(conn)
            return None

        return conn

    except Exception:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        raise


def release_polling_lock(conn):
    if conn is None:
        return

    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_unlock(%s)", (POLLING_LOCK_ID,))
        cur.close()
    except Exception as exc:
        log_error("release lock", exc)
    finally:
        try:
            DB_POOL.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def polling_loop():
    lock_conn = None

    try:
        init_db_pool()

        while not polling_stop.is_set():
            lock_conn = acquire_polling_lock()
            if lock_conn:
                break
            polling_stop.wait(POLL_RETRY_DELAY)

        if polling_stop.is_set():
            return

        telegram_status["lock_acquired"] = True

        me = tg_request("getMe", timeout=20).json()
        if not me.get("ok"):
            raise RuntimeError(f"Telegram getMe failed: {me}")

        telegram_status.update(
            ok=True,
            bot_username=me["result"].get("username"),
            polling=True,
            last_error=None
        )

        wh = tg_request("deleteWebhook", {"drop_pending_updates": False}, 20)
        if wh.status_code != 200:
            log(f"deleteWebhook warning: {wh.text[:300]}")

        try:
            offset = int(runtime_get("telegram_next_offset", "0") or "0")
        except (TypeError, ValueError):
            offset = 0

        log(f"Polling started for @{telegram_status['bot_username']}, offset={offset}")

        while not polling_stop.is_set():
            try:
                r = tg_request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": ["message", "edited_message"]
                    },
                    POLL_TIMEOUT + 10
                )

                if r.status_code != 200:
                    raise RuntimeError(
                        f"getUpdates HTTP {r.status_code}: {r.text[:300]}"
                    )

                data = r.json()
                if not data.get("ok"):
                    raise RuntimeError(f"getUpdates API error: {data}")

                telegram_status.update(ok=True, last_error=None)

                for update in data.get("result", []):
                    try:
                        process_update(update)
                        offset = max(offset, int(update.get("update_id", 0)) + 1)
                        runtime_set("telegram_next_offset", offset)
                        telegram_status["last_update"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                    except Exception as exc:
                        log_error("process update", exc)
                        log(traceback.format_exc())

            except Exception as exc:
                telegram_status.update(ok=False, last_error=str(exc))
                log_error("polling", exc)
                polling_stop.wait(POLL_RETRY_DELAY)

    except Exception as exc:
        telegram_status.update(ok=False, last_error=str(exc))
        log_error("polling startup", exc)
        log(traceback.format_exc())

    finally:
        telegram_status.update(polling=False, lock_acquired=False)
        release_polling_lock(lock_conn)
        log("Polling loop stopped")


@app.get("/")
def root():
    return {"app": "GENIOSA", "version": APP_VERSION, "status": "online"}


@app.get("/health")
def health():
    try:
        db_execute("SELECT 1")
        db_ok = True
        db_error = None
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    return {
        "app": "GENIOSA",
        "version": APP_VERSION,
        "database": {"ok": db_ok, "error": db_error},
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

    for key, value in INITIAL_FACTS.items():
        if read_fact(key) is None and not create_fact(key, value):
            log(f"Warning: initial fact not verified: {key}")

    polling_stop.clear()
    polling_thread = threading.Thread(
        target=polling_loop,
        daemon=True,
        name="geniosa-telegram-polling"
    )
    polling_thread.start()


@app.on_event("shutdown")
def shutdown():
    global DB_POOL

    polling_stop.set()

    if polling_thread:
        polling_thread.join(timeout=35)

    if polling_thread and polling_thread.is_alive():
        log("Polling thread still running; keeping pool open")
        return

    with DB_POOL_LOCK:
        if DB_POOL is not None:
            try:
                DB_POOL.closeall()
            except Exception as exc:
                log_error("close pool", exc)
            DB_POOL = None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT)
