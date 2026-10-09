
# ============================================================
# GENIOSA 6.0
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + FastAPI
#
# Features:
# - Persistent PostgreSQL storage
# - Additive-only schema migration
# - Legacy database compatibility
# - Fix for required memories.category column
# - Conversation history
# - Facts, memories, projects, decisions, tasks
# - Financial conflict records
# - Gemini AI responses
# - Optional Google Search grounding
# - Telegram polling
# - Health endpoint
#
# IMPORTANT:
# Existing PostgreSQL records are preserved.
# No DROP TABLE, TRUNCATE, or destructive migration.
# ============================================================

import os
import re
import json
import time
import threading
import traceback
from datetime import datetime, timezone

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.responses import JSONResponse


# ============================================================
# CONFIGURATION
# ============================================================

APP_VERSION = "GENIOSA 6.0"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

GENIOSA_OWNER_ID = os.getenv(
    "GENIOSA_OWNER_ID",
    ""
).strip()

ENABLE_GOOGLE_SEARCH = os.getenv(
    "ENABLE_GOOGLE_SEARCH",
    "true"
).lower() in ("1", "true", "yes", "on")

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN else ""
)

GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

HTTP_TIMEOUT = 45
TELEGRAM_POLL_TIMEOUT = 25
MAX_TELEGRAM_LENGTH = 3900

app = FastAPI(
    title=APP_VERSION,
    description="Personal Business Advisor",
    version="6.0"
)

stop_event = threading.Event()
polling_thread = None
polling_lock = threading.Lock()

LAST_ERROR = ""
LAST_GEMINI_OK = False
LAST_WEB_SEARCH_USED = False
LAST_GEMINI_CHECK = None
LAST_TELEGRAM_UPDATE = None
BOT_USERNAME = ""
BOT_ID = None


# ============================================================
# SYSTEM INSTRUCTIONS
# ============================================================

SYSTEM_INSTRUCTIONS = """
You are GENIOSA 6.0, a personal business advisor and economic
analyst for the owner of SAMTISI CONSTRUCTION LLC.

CORE RULES:

1. Be accurate, factual, and transparent.
2. Never invent facts, financial results, sources, legal rules,
   investor commitments, market prices, or completed actions.
3. Clearly distinguish:
   - facts supplied by the user;
   - information verified using external sources;
   - estimates and assumptions;
   - unknown or unverified information.
4. If you do not know something, say so directly.
5. Never present an estimate as a confirmed fact.
6. Check financial calculations carefully and show the formula
   when it helps the user.
7. For development projects, consider construction costs,
   financing, taxes, timelines, cash flow, sales, rental income,
   risk, and investor returns when relevant.
8. Do not claim to have saved a memory unless the application
   confirms that the database write succeeded.
9. Do not claim that a web search occurred unless search was
   actually used successfully and source information is available.
10. For legal, tax, regulatory, or investment matters, explain
    uncertainty and recommend verification against current
    authoritative sources where appropriate.
11. User-provided project figures are not automatically verified
    market facts.
12. Reply in the user's language. If the user writes in Georgian,
    answer in Georgian.
13. Preserve the distinction between project facts, decisions,
    tasks, and tentative proposals.
14. Do not disclose API keys, passwords, or environment variables.
15. Never claim you executed an external action unless it actually
    succeeded.
"""


# ============================================================
# GENERAL UTILITIES
# ============================================================

def log(message):
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"[{APP_VERSION} {timestamp}] {message}", flush=True)


def record_error(message):
    global LAST_ERROR
    LAST_ERROR = str(message)[:3000]
    log(f"ERROR: {LAST_ERROR}")


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
    if not GENIOSA_OWNER_ID:
        return True

    return str(chat_id) == GENIOSA_OWNER_ID


# ============================================================
# DATABASE CONNECTION
# ============================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing"
        )

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15,
        cursor_factory=RealDictCursor
    )


def db_execute(query, params=None, fetchone=False, fetchall=False):
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
    """
    Return metadata for a table that exists in the current schema.

    Metadata is used to support older database schemas without
    assuming every installation has identical column names.
    """
    rows = db_execute(
        """
        SELECT
            column_name,
            is_nullable,
            column_default,
            data_type,
            udt_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
        fetchall=True
    )

    return {row["column_name"]: row for row in rows}


def table_exists(table_name):
    row = db_execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = %s
        ) AS present
        """,
        (table_name,),
        fetchone=True
    )

    return bool(row and row["present"])


# ============================================================
# ADDITIVE DATABASE MIGRATION
# ============================================================

def migrate_database():
    """
    Creates missing tables and adds missing columns only.
    It does not delete or truncate existing records.
    """

    statements = [
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

    for statement in statements:
        db_execute(statement)

    # Additive migrations for older tables.
    # IF NOT EXISTS ensures existing columns are not overwritten.

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
        for column_name, column_type in columns.items():
            # Names/types here come from this static dictionary,
            # not from user input.
            db_execute(
                f"""
                ALTER TABLE "{table_name}"
                ADD COLUMN IF NOT EXISTS "{column_name}" {column_type}
                """
            )

    log("Schema migration completed (additive only)")


# ============================================================
# LEGACY-COMPATIBLE INSERT
# ============================================================

def infer_required_value(column_name, values, table_name):
    """
    Supplies a safe value for known legacy required columns.
    Unknown required columns are reported rather than silently
    inserting fabricated business data.
    """

    name = column_name.lower()

    aliases = {
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

    for candidate in aliases.get(name, ()):
        if candidate in values and values[candidate] is not None:
            return values[candidate]

    if name in ("created_at", "updated_at", "timestamp", "date"):
        return now_utc()

    if name in ("category", "memory_type", "type"):
        return values.get("memory_type", "general")

    if name == "importance":
        return 5

    if name == "role":
        return values.get("role", "system")

    if name == "status":
        return "recorded"

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

    # Never invent a value for an unknown required legacy field.
    return None


def dynamic_insert(table_name, values):
    """
    Insert values using only columns that actually exist.

    Required legacy columns are detected from PostgreSQL metadata.
    The memories.category issue is explicitly supported.
    """

    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
        raise ValueError("Invalid table name")

    metadata = get_table_columns(table_name)

    if not metadata:
        raise RuntimeError(
            f"Table '{table_name}' does not exist or has no visible columns"
        )

    payload = {}

    # Keep only supplied columns that exist in the actual table.
    for key, value in values.items():
        if key in metadata and value is not None:
            payload[key] = value

    # Compatibility fix for the confirmed GENIOSA 5.9 error.
    if table_name == "memories" and "category" in metadata:
        payload.setdefault(
            "category",
            values.get("memory_type") or values.get("category") or "general"
        )

    # Populate all known non-nullable legacy columns that lack defaults.
    for column_name, info in metadata.items():
        if column_name in payload:
            continue

        if info["is_nullable"] == "YES":
            continue

        if info["column_default"] is not None:
            continue

        value = infer_required_value(
            column_name,
            {**values, **payload},
            table_name
        )

        if value is not None:
            payload[column_name] = value
            continue

        # Serial/identity columns should normally have defaults.
        if column_name.lower() in ("id", "pk"):
            continue

        raise RuntimeError(
            f"Required legacy column {table_name}.{column_name} "
            "has no supplied value"
        )

    if not payload:
        raise RuntimeError(
            f"No compatible values supplied for table '{table_name}'"
        )

    columns = list(payload.keys())

    for col in columns:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", col):
            raise ValueError("Invalid column name")

    quoted_columns = ", ".join(f'"{col}"' for col in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    params = [payload[col] for col in columns]

    query = (
        f'INSERT INTO "{table_name}" ({quoted_columns}) '
        f'VALUES ({placeholders}) RETURNING *'
    )

    return db_execute(query, params, fetchone=True)


# ============================================================
# CONVERSATION HISTORY
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
                "role": role,
                "message": content,
                "text": content,
                "content": content,
                "created_at": now_utc(),
            }
        )
        return True

    except Exception as exc:
        record_error(f"save_message: {type(exc).__name__}: {exc}")
        return False


def get_recent_messages(chat_id, limit=12):
    metadata = get_table_columns("messages")

    if "chat_id" not in metadata:
        return []

    content_column = next(
        (
            name for name in ("message", "text", "content")
            if name in metadata
        ),
        None
    )

    if not content_column or "role" not in metadata:
        return []

    order_column = (
        "created_at" if "created_at" in metadata
        else "id" if "id" in metadata
        else None
    )

    order_sql = f'"{order_column}" DESC' if order_column else "1 DESC"

    rows = db_execute(
        f"""
        SELECT role, "{content_column}" AS content
        FROM messages
        WHERE chat_id = %s
        ORDER BY {order_sql}
        LIMIT %s
        """,
        (chat_id, limit),
        fetchall=True
    )

    rows.reverse()

    return [
        {
            "role": (
                "model" if row.get("role") in ("assistant", "model")
                else "user" if row.get("role") == "user"
                else "model"
            ),
            "parts": [{"text": str(row.get("content") or "")}]
        }
        for row in rows
        if row.get("content")
    ]


# ============================================================
# PERSISTENT MEMORIES
# ============================================================

def save_memory(
    chat_id,
    content,
    memory_type="general",
    importance=5
):
    """
    Saves memory and explicitly supplies category for legacy schemas.
    Returns True only if the memory insert succeeded.
    """

    if not content or not str(content).strip():
        return False

    content = str(content).strip()
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
                "importance": max(1, min(10, int(importance))),
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
        except Exception as event_exc:
            # The memory itself was saved. An event-log failure must
            # not falsely report that the memory insert failed.
            log(f"memory_events warning: {event_exc}")

        log(f"Memory saved successfully for chat_id={chat_id}")
        return True

    except Exception as exc:
        record_error(
            f"save_memory: {type(exc).__name__}: {exc}"
        )
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

    order_sql = f'"{order_column}" DESC' if order_column else "1 DESC"

    rows = db_execute(
        f"""
        SELECT "{content_column}" AS content
        FROM memories
        WHERE chat_id = %s
        ORDER BY {order_sql}
        LIMIT %s
        """,
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

    rows = db_execute(
        """
        SELECT *
        FROM facts
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
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


# ============================================================
# PROJECTS, DECISIONS, TASKS
# ============================================================

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


# ============================================================
# FINANCIAL CONFLICT RECORDS
# ============================================================

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


# ============================================================
# GEMINI AI
# ============================================================

def build_context(chat_id):
    sections = []

    try:
        facts = get_facts(chat_id)
        if facts:
            sections.append(
                "SAVED FACTS:\n" + "\n".join(facts[-40:])
            )
    except Exception as exc:
        log(f"Could not load facts: {exc}")

    try:
        memories = get_memories(chat_id)
        if memories:
            sections.append(
                "SAVED MEMORIES:\n" + "\n".join(memories[-20:])
            )
    except Exception as exc:
        log(f"Could not load memories: {exc}")

    return "\n\n".join(sections)


def call_gemini(chat_id, user_text):
    global LAST_GEMINI_OK, LAST_WEB_SEARCH_USED
    global LAST_GEMINI_CHECK

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")

    contents = get_recent_messages(chat_id)

    # The current message is saved before the AI call.
    # Avoid duplicating it in the conversation history.
    while contents and contents[-1]["parts"][0]["text"] == user_text:
        contents.pop()

    context = build_context(chat_id)

    system_text = SYSTEM_INSTRUCTIONS

    if context:
        system_text += (
            "\n\nRELEVANT STORED INFORMATION:\n"
            + context
            + "\n\nUse these records as user-provided context, "
              "not automatically as independently verified facts."
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

    # Try Google Search grounding first if enabled.
    # If the model/API rejects the tool, retry without it.
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

            log(
                "Gemini Search attempt did not succeed; "
                "retrying without Search."
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
            f"{response.text[:1500]}"
        )

    data = response.json()
    answer = extract_gemini_text(data)

    if not answer:
        raise RuntimeError(
            "Gemini returned no text: " + safe_json(data)[:1200]
        )

    LAST_GEMINI_OK = True
    LAST_GEMINI_CHECK = now_utc().isoformat()
    LAST_WEB_SEARCH_USED = response_has_grounding(data)

    return answer


def extract_gemini_text(data):
    candidates = data.get("candidates") or []

    if not candidates:
        return ""

    candidate = candidates[0]
    content = candidate.get("content") or {}
    parts = content.get("parts") or []

    chunks = []

    for part in parts:
        text_value = part.get("text")
        if text_value:
            chunks.append(text_value)

    return "\n".join(chunks).strip()


def response_has_grounding(data):
    for candidate in data.get("candidates") or []:
        metadata = candidate.get("groundingMetadata") or {}
        if (
            metadata.get("groundingChunks")
            or metadata.get("webSearchQueries")
        ):
            return True

    return False


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

    # Telegram messages have a maximum length.
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


def get_bot_info():
    global BOT_USERNAME, BOT_ID

    result = telegram_request("getMe")
    BOT_USERNAME = result.get("username", "")
    BOT_ID = result.get("id")

    log(f"Connected to Telegram bot @{BOT_USERNAME}")


# ============================================================
# BOT COMMANDS
# ============================================================

def handle_command(chat_id, text):
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command == "/start":
        send_message(
            chat_id,
            "გამარჯობა! მე ვარ GENIOSA 6.0 — შენი პირადი "
            "ბიზნეს-მრჩეველი.\n\n"
            "შემიძლია დაგეხმარო ფინანსურ ანალიზში, პროექტების "
            "დაგეგმვაში, ინვესტორებთან მუშაობასა და გადაწყვეტილებების "
            "შეფასებაში.\n\n"
            "კომანდები:\n"
            "/health — სისტემის სტატუსი\n"
            "/remember ტექსტი — მეხსიერებაში შენახვა\n"
            "/memories — შენახული მეხსიერებების ნახვა\n"
            "/memory_test — მონაცემთა ბაზის მეხსიერების ტესტი\n"
            "/conflicts_test — ფინანსური კონფლიქტის ტესტი\n"
            "/help — დახმარება"
        )
        return True

    if command == "/help":
        send_message(
            chat_id,
            "GENIOSA 6.0 კომანდები:\n"
            "/start\n"
            "/health\n"
            "/remember ტექსტი\n"
            "/memories\n"
            "/memory_test\n"
            "/conflicts_test\n\n"
            "ჩვეულებრივად მომწერე კითხვა ან დავალება."
        )
        return True

    if command == "/health":
        health = {
            "version": APP_VERSION,
            "database": False,
            "telegram_polling": polling_thread is not None
                and polling_thread.is_alive(),
            "gemini_last_request_ok": LAST_GEMINI_OK,
            "web_search_grounding_last_request": LAST_WEB_SEARCH_USED,
            "last_gemini_check": LAST_GEMINI_CHECK,
            "last_error": LAST_ERROR or "არ არის",
        }

        try:
            db_execute("SELECT 1")
            health["database"] = True
        except Exception as exc:
            health["database_error"] = str(exc)[:300]

        send_message(chat_id, safe_json(health))
        return True

    if command == "/remember":
        if not argument:
            send_message(
                chat_id,
                "გამოყენება: /remember ტექსტი\n"
                "მაგალითი: /remember SAMTISI-ს პროექტის "
                "მიმდინარე ვადაა 30 თვე."
            )
            return True

        if save_memory(chat_id, argument, "user_saved", 8):
            send_message(
                chat_id,
                "✅ ინფორმაცია წარმატებით შეინახა PostgreSQL-ში."
            )
        else:
            send_message(
                chat_id,
                "❌ ინფორმაციის შენახვა ვერ დადასტურდა. "
                "შეამოწმე Render Logs."
            )

        return True

    if command == "/memories":
        try:
            memories = get_memories(chat_id)

            if not memories:
                send_message(chat_id, "შენახული მეხსიერებები ვერ მოიძებნა.")
            else:
                send_message(
                    chat_id,
                    "შენახული მეხსიერებები:\n\n"
                    + "\n\n".join(
                        f"{i + 1}. {item}"
                        for i, item in enumerate(memories[-15:])
                    )
                )
        except Exception as exc:
            record_error(f"/memories: {exc}")
            send_message(chat_id, "მეხსიერებების წაკითხვა ვერ მოხერხდა.")

        return True

    if command == "/memory_test":
        key = "GENIOSA_MEMORY_TEST_731"
        value = "BLUE_ORCHID_2026"

        try:
            # Use a unique key per chat, without overwriting another
            # chat's test record.
            save_fact(chat_id, key, value)

            rows = get_facts(chat_id)
            success = any(
                row == f"{key}: {value}"
                for row in rows
            )

            if success:
                send_message(
                    chat_id,
                    "✅ PostgreSQL memory test successful: "
                    "ჩაწერა და ხელახლა წაკითხვა დადასტურდა."
                )
            else:
                send_message(
                    chat_id,
                    "❌ ფაქტის ჩაწერა ან ხელახლა წაკითხვა ვერ "
                    "დადასტურდა. შეამოწმე Logs."
                )

        except Exception as exc:
            record_error(f"/memory_test: {exc}")
            send_message(chat_id, f"❌ მეხსიერების ტესტი ჩავარდა: {exc}")

        return True

    if command == "/conflicts_test":
        try:
            row = save_financial_conflict(
                chat_id=chat_id,
                subject="GENIOSA_TEST_CONFLICT",
                old_value="100",
                new_value="200",
                details="Automated test record; not a real financial conflict."
            )

            send_message(
                chat_id,
                "✅ Financial conflict test successful; "
                f"record ID {row.get('id', 'saved')}. "
                "ეს სატესტო ჩანაწერია."
            )

        except Exception as exc:
            record_error(f"/conflicts_test: {exc}")
            send_message(
                chat_id,
                f"❌ ფინანსური კონფლიქტის ტესტი ჩავარდა: {exc}"
            )

        return True

    return False


# ============================================================
# MESSAGE PROCESSING
# ============================================================

def process_message(message):
    chat = message.get("chat") or {}
    chat_id = parse_chat_id(chat.get("id"))
    text_value = (message.get("text") or "").strip()

    if chat_id is None or not text_value:
        return

    user = message.get("from") or {}

    # Commands that do not require AI are processed separately.
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

    # Save the user's message before generating an answer.
    save_message(chat_id, "user", text_value)

    # Optional direct memory-saving phrasing.
    memory_match = re.match(
        r"^(დაიმახსოვრე|შეინახე მეხსიერებაში)\s*[:,-]?\s*(.+)$",
        text_value,
        flags=re.IGNORECASE | re.DOTALL
    )

    if memory_match:
        memory_content = memory_match.group(2).strip()

        if save_memory(chat_id, memory_content, "user_saved", 8):
            answer = "✅ ინფორმაცია წარმატებით შევინახე მეხსიერებაში."
        else:
            answer = (
                "❌ მეხსიერების შენახვა ვერ დადასტურდა. "
                "შეამოწმე Render Logs."
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
            "ბოდიში, პასუხის გენერირებისას ტექნიკური შეცდომა მოხდა. "
            "შეამოწმე Render Logs და API-ის სტატუსი."
        )

        save_message(chat_id, "assistant", answer)

        try:
            send_message(chat_id, answer)
        except Exception as telegram_exc:
            record_error(f"Failed to send error reply: {telegram_exc}")


# ============================================================
# TELEGRAM LONG POLLING
# ============================================================

def polling_loop():
    global LAST_TELEGRAM_UPDATE

    offset = 0

    # Continue after temporary errors; do not terminate the service.
    while not stop_event.is_set():
        try:
            updates = telegram_request(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": TELEGRAM_POLL_TIMEOUT,
                    "allowed_updates": ["message"]
                },
                timeout=TELEGRAM_POLL_TIMEOUT + 10
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


def start_polling():
    global polling_thread

    with polling_lock:
        if polling_thread is not None and polling_thread.is_alive():
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
    db_ok = False
    db_error = None

    try:
        db_execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        db_error = str(exc)[:500]

    polling_ok = (
        polling_thread is not None
        and polling_thread.is_alive()
    )

    return JSONResponse({
        "version": APP_VERSION,
        "status": (
            "healthy"
            if db_ok and polling_ok
            else "degraded"
        ),
        "database_ok": db_ok,
        "database_error": db_error,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
        "telegram_bot": BOT_USERNAME or None,
        "telegram_polling": polling_ok,
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
        "legacy_memory_category_fix": True
    }


# ============================================================
# LOCAL ENTRY POINT
# Render may use its configured start command instead.
# Example: uvicorn app:app --host 0.0.0.0 --port $PORT
# ============================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )

