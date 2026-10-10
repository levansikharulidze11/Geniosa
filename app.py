# ============================================================
# GENIOSA 6.0
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + FastAPI
# ============================================================

import os
import re
import json
import time
import threading
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.responses import JSONResponse

APP_VERSION = "GENIOSA 6.0"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-3.5-flash-lite"
).strip()
GENIOSA_OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()

ENABLE_GOOGLE_SEARCH = os.getenv(
    "ENABLE_GOOGLE_SEARCH", "true"
).lower() in ("1", "true", "yes", "on")

HTTP_TIMEOUT = 45
POLL_TIMEOUT = 25
MAX_TELEGRAM_LENGTH = 3900

POLL_LOCK_KEY_1 = 621047
POLL_LOCK_KEY_2 = 603006

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN else ""
)

GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

app = FastAPI(
    title=APP_VERSION,
    description="Personal Business Advisor",
    version="6.0"
)

stop_event = threading.Event()
polling_lock = threading.Lock()
polling_thread = None

LAST_ERROR = ""
LAST_GEMINI_OK = False
LAST_WEB_SEARCH_USED = False
LAST_GEMINI_CHECK = None
LAST_TELEGRAM_UPDATE = None

BOT_USERNAME = ""
BOT_ID = None

SYSTEM_INSTRUCTIONS = """
You are GENIOSA 6.0, the personal business advisor and economic
analyst for SAMTISI CONSTRUCTION LLC.

RULES:
1. Be accurate, factual and transparent.
2. Never invent facts, prices, financial results, sources or actions.
3. Distinguish user-provided facts, verified information, estimates,
   assumptions and unknown information.
4. Check financial calculations and show formulas where useful.
5. Consider construction costs, financing, taxes, timelines, cash flow,
   sales, rental income, risks and investor returns when relevant.
6. Never claim information was saved unless the database write succeeded.
7. Never claim a web search occurred unless grounding metadata confirms it.
8. For legal, tax and regulatory matters, recommend checking current
   authoritative sources where appropriate.
9. Treat stored user information as context, not independently verified fact.
10. Reply in the user's language. Georgian questions require Georgian answers.
11. Keep facts, decisions, tasks and tentative proposals distinct.
12. Never disclose API keys, passwords or environment variables.
13. Never claim an external action succeeded unless it actually did.
"""


# ============================================================
# UTILITIES
# ============================================================

def log(message):
    print(
        f"[{APP_VERSION} "
        f"{datetime.now(timezone.utc).isoformat()}] {message}",
        flush=True
    )


def record_error(message):
    global LAST_ERROR
    LAST_ERROR = str(message)[:3000]
    log("ERROR: " + LAST_ERROR)


def now_utc():
    return datetime.now(timezone.utc)


def safe_json(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str
        )
    except Exception:
        return "{}"


def parse_chat_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_owner(chat_id):
    # Set GENIOSA_OWNER_ID in Render for private access.
    return (
        not GENIOSA_OWNER_ID
        or str(chat_id) == GENIOSA_OWNER_ID
    )


# ============================================================
# POSTGRESQL
# ============================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15,
        cursor_factory=RealDictCursor,
        application_name="geniosa-6.0"
    )


def db_execute(
    query,
    params=None,
    fetchone=False,
    fetchall=False
):
    conn = None

    try:
        conn = db_connect()

        with conn:
            with conn.cursor() as cur:
                cur.execute(query, params or ())

                if fetchone:
                    return cur.fetchone()

                if fetchall:
                    return cur.fetchall()

                return cur.rowcount

    finally:
        if conn is not None:
            conn.close()


def get_table_columns(table_name):
    if not re.fullmatch(
        r"[a-zA-Z_][a-zA-Z0-9_]*",
        table_name
    ):
        raise ValueError("Invalid table name")

    rows = db_execute(
        """
        SELECT column_name, is_nullable, column_default,
               data_type, udt_name, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
        fetchall=True
    )

    return {
        row["column_name"]: row
        for row in rows
    }


def migrate_database():
    # Additive-only migration. No DROP, TRUNCATE or DELETE.

    create_statements = [
        """
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            role TEXT,
            message TEXT,
            text TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            category TEXT DEFAULT 'general',
            content TEXT,
            text TEXT,
            memory TEXT,
            memory_type TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 5,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS facts (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            key TEXT,
            value TEXT,
            content TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            name TEXT,
            content TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT DEFAULT 'recorded',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT DEFAULT 'open',
            due_date TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS financial_conflicts (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            subject TEXT,
            old_value TEXT,
            new_value TEXT,
            details TEXT,
            status TEXT DEFAULT 'unresolved',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    ]

    for statement in create_statements:
        db_execute(statement)

    additions = {
        "messages": {
            "chat_id": "BIGINT",
            "role": "TEXT",
            "message": "TEXT",
            "text": "TEXT",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "memories": {
            "chat_id": "BIGINT",
            "category": "TEXT DEFAULT 'general'",
            "content": "TEXT",
            "text": "TEXT",
            "memory": "TEXT",
            "memory_type": "TEXT DEFAULT 'general'",
            "importance": "INTEGER DEFAULT 5",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "memory_events": {
            "chat_id": "BIGINT",
            "event_type": "TEXT",
            "content": "TEXT",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "facts": {
            "chat_id": "BIGINT",
            "key": "TEXT",
            "value": "TEXT",
            "content": "TEXT",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "projects": {
            "chat_id": "BIGINT",
            "name": "TEXT",
            "content": "TEXT",
            "status": "TEXT DEFAULT 'active'",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "decisions": {
            "chat_id": "BIGINT",
            "content": "TEXT",
            "status": "TEXT DEFAULT 'recorded'",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "tasks": {
            "chat_id": "BIGINT",
            "content": "TEXT",
            "status": "TEXT DEFAULT 'open'",
            "due_date": "TEXT",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "financial_conflicts": {
            "chat_id": "BIGINT",
            "subject": "TEXT",
            "old_value": "TEXT",
            "new_value": "TEXT",
            "details": "TEXT",
            "status": "TEXT DEFAULT 'unresolved'",
            "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
        "app_state": {
            "value": "TEXT",
            "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
        },
    }

    for table_name, columns in additions.items():
        for column_name, definition in columns.items():
            db_execute(
                f'ALTER TABLE "{table_name}" '
                f'ADD COLUMN IF NOT EXISTS "{column_name}" '
                f'{definition}'
            )

    log("Additive database migration completed")


# ============================================================
# LEGACY DATABASE INSERTS
# ============================================================

ALIASES = {
    "chat_id": ("chat_id", "user_id", "telegram_id"),
    "user_id": ("user_id", "chat_id", "telegram_id"),
    "telegram_id": ("telegram_id", "chat_id", "user_id"),
    "content": ("content", "text", "memory", "message", "details"),
    "text": ("text", "content", "memory", "message", "details"),
    "memory": ("memory", "content", "text", "message", "details"),
    "message": ("message", "text", "content"),
    "category": ("category", "memory_type", "type"),
    "memory_type": ("memory_type", "category", "type"),
    "type": ("type", "memory_type", "category"),
    "role": ("role",),
    "importance": ("importance",),
    "status": ("status",),
    "name": ("name", "project_name", "subject"),
    "project_name": ("project_name", "name", "subject"),
    "subject": ("subject", "name"),
    "key": ("key",),
    "value": ("value", "content", "text"),
    "old_value": ("old_value",),
    "new_value": ("new_value",),
    "details": ("details", "content", "text"),
    "event_type": ("event_type", "type"),
    "due_date": ("due_date",),
}


def infer_required_value(column_name, values):
    name = column_name.lower()

    for candidate in ALIASES.get(name, ()):
        if candidate in values and values[candidate] is not None:
            return values[candidate]

    if name in (
        "created_at", "updated_at", "timestamp", "date"
    ):
        return now_utc()

    if name in ("category", "memory_type", "type"):
        return (
            values.get("memory_type")
            or values.get("category")
            or "general"
        )

    if name == "importance":
        return 5

    if name == "role":
        return values.get("role", "system")

    if name == "status":
        return values.get("status", "recorded")

    if name in ("chat_id", "user_id", "telegram_id"):
        return values.get("chat_id") or values.get("user_id")

    if name in ("content", "text", "memory", "message", "details"):
        return (
            values.get("content")
            or values.get("text")
            or values.get("memory")
            or values.get("message")
            or values.get("details")
        )

    return None


def dynamic_insert(table_name, values):
    if not re.fullmatch(
        r"[a-zA-Z_][a-zA-Z0-9_]*",
        table_name
    ):
        raise ValueError("Invalid table name")

    metadata = get_table_columns(table_name)

    if not metadata:
        raise RuntimeError(
            f"Table '{table_name}' does not exist"
        )

    payload = {
        key: value
        for key, value in values.items()
        if key in metadata and value is not None
    }

    if table_name == "memories" and "category" in metadata:
        payload.setdefault(
            "category",
            values.get("memory_type")
            or values.get("category")
            or "general"
        )

    for column_name, info in metadata.items():
        if column_name in payload:
            continue

        if info["is_nullable"] == "YES":
            continue

        if info["column_default"] is not None:
            continue

        if info.get("is_identity") == "YES":
            continue

        if info.get("is_generated") == "ALWAYS":
            continue

        value = infer_required_value(
            column_name,
            {**values, **payload}
        )

        if value is not None:
            payload[column_name] = value
        else:
            raise RuntimeError(
                f"Required legacy column {table_name}."
                f"{column_name} has no compatible value"
            )

    if not payload:
        raise RuntimeError(
            f"No compatible values for table '{table_name}'"
        )

    columns = list(payload.keys())

    for column in columns:
        if not re.fullmatch(
            r"[a-zA-Z_][a-zA-Z0-9_]*",
            column
        ):
            raise ValueError("Invalid column name")

    column_sql = ", ".join(
        f'"{column}"' for column in columns
    )
    placeholders = ", ".join(["%s"] * len(columns))
    params = [payload[column] for column in columns]

    return db_execute(
        f'INSERT INTO "{table_name}" '
        f'({column_sql}) VALUES ({placeholders}) RETURNING *',
        params,
        fetchone=True
    )


# ============================================================
# MESSAGES AND MEMORY
# ============================================================

def save_message(chat_id, role, content):
    if not content:
        return False

    try:
        dynamic_insert(
            "messages",
            {
                "chat_id": chat_id,
                "user_id": chat_id,
                "telegram_id": chat_id,
                "role": role,
                "message": content,
                "text": content,
                "content": content,
                "created_at": now_utc(),
            }
        )
        return True

    except Exception as exc:
        record_error(f"save_message: {exc}")
        return False


def get_recent_messages(chat_id, limit=12):
    metadata = get_table_columns("messages")

    if "chat_id" not in metadata or "role" not in metadata:
        return []

    content_column = next(
        (
            name for name in ("message", "text", "content")
            if name in metadata
        ),
        None
    )

    if not content_column:
        return []

    order_column = (
        "created_at" if "created_at" in metadata
        else "id" if "id" in metadata
        else None
    )

    order_sql = (
        f'"{order_column}" DESC'
        if order_column else "1 DESC"
    )

    rows = db_execute(
        f'SELECT role, "{content_column}" AS content '
        f'FROM messages WHERE chat_id = %s '
        f'ORDER BY {order_sql} LIMIT %s',
        (chat_id, limit),
        fetchall=True
    )

    rows.reverse()
    result = []

    for row in rows:
        content = str(row.get("content") or "").strip()

        if not content:
            continue

        role = (
            "model"
            if row.get("role") in ("assistant", "model", "system")
            else "user"
        )

        result.append({
            "role": role,
            "parts": [{"text": content}]
        })

    return result


def save_memory(
    chat_id,
    content,
    memory_type="general",
    importance=5
):
    content = str(content or "").strip()

    if not content:
        return False

    memory_type = str(memory_type or "general")[:100]

    try:
        row = dynamic_insert(
            "memories",
            {
                "chat_id": chat_id,
                "user_id": chat_id,
                "telegram_id": chat_id,
                "category": memory_type,
                "memory_type": memory_type,
                "type": memory_type,
                "content": content,
                "text": content,
                "memory": content,
                "importance": max(
                    1, min(10, int(importance))
                ),
                "created_at": now_utc(),
            }
        )

        if not row:
            raise RuntimeError("Memory insert returned no record")

        try:
            dynamic_insert(
                "memory_events",
                {
                    "chat_id": chat_id,
                    "user_id": chat_id,
                    "event_type": "memory_saved",
                    "content": content[:2000],
                    "created_at": now_utc(),
                }
            )
        except Exception as exc:
            log(f"memory_events warning: {exc}")

        log(f"Memory saved for chat_id={chat_id}")
        return True

    except Exception as exc:
        record_error(f"save_memory: {exc}")
        return False


def get_memories(chat_id, limit=25):
    metadata = get_table_columns("memories")

    if "chat_id" not in metadata:
        return []

    content_column = next(
        (
            name for name in ("content", "text", "memory")
            if name in metadata
        ),
        None
    )

    if not content_column:
        return []

    order_column = (
        "created_at" if "created_at" in metadata
        else "id" if "id" in metadata
        else None
    )

    order_sql = (
        f'"{order_column}" DESC'
        if order_column else "1 DESC"
    )

    rows = db_execute(
        f'SELECT "{content_column}" AS content '
        f'FROM memories WHERE chat_id = %s '
        f'ORDER BY {order_sql} LIMIT %s',
        (chat_id, limit),
        fetchall=True
    )

    return [
        str(row["content"])
        for row in rows
        if row.get("content")
    ]


def save_fact(chat_id, key, value):
    return dynamic_insert(
        "facts",
        {
            "chat_id": chat_id,
            "user_id": chat_id,
            "key": str(key)[:500],
            "value": str(value),
            "content": f"{key}: {value}",
            "created_at": now_utc(),
        }
    )


def get_facts(chat_id, limit=50):
    metadata = get_table_columns("facts")

    if "chat_id" not in metadata:
        return []

    order_column = (
        "created_at" if "created_at" in metadata
        else "id" if "id" in metadata
        else None
    )

    order_sql = (
        f'"{order_column}" DESC'
        if order_column else "1 DESC"
    )

    rows = db_execute(
        f'SELECT * FROM facts WHERE chat_id = %s '
        f'ORDER BY {order_sql} LIMIT %s',
        (chat_id, limit),
        fetchall=True
    )

    result = []

    for row in reversed(rows):
        key = row.get("key")
        value = row.get("value")

        if key is not None and value is not None:
            result.append(f"{key}: {value}")
        elif row.get("content"):
            result.append(str(row["content"]))

    return result


def save_project(chat_id, name, content, status="active"):
    return dynamic_insert(
        "projects",
        {
            "chat_id": chat_id,
            "user_id": chat_id,
            "name": name,
            "project_name": name,
            "content": content,
            "status": status,
            "created_at": now_utc(),
        }
    )


def save_decision(chat_id, content, status="recorded"):
    return dynamic_insert(
        "decisions",
        {
            "chat_id": chat_id,
            "user_id": chat_id,
            "content": content,
            "status": status,
            "created_at": now_utc(),
        }
    )


def save_task(chat_id, content, due_date=None):
    return dynamic_insert(
        "tasks",
        {
            "chat_id": chat_id,
            "user_id": chat_id,
            "content": content,
            "status": "open",
            "due_date": due_date,
            "created_at": now_utc(),
        }
    )


def save_financial_conflict(
    chat_id,
    subject,
    old_value,
    new_value,
    details=""
):
    return dynamic_insert(
        "financial_conflicts",
        {
            "chat_id": chat_id,
            "user_id": chat_id,
            "subject": subject,
            "old_value": str(old_value),
            "new_value": str(new_value),
            "details": details,
            "status": "unresolved",
            "created_at": now_utc(),
        }
    )


def build_context(chat_id):
    sections = []

    try:
        facts = get_facts(chat_id)
        if facts:
            sections.append(
                "SAVED FACTS:\n" + "\n".join(facts[-35:])
            )
    except Exception as exc:
        log(f"Load facts warning: {exc}")

    try:
        memories = get_memories(chat_id)
        if memories:
            sections.append(
                "SAVED MEMORIES:\n" + "\n".join(memories[-20:])
            )
    except Exception as exc:
        log(f"Load memories warning: {exc}")

    return "\n\n".join(sections)


# ============================================================
# GEMINI AI AND GOOGLE SEARCH GROUNDING
# ============================================================

def extract_gemini_text(data):
    candidates = data.get("candidates") or []

    if not candidates:
        return ""

    parts = (
        (candidates[0].get("content") or {}).get("parts") or []
    )

    return "\n".join(
        part["text"]
        for part in parts
        if part.get("text")
    ).strip()


def response_has_grounding(data):
    for candidate in data.get("candidates") or []:
        metadata = candidate.get("groundingMetadata") or {}

        if (
            metadata.get("groundingChunks")
            or metadata.get("webSearchQueries")
        ):
            return True

    return False


def call_gemini(chat_id, user_text):
    global LAST_GEMINI_OK
    global LAST_WEB_SEARCH_USED
    global LAST_GEMINI_CHECK

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")

    contents = get_recent_messages(chat_id)

    # Current user message was already saved.
    if (
        contents
        and contents[-1]["role"] == "user"
        and contents[-1]["parts"][0]["text"] == user_text
    ):
        contents.pop()

    context = build_context(chat_id)
    system_text = SYSTEM_INSTRUCTIONS

    if context:
        system_text += (
            "\n\nSAVED USER CONTEXT "
            "(not independently verified):\n"
            + context
        )

    contents.append({
        "role": "user",
        "parts": [{"text": user_text}]
    })

    payload = {
        "system_instruction": {
            "parts": [{"text": system_text}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 4096
        }
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    LAST_WEB_SEARCH_USED = False

    if ENABLE_GOOGLE_SEARCH:
        search_payload = dict(payload)
        search_payload["tools"] = [{"google_search": {}}]

        try:
            response = requests.post(
                GEMINI_API,
                headers=headers,
                json=search_payload,
                timeout=HTTP_TIMEOUT
            )

            if response.ok:
                data = response.json()
                answer = extract_gemini_text(data)

                if answer:
                    LAST_GEMINI_OK = True
                    LAST_GEMINI_CHECK = now_utc().isoformat()
                    LAST_WEB_SEARCH_USED = response_has_grounding(data)
                    return answer

            else:
                log(
                    f"Gemini Search HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

        except Exception as exc:
            log(f"Gemini Search attempt failed: {exc}")

    response = requests.post(
        GEMINI_API,
        headers=headers,
        json=payload,
        timeout=HTTP_TIMEOUT
    )

    if not response.ok:
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: "
            f"{response.text[:1200]}"
        )

    data = response.json()
    answer = extract_gemini_text(data)

    if not answer:
        raise RuntimeError(
            "Gemini returned no text: " + safe_json(data)[:1000]
        )

    LAST_GEMINI_OK = True
    LAST_GEMINI_CHECK = now_utc().isoformat()
    LAST_WEB_SEARCH_USED = response_has_grounding(data)

    return answer


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_request(method, payload=None, timeout=35):
    if not TELEGRAM_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=timeout
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram {method} failed: {safe_json(data)[:1000]}"
        )

    return data.get("result")


def send_message(chat_id, text):
    text = str(text or "").strip()

    if not text:
        text = "პასუხის ტექსტი ვერ შეიქმნა. სცადე ხელახლა."

    chunks = [
        text[i:i + MAX_TELEGRAM_LENGTH]
        for i in range(0, len(text), MAX_TELEGRAM_LENGTH)
    ]

    for chunk in chunks:
        telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True
            }
        )

    return True


def send_document(chat_id, path, caption=""):
    with open(path, "rb") as handle:
        response = requests.post(
            f"{TELEGRAM_API}/sendDocument",
            data={
                "chat_id": str(chat_id),
                "caption": str(caption)[:900]
            },
            files={
                "document": (Path(path).name, handle)
            },
            timeout=HTTP_TIMEOUT
        )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"Telegram sendDocument HTTP {response.status_code}"
        )

    if not response.ok or not data.get("ok"):
        raise RuntimeError(
            f"Telegram sendDocument failed: {safe_json(data)[:1000]}"
        )

    return True


def get_bot_info():
    global BOT_USERNAME, BOT_ID

    result = telegram_request("getMe")
    BOT_USERNAME = result.get("username", "")
    BOT_ID = result.get("id")

    log(f"Connected to Telegram bot @{BOT_USERNAME}")


# ============================================================
# DOCUMENT GENERATION
# ============================================================

def create_document(file_type, title, body):
    file_type = file_type.lower().strip()

    safe_title = re.sub(
        r"[^a-zA-Z0-9ა-ჰ_-]+",
        "_",
        title
    ).strip("_")[:60] or "geniosa_document"

    temp_dir = tempfile.mkdtemp(prefix="geniosa_")
    path = os.path.join(
        temp_dir,
        f"{safe_title}.{file_type}"
    )

    lines = body.splitlines() or [body]

    if file_type == "docx":
        from docx import Document

        document = Document()
        document.add_heading(title, 0)

        for line in lines:
            document.add_paragraph(line)

        document.save(path)

    elif file_type == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Geniosa"

        sheet.append([title])
        sheet.append(["Content"])

        for line in lines:
            sheet.append([line])

        sheet.column_dimensions["A"].width = 100
        workbook.save(path)

    elif file_type == "pdf":
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer
        )
        from reportlab.lib.units import cm
        from xml.sax.saxutils import escape

        pdf = SimpleDocTemplate(
            path,
            pagesize=A4,
            rightMargin=2 * cm,
            leftMargin=2 * cm
        )

        styles = getSampleStyleSheet()
        story = [
            Paragraph(escape(title), styles["Title"]),
            Spacer(1, 12)
        ]

        for line in lines:
            story.append(
                Paragraph(escape(line) or " ", styles["BodyText"])
            )
            story.append(Spacer(1, 6))

        pdf.build(story)

    elif file_type == "pptx":
        from pptx import Presentation
        from pptx.util import Pt

        presentation = Presentation()

        first = presentation.slides.add_slide(
            presentation.slide_layouts[0]
        )
        first.shapes.title.text = title
        first.placeholders[1].text = "Prepared by GENIOSA 6.0"

        chunks = [
            lines[i:i + 8]
            for i in range(0, len(lines), 8)
        ] or [[""]]

        for index, chunk in enumerate(chunks, start=1):
            slide = presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
            slide.shapes.title.text = f"{title} — {index}"

            frame = slide.placeholders[1].text_frame
            frame.clear()

            for j, line in enumerate(chunk):
                paragraph = (
                    frame.paragraphs[0]
                    if j == 0 else frame.add_paragraph()
                )
                paragraph.text = line[:500]
                paragraph.font.size = Pt(18)

        presentation.save(path)

    else:
        raise ValueError(
            "ფორმატი უნდა იყოს docx, xlsx, pdf ან pptx"
        )

    return path


# ============================================================
# COMMANDS
# ============================================================

def handle_command(chat_id, text):
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command == "/start":
        send_message(
            chat_id,
            "გამარჯობა! მე ვარ GENIOSA 6.0.\n\n"
            "ბიზნესის, ფინანსების, პროექტებისა და ინვესტორების "
            "შესახებ კითხვებზე შემიძლია დაგეხმარო.\n\n"
            "/health — სისტემის სტატუსი\n"
            "/remember ტექსტი — ინფორმაციის შენახვა\n"
            "/memories — მეხსიერებების ნახვა\n"
            "/memory_test — მონაცემთა ბაზის ტესტი\n"
            "/conflicts_test — სატესტო ჩანაწერი\n"
            "/make_doc ფორმატი | სათაური | ტექსტი\n"
            "/help — დახმარება"
        )
        return True

    if command == "/help":
        send_message(
            chat_id,
            "კომანდები:\n"
            "/start\n/health\n/remember ტექსტი\n/memories\n"
            "/memory_test\n/conflicts_test\n"
            "/make_doc docx | სათაური | ტექსტი\n\n"
            "ფორმატები: docx, xlsx, pdf, pptx"
        )
        return True

    if command == "/health":
        data = {
            "version": APP_VERSION,
            "database": False,
            "telegram_polling": bool(
                polling_thread and polling_thread.is_alive()
            ),
            "gemini_last_request_ok": LAST_GEMINI_OK,
            "web_search_grounding_last_request": LAST_WEB_SEARCH_USED,
            "last_gemini_check": LAST_GEMINI_CHECK,
            "last_telegram_update": LAST_TELEGRAM_UPDATE,
            "last_error": LAST_ERROR or None
        }

        try:
            db_execute("SELECT 1")
            data["database"] = True
        except Exception as exc:
            data["database_error"] = str(exc)[:300]

        send_message(chat_id, safe_json(data))
        return True

    if command == "/remember":
        if not argument:
            send_message(
                chat_id,
                "გამოყენება: /remember ტექსტი"
            )
        elif save_memory(
            chat_id, argument, "user_saved", 8
        ):
            send_message(
                chat_id,
                "ინფორმაცია წარმატებით შეინახა PostgreSQL-ში."
            )
        else:
            send_message(
                chat_id,
                "შენახვა ვერ დადასტურდა. შეამოწმე Render Logs."
            )

        return True

    if command == "/memories":
        try:
            memories = get_memories(chat_id)

            if memories:
                answer = "შენახული მეხსიერებები:\n\n" + "\n\n".join(
                    f"{i + 1}. {value}"
                    for i, value in enumerate(memories[-15:])
                )
            else:
                answer = "შენახული მეხსიერებები ვერ მოიძებნა."

            send_message(chat_id, answer)

        except Exception as exc:
            record_error(f"/memories: {exc}")
            send_message(
                chat_id,
                "მეხსიერებების წაკითხვა ვერ მოხერხდა."
            )

        return True

    if command == "/memory_test":
        key = "GENIOSA_MEMORY_TEST_731"
        value = "BLUE_ORCHID_2026"

        try:
            save_fact(chat_id, key, value)
            success = any(
                row == f"{key}: {value}"
                for row in get_facts(chat_id)
            )

            send_message(
                chat_id,
                "PostgreSQL ჩაწერა/წაკითხვა დადასტურდა."
                if success
                else "ტესტი ვერ დადასტურდა. შეამოწმე Logs."
            )

        except Exception as exc:
            record_error(f"/memory_test: {exc}")
            send_message(
                chat_id,
                f"ტესტი ჩავარდა: {str(exc)[:300]}"
            )

        return True

    if command == "/conflicts_test":
        try:
            row = save_financial_conflict(
                chat_id,
                "GENIOSA_TEST_CONFLICT",
                "100",
                "200",
                "Automated test record; not a real financial conflict."
            )

            send_message(
                chat_id,
                f"სატესტო ჩანაწერი შეიქმნა. "
                f"ID: {row.get('id', 'saved')}"
            )

        except Exception as exc:
            record_error(f"/conflicts_test: {exc}")
            send_message(
                chat_id,
                f"ტესტი ჩავარდა: {str(exc)[:300]}"
            )

        return True

    if command == "/make_doc":
        fields = [
            item.strip()
            for item in argument.split("|", 2)
        ]

        if len(fields) != 3:
            send_message(
                chat_id,
                "გამოყენება:\n"
                "/make_doc docx | სათაური | ტექსტი"
            )
            return True

        file_type, title, body = fields

        try:
            path = create_document(file_type, title, body)

            send_document(
                chat_id,
                path,
                f"შექმნილია GENIOSA 6.0-ის მიერ: {title}"
            )

            save_message(
                chat_id,
                "assistant",
                f"დოკუმენტი შეიქმნა და გაიგზავნა: {Path(path).name}"
            )

        except Exception as exc:
            record_error(f"/make_doc: {type(exc).__name__}: {exc}")
            send_message(
                chat_id,
                "დოკუმენტის შექმნა ვერ მოხერხდა. "
                "შეამოწმე requirements.txt და Render Logs."
            )

        return True

    return False


# ============================================================
# PROCESS USER MESSAGE
# ============================================================

def process_message(message):
    chat_id = parse_chat_id(
        (message.get("chat") or {}).get("id")
    )
    text_value = (message.get("text") or "").strip()

    if chat_id is None or not text_value:
        return

    if not is_owner(chat_id):
        send_message(
            chat_id,
            "წვდომა შეზღუდულია. შეამოწმე GENIOSA_OWNER_ID."
        )
        return

    if text_value.startswith("/"):
        try:
            if handle_command(chat_id, text_value):
                return
        except Exception as exc:
            record_error(f"Command handler: {exc}")

            try:
                send_message(
                    chat_id,
                    "კომანდის შესრულებისას შეცდომა მოხდა. "
                    "შეამოწმე Render Logs."
                )
            except Exception:
                pass

            return

    save_message(chat_id, "user", text_value)

    match = re.match(
        r"^(დაიმახსოვრე|შეინახე მეხსიერებაში)"
        r"\s*[:,-]?\s*(.+)$",
        text_value,
        flags=re.IGNORECASE | re.DOTALL
    )

    if match:
        content = match.group(2).strip()
        success = save_memory(
            chat_id, content, "user_saved", 8
        )

        answer = (
            "ინფორმაცია წარმატებით შევინახე მეხსიერებაში."
            if success
            else "შენახვა ვერ დადასტურდა. შეამოწმე Render Logs."
        )

        save_message(chat_id, "assistant", answer)
        send_message(chat_id, answer)
        return

    try:
        answer = call_gemini(chat_id, text_value)

        save_message(chat_id, "assistant", answer)
        send_message(chat_id, answer)

    except Exception as exc:
        record_error(
            f"AI response: {type(exc).__name__}: {exc}"
        )

        answer = (
            "პასუხის გენერირებისას ტექნიკური შეცდომა მოხდა. "
            "შეამოწმე Render Logs და Gemini API-ის სტატუსი."
        )

        save_message(chat_id, "assistant", answer)

        try:
            send_message(chat_id, answer)
        except Exception as send_exc:
            record_error(f"Failed to send error reply: {send_exc}")


# ============================================================
# TELEGRAM POLLING — POSTGRESQL ADVISORY LOCK
# ============================================================

def acquire_polling_lock():
    conn = db_connect()
    conn.autocommit = True

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
            (POLL_LOCK_KEY_1, POLL_LOCK_KEY_2)
        )
        result = cursor.fetchone()

    if not result or not result["acquired"]:
        conn.close()
        log("Another instance owns the Telegram polling lock.")
        return None

    return conn


def polling_loop():
    global LAST_TELEGRAM_UPDATE

    lock_conn = None

    try:
        lock_conn = acquire_polling_lock()

        if lock_conn is None:
            return

        try:
            telegram_request(
                "deleteWebhook",
                {"drop_pending_updates": False}
            )
        except Exception as exc:
            log(f"deleteWebhook warning: {exc}")

        offset = 0

        while not stop_event.is_set():
            try:
                updates = telegram_request(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": ["message"]
                    },
                    timeout=POLL_TIMEOUT + 10
                )

                for update in updates or []:
                    update_id = update.get("update_id")

                    if update_id is not None:
                        offset = update_id + 1
                        LAST_TELEGRAM_UPDATE = update_id

                    message = update.get("message")

                    if message:
                        try:
                            process_message(message)
                        except Exception as exc:
                            record_error(
                                f"process_message: "
                                f"{type(exc).__name__}: {exc}"
                            )

            except Exception as exc:
                record_error(
                    f"Telegram polling: {type(exc).__name__}: {exc}"
                )
                stop_event.wait(5)

    except Exception as exc:
        record_error(
            f"Polling lock/start failed: {type(exc).__name__}: {exc}"
        )

    finally:
        if lock_conn is not None:
            try:
                with lock_conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(%s, %s)",
                        (POLL_LOCK_KEY_1, POLL_LOCK_KEY_2)
                    )
            except Exception:
                pass

            try:
                lock_conn.close()
            except Exception:
                pass


def start_polling():
    global polling_thread

    with polling_lock:
        if polling_thread and polling_thread.is_alive():
            return

        stop_event.clear()

        polling_thread = threading.Thread(
            target=polling_loop,
            name="geniosa-telegram-polling",
            daemon=True
        )

        polling_thread.start()
        log("Telegram polling thread started")


# ============================================================
# FASTAPI LIFECYCLE
# ============================================================

@app.on_event("startup")
def on_startup():
    log(f"Starting {APP_VERSION}")

    try:
        migrate_database()
        db_execute("SELECT 1")
        log("PostgreSQL connection verified")

    except Exception as exc:
        record_error(
            f"Startup database check: {type(exc).__name__}: {exc}"
        )

    if TELEGRAM_BOT_TOKEN:
        try:
            get_bot_info()
            start_polling()

        except Exception as exc:
            record_error(
                f"Telegram startup: {type(exc).__name__}: {exc}"
            )
    else:
        record_error("TELEGRAM_BOT_TOKEN is missing")


@app.on_event("shutdown")
def on_shutdown():
    stop_event.set()
    log("Shutdown requested")


@app.get("/")
def home():
    return {
        "service": APP_VERSION,
        "status": "running",
        "telegram_bot": BOT_USERNAME or "not verified",
        "database_configured": bool(DATABASE_URL),
        "gemini_configured": bool(GEMINI_API_KEY),
        "message": "GENIOSA service is running"
    }


@app.get("/health")
def health():
    database_ok = False
    database_error = None

    try:
        db_execute("SELECT 1")
        database_ok = True
    except Exception as exc:
        database_error = str(exc)[:500]

    polling_ok = bool(
        polling_thread and polling_thread.is_alive()
    )

    return JSONResponse({
        "version": APP_VERSION,
        "status": (
            "healthy" if database_ok and polling_ok
            else "degraded"
        ),
        "database_ok": database_ok,
        "database_error": database_error,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
        "telegram_bot": BOT_USERNAME or None,
        "telegram_polling_thread_alive": polling_ok,
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_last_request_ok": LAST_GEMINI_OK,
        "gemini_last_check": LAST_GEMINI_CHECK,
        "google_search_enabled": ENABLE_GOOGLE_SEARCH,
        "web_search_grounding_last_request": LAST_WEB_SEARCH_USED,
        "last_telegram_update": LAST_TELEGRAM_UPDATE,
        "last_error": LAST_ERROR or None
    })


@app.get("/version")
def version():
    return {
        "version": APP_VERSION,
        "database_migration": "additive_only",
        "legacy_memory_category_fix": True,
        "postgres_advisory_polling_lock": True,
        "document_generation": ["docx", "xlsx", "pdf", "pptx"]
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )
