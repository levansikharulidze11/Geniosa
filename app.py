# ============================================================
# GENIOSA 5.4
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL
#
# IMPORTANT:
# - Existing PostgreSQL data is preserved.
# - NO DROP
# - NO TRUNCATE
# - NO mass DELETE
# - Additive database migrations only.
# - Legacy schemas are supported where possible.
# ============================================================

import os
import re
import json
import time
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

import requests
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.responses import JSONResponse


# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "5.4"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GENIOSA_OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

# Fallbacks only if configured model is unavailable.
GEMINI_FALLBACK_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

DB_MIN_CONNECTIONS = 1
DB_MAX_CONNECTIONS = 5

HISTORY_LIMIT = 24
HISTORY_CHAR_LIMIT = 12000
CONTEXT_CHAR_LIMIT = 18000

TELEGRAM_TIMEOUT = 35
GEMINI_TIMEOUT = 90

SEED_VERSION = "5.4-v1"

POLL_INTERVAL = 1.0

POLLING_STOP_EVENT = threading.Event()
POLLING_THREAD = None
POLLING_RUNNING = False
POLLING_LOCK_CONNECTION = None
POLLING_LOCK_ACQUIRED = False
LAST_UPDATE_ID = None

DB_POOL = None
DB_READY = False
STARTUP_COMPLETE = False


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="GENIOSA",
    version=APP_VERSION
)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).isoformat()
    print(f"[GENIOSA {now}] {message}", flush=True)


def log_error(prefix, exc):
    log(f"{prefix}: {type(exc).__name__}: {exc}")
    traceback.print_exc()


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


# ============================================================
# DATABASE CONNECTION POOL
# ============================================================

def initialize_db_pool():
    global DB_POOL

    if not DATABASE_URL:
        log("DATABASE_URL is not configured")
        return False

    try:
        DB_POOL = pool.ThreadedConnectionPool(
            DB_MIN_CONNECTIONS,
            DB_MAX_CONNECTIONS,
            DATABASE_URL
        )

        log("DATABASE CONNECTION POOL READY")
        return True

    except Exception as exc:
        log_error("DATABASE POOL ERROR", exc)
        DB_POOL = None
        return False


@contextmanager
def db_connection():
    """
    Safe PostgreSQL connection handling.

    Every acquired connection is ALWAYS returned to the pool.
    This prevents the 5.2 connection-pool exhaustion problem.
    """
    global DB_POOL

    if DB_POOL is None:
        raise RuntimeError("Database pool is not initialized")

    conn = None

    try:
        conn = DB_POOL.getconn()

        if conn.closed:
            try:
                DB_POOL.putconn(conn, close=True)
            except Exception:
                pass

            conn = DB_POOL.getconn()

        yield conn

    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise

    finally:
        if conn is not None:
            try:
                if not conn.closed:
                    conn.rollback()
            except Exception:
                pass

            try:
                DB_POOL.putconn(conn)
            except Exception as exc:
                log_error("DB PUT CONNECTION ERROR", exc)


def db_execute(
    query,
    params=None,
    fetch=False,
    fetchone=False,
    commit=True
):
    with db_connection() as conn:
        cursor_factory = RealDictCursor if (fetch or fetchone) else None

        with conn.cursor(cursor_factory=cursor_factory) as cur:
            cur.execute(query, params or ())

            result = None

            if fetchone:
                result = cur.fetchone()

            elif fetch:
                result = cur.fetchall()

            if commit:
                conn.commit()

            return result


def database_ping():
    try:
        row = db_execute(
            "SELECT 1 AS ok",
            fetchone=True,
            commit=False
        )

        return bool(row and row["ok"] == 1)

    except Exception as exc:
        log_error("DATABASE PING ERROR", exc)
        return False


# ============================================================
# SQL HELPERS
# ============================================================

def quote_identifier(name):
    return '"' + str(name).replace('"', '""') + '"'


def table_exists(table_name):
    row = db_execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        ) AS exists
        """,
        (table_name,),
        fetchone=True,
        commit=False
    )

    return bool(row and row["exists"])


def get_columns(table_name):
    rows = db_execute(
        """
        SELECT
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
        fetch=True,
        commit=False
    )

    return {
        row["column_name"]: row
        for row in rows
    }


def column_exists(table_name, column_name):
    columns = get_columns(table_name)
    return column_name in columns


def add_column(table_name, column_name, definition):
    if column_exists(table_name, column_name):
        return False

    query = (
        f"ALTER TABLE {quote_identifier(table_name)} "
        f"ADD COLUMN {quote_identifier(column_name)} {definition}"
    )

    db_execute(query)
    log(f"ADDED COLUMN {table_name}.{column_name}")

    return True


def ensure_table(table_name, create_sql, columns):
    if not table_exists(table_name):
        db_execute(create_sql)
        log(f"CREATED TABLE {table_name}")

    for column_name, definition in columns.items():
        try:
            add_column(
                table_name,
                column_name,
                definition
            )
        except Exception as exc:
            log_error(
                f"ADD COLUMN ERROR {table_name}.{column_name}",
                exc
            )


# ============================================================
# DATABASE MIGRATIONS
# ============================================================

def migrate_messages():
    ensure_table(
        "messages",
        """
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            role TEXT,
            message TEXT,
            text TEXT,
            content TEXT,
            body TEXT,
            message_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "role": "TEXT",
            "message": "TEXT",
            "text": "TEXT",
            "content": "TEXT",
            "body": "TEXT",
            "message_text": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_memories():
    ensure_table(
        "memories",
        """
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            memory_key TEXT,
            content TEXT,
            memory TEXT,
            text TEXT,
            body TEXT,
            memory_type TEXT,
            source TEXT,
            importance TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "memory_key": "TEXT",
            "content": "TEXT",
            "memory": "TEXT",
            "text": "TEXT",
            "body": "TEXT",
            "memory_type": "TEXT",
            "source": "TEXT",
            "importance": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_memory_events():
    ensure_table(
        "memory_events",
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            memory_key TEXT,
            action TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "memory_key": "TEXT",
            "action": "TEXT",
            "content": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_facts():
    ensure_table(
        "facts",
        """
        CREATE TABLE IF NOT EXISTS facts (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            fact_key TEXT,
            fact_value TEXT,
            content TEXT,
            fact TEXT,
            value TEXT,
            text TEXT,
            body TEXT,
            source TEXT,
            confidence TEXT,
            status TEXT,
            category TEXT,
            memory_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "fact_key": "TEXT",
            "fact_value": "TEXT",
            "content": "TEXT",
            "fact": "TEXT",
            "value": "TEXT",
            "text": "TEXT",
            "body": "TEXT",
            "source": "TEXT",
            "confidence": "TEXT",
            "status": "TEXT",
            "category": "TEXT",
            "memory_type": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )

    migrate_legacy_fact_keys()
    synchronize_fact_values()


def migrate_legacy_fact_keys():
    """
    IMPORTANT:
    Never assumes facts.id exists.

    PostgreSQL ctid is used only to identify rows during this
    migration. Existing values are preserved.
    """

    try:
        columns = get_columns("facts")

        if "fact_key" not in columns:
            return

        if "chat_id" not in columns:
            return

        query = """
        WITH numbered AS (
            SELECT
                ctid,
                chat_id,
                'legacy_fact_' ||
                COALESCE(chat_id::TEXT, '0') ||
                '_' ||
                ROW_NUMBER() OVER (
                    PARTITION BY chat_id
                    ORDER BY ctid
                )::TEXT AS generated_key
            FROM facts
            WHERE fact_key IS NULL
        )
        UPDATE facts f
        SET fact_key = numbered.generated_key
        FROM numbered
        WHERE f.ctid = numbered.ctid
          AND f.fact_key IS NULL
        """

        db_execute(query)
        log("FACT_KEY LEGACY MIGRATION COMPLETE")

    except Exception as exc:
        log_error("FACT KEY MIGRATION ERROR", exc)


def synchronize_fact_values():
    """
    Legacy facts may have the actual value stored in content/fact/value/text
    while fact_value is NOT NULL.

    This fills missing fact_value without deleting or overwriting
    existing fact_value.
    """

    try:
        columns = get_columns("facts")

        if "fact_value" not in columns:
            return

        candidates = []

        for column in (
            "content",
            "fact",
            "value",
            "text",
            "body"
        ):
            if column in columns:
                candidates.append(
                    f"NULLIF(TRIM({quote_identifier(column)}), '')"
                )

        if not candidates:
            return

        expression = "COALESCE(" + ", ".join(candidates) + ")"

        query = f"""
        UPDATE facts
        SET fact_value = {expression}
        WHERE fact_value IS NULL
        """

        db_execute(query)

        log("FACT_VALUE LEGACY SYNCHRONIZATION COMPLETE")

    except Exception as exc:
        log_error("FACT VALUE SYNCHRONIZATION ERROR", exc)


def migrate_fact_history():
    ensure_table(
        "fact_history",
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            fact_key TEXT,
            old_value TEXT,
            new_value TEXT,
            action TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "fact_key": "TEXT",
            "old_value": "TEXT",
            "new_value": "TEXT",
            "action": "TEXT",
            "source": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_decisions():
    ensure_table(
        "decisions",
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            content TEXT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "title": "TEXT",
            "content": "TEXT",
            "status": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_tasks():
    ensure_table(
        "tasks",
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            description TEXT,
            status TEXT,
            priority TEXT,
            due_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "title": "TEXT",
            "description": "TEXT",
            "status": "TEXT",
            "priority": "TEXT",
            "due_date": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_projects():
    ensure_table(
        "projects",
        """
        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            name TEXT,
            description TEXT,
            location TEXT,
            status TEXT,
            budget TEXT,
            investment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "title": "TEXT",
            "name": "TEXT",
            "description": "TEXT",
            "location": "TEXT",
            "status": "TEXT",
            "budget": "TEXT",
            "investment": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_research():
    ensure_table(
        "research",
        """
        CREATE TABLE IF NOT EXISTS research (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            topic TEXT,
            content TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "topic": "TEXT",
            "content": "TEXT",
            "source": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_financial_models():
    ensure_table(
        "financial_models",
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            project_name TEXT,
            model_name TEXT,
            data JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "project_name": "TEXT",
            "model_name": "TEXT",
            "data": "JSONB",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_bot_projects():
    ensure_table(
        "bot_projects",
        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            project_key TEXT,
            name TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "project_key": "TEXT",
            "name": "TEXT",
            "description": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_bot_files():
    ensure_table(
        "bot_files",
        """
        CREATE TABLE IF NOT EXISTS bot_files (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            file_name TEXT,
            file_path TEXT,
            mime_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "file_name": "TEXT",
            "file_path": "TEXT",
            "mime_type": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def migrate_system_seed_meta():
    ensure_table(
        "system_seed_meta",
        """
        CREATE TABLE IF NOT EXISTS system_seed_meta (
            chat_id BIGINT,
            seed_version TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        {
            "chat_id": "BIGINT",
            "seed_version": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        }
    )


def create_indexes():
    indexes = [
        (
            "idx_v54_messages_chat_created",
            "messages",
            ["chat_id", "created_at"]
        ),
        (
            "idx_v54_facts_chat_key",
            "facts",
            ["chat_id", "fact_key"]
        ),
        (
            "idx_v54_memories_chat_created",
            "memories",
            ["chat_id", "created_at"]
        ),
        (
            "idx_v54_projects_chat_created",
            "projects",
            ["chat_id", "created_at"]
        ),
        (
            "idx_v54_tasks_chat_status",
            "tasks",
            ["chat_id", "status"]
        ),
        (
            "idx_v54_decisions_chat_created",
            "decisions",
            ["chat_id", "created_at"]
        ),
    ]

    for index_name, table_name, columns in indexes:
        try:
            quoted_columns = ", ".join(
                quote_identifier(c)
                for c in columns
            )

            query = (
                f"CREATE INDEX IF NOT EXISTS "
                f"{quote_identifier(index_name)} "
                f"ON {quote_identifier(table_name)} "
                f"({quoted_columns})"
            )

            db_execute(query)

        except Exception as exc:
            log_error(
                f"INDEX ERROR {index_name}",
                exc
            )


def initialize_database():
    global DB_READY

    if DB_POOL is None:
        return False

    try:
        migrate_messages()
        migrate_memories()
        migrate_memory_events()
        migrate_facts()
        migrate_fact_history()
        migrate_decisions()
        migrate_tasks()
        migrate_projects()
        migrate_research()
        migrate_financial_models()
        migrate_bot_projects()
        migrate_bot_files()
        migrate_system_seed_meta()

        create_indexes()

        if not database_ping():
            raise RuntimeError("Database ping failed")

        DB_READY = True

        log("DATABASE INITIALIZED AND MIGRATED SUCCESSFULLY")
        log("DATABASE READY")

        return True

    except Exception as exc:
        DB_READY = False
        log_error("DATABASE INITIALIZATION ERROR", exc)
        return False


# ============================================================
# GENERIC SAFE INSERT
# ============================================================

def dynamic_insert(table_name, values):
    """
    Inserts only columns that actually exist.

    Also checks legacy NOT NULL columns without defaults.
    This prevents the old:
        messages.message = NULL
    and:
        facts.fact_value = NULL
    problems.
    """

    columns = get_columns(table_name)

    insert_values = {}

    for key, value in values.items():
        if key in columns:
            insert_values[key] = value

    required_missing = []

    for column_name, metadata in columns.items():

        is_required = (
            metadata["is_nullable"] == "NO"
            and metadata["column_default"] is None
        )

        if is_required and column_name not in insert_values:
            required_missing.append(column_name)

    if required_missing:
        raise RuntimeError(
            f"Required columns missing for {table_name}: "
            f"{', '.join(required_missing)}"
        )

    if not insert_values:
        raise RuntimeError(
            f"No compatible columns found for {table_name}"
        )

    column_sql = ", ".join(
        quote_identifier(c)
        for c in insert_values.keys()
    )

    placeholders = ", ".join(
        ["%s"] * len(insert_values)
    )

    query = (
        f"INSERT INTO {quote_identifier(table_name)} "
        f"({column_sql}) "
        f"VALUES ({placeholders})"
    )

    db_execute(
        query,
        tuple(insert_values.values())
    )


# ============================================================
# MESSAGES
# ============================================================

def save_message(chat_id, role, message_text):
    text_value = str(message_text or "")

    values = {
        "chat_id": chat_id,
        "role": role,
        "message": text_value,
        "text": text_value,
        "content": text_value,
        "body": text_value,
        "message_text": text_value,
        "created_at": utc_now(),
    }

    try:
        dynamic_insert(
            "messages",
            values
        )
        return True

    except Exception as exc:
        log_error("SAVE_MESSAGE ERROR", exc)
        return False


def get_history(chat_id, limit=HISTORY_LIMIT):
    try:
        rows = db_execute(
            """
            SELECT *
            FROM messages
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True,
            commit=False
        )

        rows = list(reversed(rows))

        result = []

        columns = get_columns("messages")

        for row in rows:
            role = row.get("role") or "user"

            candidates = []

            for column in (
                "message",
                "text",
                "content",
                "body",
                "message_text"
            ):
                if column in columns:
                    value = row.get(column)

                    if value is not None and str(value).strip():
                        candidates.append(str(value))

            content = candidates[0] if candidates else ""

            if content:
                result.append({
                    "role": role,
                    "content": content
                })

        return result

    except Exception as exc:
        log_error("GET_HISTORY ERROR", exc)
        return []


# ============================================================
# MEMORY
# ============================================================

def create_memory(
    chat_id,
    content,
    memory_key=None,
    memory_type="general",
    source="user",
    importance="normal"
):
    content = str(content or "").strip()

    if not content:
        return False

    try:
        dynamic_insert(
            "memories",
            {
                "chat_id": chat_id,
                "memory_key": memory_key,
                "content": content,
                "memory": content,
                "text": content,
                "body": content,
                "memory_type": memory_type,
                "source": source,
                "importance": importance,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )

        try:
            dynamic_insert(
                "memory_events",
                {
                    "chat_id": chat_id,
                    "memory_key": memory_key,
                    "action": "create",
                    "content": content,
                    "created_at": utc_now(),
                }
            )
        except Exception as exc:
            log_error("MEMORY EVENT ERROR", exc)

        return True

    except Exception as exc:
        log_error("CREATE_MEMORY ERROR", exc)
        return False


def get_memories(chat_id, limit=50):
    try:
        columns = get_columns("memories")

        rows = db_execute(
            """
            SELECT *
            FROM memories
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True,
            commit=False
        )

        result = []

        for row in rows:
            candidates = []

            for column in (
                "content",
                "memory",
                "text",
                "body"
            ):
                if column in columns:
                    value = row.get(column)

                    if value is not None and str(value).strip():
                        candidates.append(str(value))

            if candidates:
                result.append({
                    "key": row.get("memory_key"),
                    "content": candidates[0],
                    "type": row.get("memory_type"),
                    "importance": row.get("importance"),
                })

        return result

    except Exception as exc:
        log_error("GET_MEMORIES ERROR", exc)
        return []


# ============================================================
# FACTS
# ============================================================

def fact_exists(chat_id, fact_key):
    try:
        row = db_execute(
            """
            SELECT 1
            FROM facts
            WHERE chat_id = %s
              AND fact_key = %s
            LIMIT 1
            """,
            (chat_id, fact_key),
            fetchone=True,
            commit=False
        )

        return row is not None

    except Exception as exc:
        log_error(
            f"FACT_EXISTS ERROR key={fact_key}",
            exc
        )
        return False


def create_fact(
    chat_id,
    fact_key,
    fact_value,
    source="GENIOSA_SYSTEM",
    status="confirmed",
    confidence="confirmed",
    category="system",
    memory_type="confirmed_fact"
):
    fact_key = str(fact_key or "").strip()
    fact_value = str(fact_value or "").strip()

    if not fact_key or not fact_value:
        return "failed"

    try:
        if fact_exists(chat_id, fact_key):
            return "exists"

        now = utc_now()

        # CRITICAL:
        # fact_value is explicitly populated.
        # This fixes the exact 5.3 error shown in Render logs.
        values = {
            "chat_id": chat_id,
            "fact_key": fact_key,

            "fact_value": fact_value,
            "content": fact_value,
            "fact": fact_value,
            "value": fact_value,
            "text": fact_value,
            "body": fact_value,

            "source": source,
            "confidence": confidence,
            "status": status,
            "category": category,
            "memory_type": memory_type,

            "created_at": now,
            "updated_at": now,
        }

        dynamic_insert(
            "facts",
            values
        )

        return "inserted"

    except Exception as exc:
        log_error(
            f"CREATE_FACT ERROR key={fact_key}",
            exc
        )
        return "failed"


def get_facts(chat_id, limit=100):
    try:
        columns = get_columns("facts")

        rows = db_execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True,
            commit=False
        )

        result = []

        for row in rows:
            value = None

            for column in (
                "fact_value",
                "content",
                "fact",
                "value",
                "text",
                "body"
            ):
                if column in columns:
                    candidate = row.get(column)

                    if candidate is not None and str(candidate).strip():
                        value = str(candidate)
                        break

            if value:
                result.append({
                    "key": row.get("fact_key"),
                    "value": value,
                    "source": row.get("source"),
                    "status": row.get("status"),
                    "confidence": row.get("confidence"),
                    "category": row.get("category"),
                })

        return result

    except Exception as exc:
        log_error("GET_FACTS ERROR", exc)
        return []


# ============================================================
# SYSTEM FACTS
# ============================================================

SYSTEM_FACTS = [

    # --------------------------------------------------------
    # COMPANY
    # --------------------------------------------------------

    (
        "company.name",
        "SAMTISI CONSTRUCTION LLC"
    ),
    (
        "company.id",
        "406391202"
    ),
    (
        "company.founded",
        "December 5, 2022"
    ),
    (
        "company.location",
        "Tbilisi, Georgia"
    ),
    (
        "company.industry",
        "Construction & Development"
    ),
    (
        "company.profile",
        "Georgian construction and development company."
    ),
    (
        "company.capabilities",
        "Residential, commercial and infrastructure construction and development."
    ),
    (
        "company.engineering",
        "Planning and engineering coordination through construction and completion."
    ),
    (
        "company.standards",
        "High standards, modern engineering solutions, reliability and strict quality control."
    ),
    (
        "company.goal",
        "Become a leading construction and development company in Georgia."
    ),

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    (
        "nikkea12.location",
        "Nikkea 12, Kutaisi, Georgia"
    ),
    (
        "nikkea12.land",
        "3,070 m²"
    ),
    (
        "nikkea12.saleable",
        "10,854 m²"
    ),
    (
        "nikkea12.rooms",
        "7,212 m² hotel/apartment rooms area"
    ),
    (
        "nikkea12.monolith",
        "21,500 m²"
    ),
    (
        "nikkea12.architecture",
        "16,000 m²"
    ),
    (
        "nikkea12.commercial_floor1",
        "836 m²"
    ),
    (
        "nikkea12.commercial_floor2",
        "1,006 m²"
    ),
    (
        "nikkea12.rooms_floors",
        "Floors 3–14 contain approximately 7,212 m² of hotel/apartment rooms."
    ),
    (
        "nikkea12.concept",
        "Hotel-type apartments, casino, shopping center, restaurant and top-floor lounge bar."
    ),
    (
        "nikkea12.landowner",
        "Landowner requirement: 1,800 m² apartments + 25 parking spaces + $300,000 cash."
    ),
    (
        "nikkea12.monolith_cost",
        "$170/m²"
    ),
    (
        "nikkea12.fitout_assumption",
        "Up to approximately $450/m² for the applicable fit-out assumption."
    ),
    (
        "nikkea12.apartment_price",
        "$1,500–1,800/m² minimum target sale range."
    ),
    (
        "nikkea12.commercial_price",
        "$2,500/m²"
    ),
    (
        "nikkea12.investor_share",
        "Investor 80%, SAMTISI operator 20%."
    ),
    (
        "nikkea12.investor_contribution",
        "Investor contribution model discussed as 35% of total cost + $300,000."
    ),
    (
        "nikkea12.bank_financing",
        "Remaining financing may be provided by bank financing; discussed interest assumption approximately 11.5–13.5% maximum."
    ),
    (
        "nikkea12.casino",
        "1,006 m² casino area is intended to be retained rather than sold."
    ),
    (
        "nikkea12.casino_value",
        "Target casino valuation discussed at minimum $2,000/m²."
    ),
    (
        "nikkea12.casino_rent",
        "Potential casino rent assumption discussed at minimum $50/m²/month."
    ),
    (
        "nikkea12.property_company",
        "Separate property/service company concept with 50/50 ownership between investor and SAMTISI."
    ),
    (
        "nikkea12.rental_split",
        "Proposed rental income allocation: 30% property company / 70% owner."
    ),
    (
        "nikkea12.daily_rent",
        "Target daily apartment rental discussed at approximately $150–300 per apartment."
    ),

    # --------------------------------------------------------
    # SAMGORI
    # --------------------------------------------------------

    (
        "samgori.location",
        "Giorgi Naderishvili Street, Samgori district, Tbilisi."
    ),
    (
        "samgori.land",
        "7,390 m²"
    ),
    (
        "samgori.build_area",
        "46,131 m²"
    ),
    (
        "samgori.construction_volume",
        "41,874 m³"
    ),
    (
        "samgori.saleable_area",
        "30,540 m² excluding parking."
    ),
    (
        "samgori.parking",
        "6,516 m²"
    ),
    (
        "samgori.residential",
        "22,143 m²"
    ),
    (
        "samgori.summer_area",
        "5,040 m²"
    ),
    (
        "samgori.commercial",
        "1,647 m²"
    ),
    (
        "samgori.office",
        "1,710 m²"
    ),
    (
        "samgori.construction_cost",
        "Approximately $253/m² of saleable area."
    ),
    (
        "samgori.land_cost",
        "$7,750,000"
    ),
    (
        "samgori.construction_need",
        "$15,000,000"
    ),
    (
        "samgori.total_capital",
        "Approximately $23,500,000."
    ),
    (
        "samgori.expected_profit",
        "Approximately $10,000,000 net profit target."
    ),
    (
        "samgori.feasibility_date",
        "April 14, 2026"
    ),
    (
        "samgori_period",
        "30 months + 12 months contingency."
    ),
    (
        "samgori.market_white_frame",
        "Isani–Samgori market check discussed around $1,300/m² for white-frame units."
    ),
    (
        "samgori.market_finished",
        "Market check discussed around $1,500/m² for finished units."
    ),

    # --------------------------------------------------------
    # GOLDEN LAKE / OQRI
    # --------------------------------------------------------

    (
        "goldenlake.main_land",
        "Approximately 46 hectares."
    ),
    (
        "goldenlake.area",
        "Lake and surrounding area discussed at approximately 70–80 hectares."
    ),
    (
        "goldenlake.land_need",
        "$35–40 million land requirement discussed."
    ),
    (
        "goldenlake.construction",
        "Planned construction approximately 250,000 m²."
    ),
    (
        "goldenlake.hotel5",
        "5-star hotel with approximately 200 rooms."
    ),
    (
        "goldenlake.hotel4",
        "4-star hotel with approximately 100–120 rooms."
    ),
    (
        "goldenlake.aquapark",
        "Aquapark concept."
    ),
    (
        "goldenlake.casino",
        "Casino concept."
    ),
    (
        "goldenlake.arena",
        "Sports/concert arena concept for approximately 10,000 guests."
    ),
    (
        "goldenlake.arena_area",
        "Arena area discussed at approximately 20,000–30,000 m²."
    ),
    (
        "goldenlake.apartments",
        "Approximately 150,000 m² apartment sale area."
    ),
    (
        "goldenlake.commercial",
        "Approximately 30,000 m² commercial area."
    ),
    (
        "goldenlake.apartment_fitout",
        "Apartment fit-out assumption approximately $1,000–1,200/m²."
    ),
    (
        "goldenlake.apartment_sale",
        "Apartment sale assumption approximately $2,500–3,000/m²."
    ),
    (
        "goldenlake.commercial_sale",
        "Commercial sale assumption approximately $3,500–5,000/m²."
    ),
    (
        "goldenlake.hotel5_rate",
        "5-star hotel rate assumption approximately $250–500/night."
    ),
    (
        "goldenlake.hotel4_rate",
        "4-star hotel rate assumption approximately $120–180/night."
    ),
    (
        "goldenlake.history",
        "Concept materials referenced a 1995 waterski championship."
    ),
    (
        "goldenlake_region",
        "Concept references the Paliastomi/Kolkheti area and a yacht club concept."
    ),
]


# ============================================================
# SEED METADATA
# ============================================================

def seed_meta_exists(chat_id):
    try:
        row = db_execute(
            """
            SELECT 1
            FROM system_seed_meta
            WHERE chat_id = %s
              AND seed_version = %s
            LIMIT 1
            """,
            (chat_id, SEED_VERSION),
            fetchone=True,
            commit=False
        )

        return row is not None

    except Exception as exc:
        log_error("SEED META CHECK ERROR", exc)
        return False


def save_seed_meta(chat_id):
    """
    Does NOT use:
        ON CONFLICT(chat_id, seed_version)

    because the legacy table may not have a UNIQUE constraint.

    Instead:
    1. check existence;
    2. insert;
    3. tolerate duplicate errors.
    """

    if seed_meta_exists(chat_id):
        return True

    try:
        db_execute(
            """
            INSERT INTO system_seed_meta
                (chat_id, seed_version, created_at)
            VALUES
                (%s, %s, %s)
            """,
            (
                chat_id,
                SEED_VERSION,
                utc_now()
            )
        )

        return True

    except psycopg2.errors.UniqueViolation:
        return True

    except Exception as exc:
        log_error("SAVE SEED META ERROR", exc)
        return False


def seed_system_facts_for_chat(chat_id):
    """
    Seed only missing facts.

    Existing facts are NEVER overwritten.

    Seed metadata is saved ONLY if all system facts are confirmed
    present after the operation.
    """

    try:
        if seed_meta_exists(chat_id):
            log(
                f"SYSTEM FACT SEED ALREADY COMPLETE "
                f"chat={chat_id} version={SEED_VERSION}"
            )
            return True

        total = len(SYSTEM_FACTS)
        present = 0
        failed = 0

        for fact_key, fact_value in SYSTEM_FACTS:

            status = create_fact(
                chat_id=chat_id,
                fact_key=fact_key,
                fact_value=fact_value,
                source="GENIOSA_SYSTEM",
                status="confirmed",
                confidence="confirmed",
                category="system",
                memory_type="confirmed_fact"
            )

            if status in ("inserted", "exists"):
                present += 1
            else:
                failed += 1

        log(
            f"SYSTEM FACT SEED RESULT "
            f"chat={chat_id} present={present}/{total} failed={failed}"
        )

        if present == total and failed == 0:
            if save_seed_meta(chat_id):
                log(
                    f"SYSTEM FACT SEED COMPLETE "
                    f"chat={chat_id}"
                )
                return True

        log(
            f"SYSTEM FACT SEED INCOMPLETE "
            f"chat={chat_id}; will retry on next startup"
        )

        return False

    except Exception as exc:
        log_error(
            f"SEED ERROR chat={chat_id}",
            exc
        )
        return False


# ============================================================
# PROJECTS / TASKS / DECISIONS
# ============================================================

def get_projects(chat_id, limit=50):
    try:
        return db_execute(
            """
            SELECT *
            FROM projects
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True,
            commit=False
        )

    except Exception as exc:
        log_error("GET_PROJECTS ERROR", exc)
        return []


def get_tasks(chat_id, limit=50):
    try:
        return db_execute(
            """
            SELECT *
            FROM tasks
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True,
            commit=False
        )

    except Exception as exc:
        log_error("GET_TASKS ERROR", exc)
        return []


def get_decisions(chat_id, limit=50):
    try:
        return db_execute(
            """
            SELECT *
            FROM decisions
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True,
            commit=False
        )

    except Exception as exc:
        log_error("GET_DECISIONS ERROR", exc)
        return []


# ============================================================
# KNOWN CHAT IDS
# ============================================================

def get_known_chat_ids():
    chat_ids = set()

    for table_name in (
        "messages",
        "facts",
        "memories",
        "projects",
        "tasks",
        "decisions"
    ):
        try:
            if not table_exists(table_name):
                continue

            columns = get_columns(table_name)

            if "chat_id" not in columns:
                continue

            rows = db_execute(
                f"""
                SELECT DISTINCT chat_id
                FROM {quote_identifier(table_name)}
                WHERE chat_id IS NOT NULL
                """,
                fetch=True,
                commit=False
            )

            for row in rows:
                if row.get("chat_id") is not None:
                    chat_ids.add(int(row["chat_id"]))

        except Exception as exc:
            log_error(
                f"KNOWN CHAT IDS ERROR table={table_name}",
                exc
            )

    return sorted(chat_ids)


# ============================================================
# CONTEXT
# ============================================================

def build_context(chat_id):
    parts = []

    try:
        facts = get_facts(chat_id)

        if facts:
            parts.append("CONFIRMED FACTS:")

            for fact in facts:
                parts.append(
                    f"- {fact['key']}: {fact['value']}"
                )

    except Exception as exc:
        log_error("CONTEXT FACTS ERROR", exc)

    try:
        memories = get_memories(chat_id)

        if memories:
            parts.append("")
            parts.append("SAVED MEMORIES:")

            for memory in memories:
                parts.append(
                    f"- {memory['content']}"
                )

    except Exception as exc:
        log_error("CONTEXT MEMORIES ERROR", exc)

    try:
        projects = get_projects(chat_id)

        if projects:
            parts.append("")
            parts.append("PROJECT RECORDS:")

            for project in projects:
                title = (
                    project.get("title")
                    or project.get("name")
                    or "Unnamed project"
                )

                description = project.get("description") or ""
                location = project.get("location") or ""
                status = project.get("status") or ""

                parts.append(
                    f"- {title} | "
                    f"{location} | "
                    f"{status} | "
                    f"{description}"
                )

    except Exception as exc:
        log_error("CONTEXT PROJECTS ERROR", exc)

    try:
        tasks = get_tasks(chat_id)

        if tasks:
            parts.append("")
            parts.append("TASKS:")

            for task in tasks:
                parts.append(
                    f"- {task.get('title') or ''} | "
                    f"{task.get('status') or ''} | "
                    f"{task.get('priority') or ''}"
                )

    except Exception as exc:
        log_error("CONTEXT TASKS ERROR", exc)

    try:
        decisions = get_decisions(chat_id)

        if decisions:
            parts.append("")
            parts.append("DECISIONS:")

            for decision in decisions:
                parts.append(
                    f"- {decision.get('title') or ''} | "
                    f"{decision.get('status') or ''} | "
                    f"{decision.get('content') or ''}"
                )

    except Exception as exc:
        log_error("CONTEXT DECISIONS ERROR", exc)

    context = "\n".join(parts)

    if len(context) > CONTEXT_CHAR_LIMIT:
        context = context[:CONTEXT_CHAR_LIMIT] + "\n[CONTEXT TRUNCATED]"

    return context


# ============================================================
# GENIOSA CONSTITUTION
# ============================================================

GENIOSA_CONSTITUTION = """
You are GENIOSA, a personal business advisor and strategic assistant.

CORE RULES:

1. The user is the founder/leader of the business context you are advising.
2. Your job is to help with business strategy, construction/development,
   investment, financial analysis, project economics, planning, risks,
   negotiations and decision-making.

3. NEVER invent facts.
4. NEVER present assumptions as confirmed facts.
5. Clearly distinguish:
   - CONFIRMED FACT
   - ASSUMPTION
   - ESTIMATE
   - SCENARIO
   - UNKNOWN / REQUIRES VERIFICATION

6. If information is missing, say that it is missing.
7. If a calculation depends on assumptions, show the assumptions.
8. When useful, calculate numbers transparently.
9. Do not pretend that an external web search, legal database,
   market database or current official source was consulted unless
   that capability actually occurred.

10. For current market prices, laws, regulations, taxes, interest rates,
    permits, sanctions, company status or other time-sensitive information,
    explicitly say that current verification is required when no verified
    current source is available.

11. Never silently change confirmed project facts.
12. If the user gives a new value that conflicts with an existing fact,
    point out the conflict and ask/indicate which value should be treated
    as current.

13. For investment analysis:
    - distinguish revenue from profit;
    - distinguish gross profit from net profit;
    - include financing costs when relevant;
    - consider taxes, contingencies, construction costs and sales costs;
    - identify major risks.

14. For investor proposals:
    clearly separate investor capital, debt, equity, ownership,
    control rights, profit distribution and repayment mechanisms.

15. For construction projects:
    pay attention to land, GFA, saleable area, construction cost,
    fit-out cost, financing, schedule, sales price, absorption,
    contingency and cash flow.

16. If the user asks for a recommendation, give a direct recommendation
    followed by the reasoning and the key risks.

17. Do not claim to have performed actions that you did not perform.

18. Protect business information. Do not unnecessarily expose internal
    technical details, API keys, database details or server information.

19. If a user asks you to permanently remember something, use the available
    memory mechanism or instruct them to use /remember if appropriate.

20. Your answers should be practical and decision-oriented.
"""


# ============================================================
# GEMINI HISTORY PREPARATION
# ============================================================

def normalize_gemini_history(history):
    """
    Converts database history into Gemini roles:
      user -> user
      assistant/model -> model

    Consecutive identical roles are merged.
    Leading model messages are removed.
    """

    normalized = []

    for item in history:
        role = str(item.get("role") or "user").lower()
        content = str(item.get("content") or "").strip()

        if not content:
            continue

        if role in ("assistant", "model", "bot", "geniosa"):
            gemini_role = "model"
        else:
            gemini_role = "user"

        if not normalized:
            if gemini_role == "model":
                continue

            normalized.append({
                "role": gemini_role,
                "text": content
            })
            continue

        if normalized[-1]["role"] == gemini_role:
            normalized[-1]["text"] += "\n\n" + content
        else:
            normalized.append({
                "role": gemini_role,
                "text": content
            })

    # Limit total history size.
    while normalized:
        total = sum(
            len(item["text"])
            for item in normalized
        )

        if total <= HISTORY_CHAR_LIMIT:
            break

        normalized.pop(0)

    return normalized


def build_gemini_contents(chat_id, current_message):
    history = get_history(
        chat_id,
        HISTORY_LIMIT
    )

    # The current user message was already saved before calling Gemini.
    # Remove that last duplicate from historical context.
    if history:
        last = history[-1]

        if (
            last.get("role") == "user"
            and last.get("content", "").strip()
            == current_message.strip()
        ):
            history = history[:-1]

    normalized = normalize_gemini_history(history)

    context = build_context(chat_id)

    current_prompt = (
        GENIOSA_CONSTITUTION
        + "\n\n"
        + "BUSINESS CONTEXT:\n"
        + (context if context else "No saved business context available.")
        + "\n\n"
        + "CURRENT USER REQUEST:\n"
        + current_message
    )

    if normalized:
        if normalized[-1]["role"] == "user":
            normalized[-1]["text"] += (
                "\n\n"
                + current_prompt
            )
        else:
            normalized.append({
                "role": "user",
                "text": current_prompt
            })
    else:
        normalized.append({
            "role": "user",
            "text": current_prompt
        })

    return [
        {
            "role": item["role"],
            "parts": [
                {
                    "text": item["text"]
                }
            ]
        }
        for item in normalized
    ]


# ============================================================
# GEMINI API
# ============================================================

def gemini_endpoint(model):
    return (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )


def extract_gemini_text(data):
    try:
        candidates = data.get("candidates") or []

        if not candidates:
            return ""

        candidate = candidates[0]

        content = candidate.get("content") or {}
        parts = content.get("parts") or []

        texts = []

        for part in parts:
            text_value = part.get("text")

            if text_value:
                texts.append(str(text_value))

        return "\n".join(texts).strip()

    except Exception as exc:
        log_error("GEMINI RESPONSE PARSE ERROR", exc)
        return ""


def ask_gemini(chat_id, user_message):
    if not GEMINI_API_KEY:
        return (
            "Gemini API key is not configured. "
            "Please configure GEMINI_API_KEY in Render."
        )

    try:
        contents = build_gemini_contents(
            chat_id,
            user_message
        )
    except Exception as exc:
        log_error("GEMINI CONTENT BUILD ERROR", exc)

        contents = [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            GENIOSA_CONSTITUTION
                            + "\n\n"
                            + user_message
                        )
                    }
                ]
            }
        ]

    models = []

    for model in [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS:
        if model and model not in models:
            models.append(model)

    last_status = None

    for model in models:

        url = gemini_endpoint(model)

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
                "maxOutputTokens": 4096
            }
        }

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY
        }

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=GEMINI_TIMEOUT
            )

            last_status = response.status_code

            if response.status_code == 200:
                data = response.json()

                answer = extract_gemini_text(data)

                if answer:
                    log(
                        f"GEMINI RESPONSE OK model={model}"
                    )
                    return answer

                log(
                    f"GEMINI EMPTY RESPONSE model={model}"
                )

            else:
                # Full response is logged server-side only.
                log(
                    f"GEMINI ERROR model={model} "
                    f"status={response.status_code} "
                    f"body={response.text[:2000]}"
                )

                # Authentication/quota errors should not cause
                # pointless model switching.
                if response.status_code in (401, 403, 429):
                    break

                # 400/404 can indicate unavailable model/API shape.
                continue

        except requests.RequestException as exc:
            log_error(
                f"GEMINI REQUEST ERROR model={model}",
                exc
            )
            continue

        except Exception as exc:
            log_error(
                f"GEMINI UNEXPECTED ERROR model={model}",
                exc
            )
            continue

    if last_status == 429:
        return (
            "ამ მომენტში AI სერვისის მოთხოვნის ლიმიტი მიღწეულია. "
            "გთხოვ, ცოტა მოგვიანებით სცადო."
        )

    if last_status in (401, 403):
        return (
            "AI სერვისთან ავტორიზაციის პრობლემა დაფიქსირდა. "
            "საჭიროა Gemini API-ის კონფიგურაციის შემოწმება."
        )

    return (
        "ამ მომენტში Geniosa-მ AI სერვისთან პასუხის მიღება ვერ შეძლო. "
        "გთხოვ, რამდენიმე წამში ხელახლა სცადო."
    )


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_url(method):
    return (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )


def telegram_call(
    method,
    payload=None,
    timeout=TELEGRAM_TIMEOUT
):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    response = requests.post(
        telegram_url(method),
        json=payload or {},
        timeout=timeout
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "ok": False,
            "description": response.text
        }

    if response.status_code >= 400:
        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{str(data)[:1000]}"
        )

    return data


def send_message(chat_id, text_value):
    if not text_value:
        return False

    text_value = str(text_value)

    # Telegram message limit is approximately 4096 characters.
    chunks = []

    while len(text_value) > 3900:
        cut = text_value.rfind(
            "\n",
            0,
            3900
        )

        if cut < 1000:
            cut = 3900

        chunks.append(
            text_value[:cut]
        )

        text_value = text_value[cut:]

    chunks.append(text_value)

    success = True

    for chunk in chunks:
        try:
            result = telegram_call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk
                }
            )

            if not result.get("ok"):
                success = False

        except Exception as exc:
            log_error("SEND MESSAGE ERROR", exc)
            success = False

    return success


def clear_webhook():
    try:
        result = telegram_call(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            }
        )

        if result.get("ok"):
            log("Telegram webhook cleared")
            return True

        log(
            f"Telegram webhook clear failed: {result}"
        )
        return False

    except Exception as exc:
        log_error("CLEAR WEBHOOK ERROR", exc)
        return False


# ============================================================
# OWNER SECURITY
# ============================================================

def is_authorized(update):
    """
    If GENIOSA_OWNER_ID is empty:
        allow normal operation.

    If configured:
        only the specified Telegram user ID is allowed.
    """

    if not GENIOSA_OWNER_ID:
        return True

    try:
        message = update.get("message") or {}
        sender = message.get("from") or {}

        user_id = str(
            sender.get("id") or ""
        )

        return user_id == GENIOSA_OWNER_ID

    except Exception:
        return False


# ============================================================
# COMMANDS
# ============================================================

def handle_command(chat_id, text_value):
    text_value = text_value.strip()

    command = text_value.split()[0].lower()

    if command == "/start":
        return (
            "გამარჯობა. მე ვარ GENIOSA — შენი პირადი ბიზნეს "
            "მრჩეველი და ასისტენტი.\n\n"
            "მე ვმუშაობ შენს პროექტებთან, ბიზნეს-ფაქტებთან, "
            "გადაწყვეტილებებთან და ფინანსურ ანალიზთან.\n\n"
            "ძირითადი ბრძანებები:\n"
            "/help — დახმარება\n"
            "/summary — მიმდინარე კონტექსტის შეჯამება\n"
            "/facts — შენახული ფაქტები\n"
            "/memory — შენახული მეხსიერება\n"
            "/remember ტექსტი — ინფორმაციის მუდმივ მეხსიერებაში შენახვა"
        )

    if command == "/help":
        return (
            "GENIOSA ბრძანებები:\n\n"
            "/start — დაწყება\n"
            "/help — დახმარება\n"
            "/summary — პროექტებისა და ბიზნეს-კონტექსტის შეჯამება\n"
            "/facts — დადასტურებული ფაქტები\n"
            "/memory — შენახული მეხსიერება\n"
            "/remember ტექსტი — ახალი ინფორმაციის მეხსიერებაში შენახვა\n\n"
            "ჩვეულებრივ რეჟიმში უბრალოდ მომწერე კითხვა."
        )

    if command == "/facts":
        facts = get_facts(chat_id)

        if not facts:
            return "ამ ჩატში დადასტურებული ფაქტები ჯერ არ არის."

        lines = ["დადასტურებული ფაქტები:\n"]

        for fact in facts:
            lines.append(
                f"• {fact['key']}: {fact['value']}"
            )

        return "\n".join(lines)

    if command == "/memory":
        memories = get_memories(chat_id)

        if not memories:
            return "შენახული მეხსიერება ჯერ არ არის."

        lines = ["შენახული მეხსიერება:\n"]

        for memory in memories:
            lines.append(
                f"• {memory['content']}"
            )

        return "\n".join(lines)

    if command == "/remember":
        parts = text_value.split(maxsplit=1)

        if len(parts) < 2:
            return (
                "გამოიყენე ასე:\n"
                "/remember აქ ჩაწერე ინფორმაცია, "
                "რომელიც გინდა რომ Geniosa-მ დაიმახსოვროს."
            )

        memory_text = parts[1].strip()

        if create_memory(
            chat_id=chat_id,
            content=memory_text,
            memory_type="user_saved",
            source="user",
            importance="high"
        ):
            return "დამახსოვრებულია."

        return (
            "ინფორმაციის შენახვა ვერ მოხერხდა. "
            "გთხოვ, ხელახლა სცადო."
        )

    if command == "/summary":
        return build_summary(chat_id)

    return None


def build_summary(chat_id):
    facts = get_facts(chat_id)
    memories = get_memories(chat_id)
    projects = get_projects(chat_id)
    tasks = get_tasks(chat_id)
    decisions = get_decisions(chat_id)

    lines = [
        "GENIOSA — მიმდინარე კონტექსტის შეჯამება",
        ""
    ]

    lines.append(
        f"დადასტურებული ფაქტები: {len(facts)}"
    )
    lines.append(
        f"მეხსიერება: {len(memories)}"
    )
    lines.append(
        f"პროექტები: {len(projects)}"
    )
    lines.append(
        f"Tasks: {len(tasks)}"
    )
    lines.append(
        f"Decisions: {len(decisions)}"
    )

    if facts:
        lines.append("")
        lines.append("მთავარი ფაქტები:")

        for fact in facts[:20]:
            lines.append(
                f"• {fact['key']}: {fact['value']}"
            )

    if projects:
        lines.append("")
        lines.append("პროექტები:")

        for project in projects[:10]:
            lines.append(
                "• "
                + str(
                    project.get("title")
                    or project.get("name")
                    or "Unnamed"
                )
            )

    return "\n".join(lines)


# ============================================================
# TELEGRAM UPDATE HANDLING
# ============================================================

def process_text_message(chat_id, text_value):
    text_value = text_value.strip()

    if not text_value:
        return

    command_response = None

    if text_value.startswith("/"):
        try:
            command_response = handle_command(
                chat_id,
                text_value
            )
        except Exception as exc:
            log_error("COMMAND ERROR", exc)

    if command_response is not None:
        save_message(
            chat_id,
            "user",
            text_value
        )

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

    # Save current user message BEFORE Gemini call.
    save_message(
        chat_id,
        "user",
        text_value
    )

    answer = ask_gemini(
        chat_id,
        text_value
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


def handle_update(update):
    if not isinstance(update, dict):
        return

    if not is_authorized(update):
        message = update.get("message") or {}
        chat = message.get("chat") or {}

        chat_id = chat.get("id")

        if chat_id is not None:
            send_message(
                chat_id,
                "წვდომა ამ Geniosa ბოტზე შეზღუდულია."
            )

        return

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat") or {}

    chat_id = chat.get("id")

    if chat_id is None:
        return

    text_value = message.get("text")

    if text_value:
        process_text_message(
            int(chat_id),
            str(text_value)
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def acquire_polling_lock():
    global POLLING_LOCK_CONNECTION
    global POLLING_LOCK_ACQUIRED

    if not DATABASE_URL:
        return False

    try:
        # Dedicated connection.
        # It must remain open while polling is running.
        POLLING_LOCK_CONNECTION = psycopg2.connect(
            DATABASE_URL
        )

        with POLLING_LOCK_CONNECTION.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (59030447754,)
            )

            row = cur.fetchone()

            acquired = bool(
                row and row[0]
            )

        if acquired:
            POLLING_LOCK_ACQUIRED = True
            log("POSTGRES POLLING LOCK ACQUIRED")
            return True

        try:
            POLLING_LOCK_CONNECTION.close()
        except Exception:
            pass

        POLLING_LOCK_CONNECTION = None

        log(
            "ANOTHER GENIOSA INSTANCE OWNS TELEGRAM POLLING"
        )

        return False

    except Exception as exc:
        log_error("POLLING LOCK ERROR", exc)

        try:
            if POLLING_LOCK_CONNECTION:
                POLLING_LOCK_CONNECTION.close()
        except Exception:
            pass

        POLLING_LOCK_CONNECTION = None
        POLLING_LOCK_ACQUIRED = False

        return False


def release_polling_lock():
    global POLLING_LOCK_CONNECTION
    global POLLING_LOCK_ACQUIRED

    if POLLING_LOCK_CONNECTION is not None:

        try:
            if not POLLING_LOCK_CONNECTION.closed:

                with POLLING_LOCK_CONNECTION.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (59030447754,)
                    )

                POLLING_LOCK_CONNECTION.commit()

        except Exception as exc:
            log_error(
                "POLLING LOCK RELEASE ERROR",
                exc
            )

        finally:
            try:
                POLLING_LOCK_CONNECTION.close()
            except Exception:
                pass

    POLLING_LOCK_CONNECTION = None
    POLLING_LOCK_ACQUIRED = False

    log("POSTGRES POLLING LOCK RELEASED")


def get_updates(offset=None):
    payload = {
        "timeout": 25,
        "allowed_updates": [
            "message"
        ]
    }

    if offset is not None:
        payload["offset"] = offset

    return telegram_call(
        "getUpdates",
        payload,
        timeout=35
    )


def polling_loop():
    global POLLING_RUNNING
    global LAST_UPDATE_ID

    if not acquire_polling_lock():
        POLLING_RUNNING = False
        return

    try:
        clear_webhook()

        POLLING_RUNNING = True

        log("Telegram polling thread started")

        while not POLLING_STOP_EVENT.is_set():

            try:
                result = get_updates(
                    LAST_UPDATE_ID
                )

                if not result.get("ok"):
                    log(
                        f"Telegram getUpdates error: {result}"
                    )
                    time.sleep(3)
                    continue

                updates = result.get("result") or []

                for update in updates:

                    if POLLING_STOP_EVENT.is_set():
                        break

                    update_id = update.get("update_id")

                    try:
                        handle_update(update)

                    except Exception as exc:
                        log_error(
                            "HANDLE UPDATE ERROR",
                            exc
                        )

                    # Advance offset AFTER handling the update.
                    if update_id is not None:
                        LAST_UPDATE_ID = (
                            int(update_id) + 1
                        )

            except requests.RequestException as exc:
                log_error(
                    "TELEGRAM POLLING REQUEST ERROR",
                    exc
                )

                time.sleep(3)

            except Exception as exc:
                log_error(
                    "TELEGRAM POLLING ERROR",
                    exc
                )

                time.sleep(3)

    finally:
        POLLING_RUNNING = False

        release_polling_lock()

        log(
            "Telegram polling thread stopped"
        )


def start_polling():
    global POLLING_THREAD

    if not TELEGRAM_BOT_TOKEN:
        log("Telegram token not configured")
        return False

    if POLLING_THREAD is not None:
        if POLLING_THREAD.is_alive():
            return True

    POLLING_STOP_EVENT.clear()

    POLLING_THREAD = threading.Thread(
        target=polling_loop,
        name="geniosa-telegram-polling",
        daemon=True
    )

    POLLING_THREAD.start()

    return True


def stop_polling():
    POLLING_STOP_EVENT.set()

    thread = POLLING_THREAD

    if thread is not None:
        try:
            if thread.is_alive():
                thread.join(timeout=10)
        except Exception as exc:
            log_error(
                "POLLING THREAD JOIN ERROR",
                exc
            )


# ============================================================
# HEALTH
# ============================================================

def telegram_health():
    if not TELEGRAM_BOT_TOKEN:
        return False

    try:
        result = telegram_call(
            "getMe",
            {},
            timeout=15
        )

        return bool(result.get("ok"))

    except Exception as exc:
        log_error(
            "TELEGRAM HEALTH ERROR",
            exc
        )
        return False


def gemini_health():
    return bool(GEMINI_API_KEY)


@app.get("/")
def root():
    return JSONResponse(
        {
            "service": "GENIOSA",
            "version": APP_VERSION,
            "status": "live",
            "telegram_polling": POLLING_RUNNING,
            "database": DB_READY
        }
    )


@app.head("/")
def root_head():
    return JSONResponse(
        content={}
    )


@app.get("/health")
def health():
    db_ok = database_ping() if DB_POOL else False

    return JSONResponse(
        {
            "service": "GENIOSA",
            "version": APP_VERSION,
            "telegram_configured": bool(
                TELEGRAM_BOT_TOKEN
            ),
            "telegram_ok": telegram_health(),
            "gemini_configured": gemini_health(),
            "database_ok": db_ok,
            "db_pool_ready": DB_POOL is not None,
            "polling_running": POLLING_RUNNING,
            "startup_complete": STARTUP_COMPLETE
        }
    )


@app.get("/facts/{chat_id}")
def facts_endpoint(chat_id: int):
    return JSONResponse(
        {
            "chat_id": chat_id,
            "facts": get_facts(chat_id)
        }
    )


@app.get("/memory/{chat_id}")
def memory_endpoint(chat_id: int):
    return JSONResponse(
        {
            "chat_id": chat_id,
            "memory": get_memories(chat_id)
        }
    )


@app.get("/summary/{chat_id}")
def summary_endpoint(chat_id: int):
    return JSONResponse(
        {
            "chat_id": chat_id,
            "summary": build_summary(chat_id)
        }
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup_event():
    global STARTUP_COMPLETE

    log("=" * 60)
    log(f"GENIOSA {APP_VERSION} STARTUP")
    log("=" * 60)

    log(
        "CONFIG: "
        + str(
            {
                "telegram": bool(TELEGRAM_BOT_TOKEN),
                "gemini": bool(GEMINI_API_KEY),
                "database": bool(DATABASE_URL),
                "model": GEMINI_MODEL,
                "owner_protection": bool(
                    GENIOSA_OWNER_ID
                )
            }
        )
    )

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    if initialize_db_pool():

        if initialize_database():

            # Existing users/chats receive system facts.
            known_chats = get_known_chat_ids()

            log(
                f"KNOWN CHAT IDS: {known_chats}"
            )

            for chat_id in known_chats:

                try:
                    seed_system_facts_for_chat(
                        chat_id
                    )

                except Exception as exc:
                    log_error(
                        f"SEED CHAT ERROR {chat_id}",
                        exc
                    )

        else:
            log(
                "DATABASE INITIALIZATION FAILED"
            )

    else:
        log(
            "DATABASE POOL INITIALIZATION FAILED"
        )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    if TELEGRAM_BOT_TOKEN:
        start_polling()
    else:
        log(
            "TELEGRAM_BOT_TOKEN NOT CONFIGURED"
        )

    STARTUP_COMPLETE = True

    log(
        f"GENIOSA {APP_VERSION} STARTUP COMPLETE"
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
def shutdown_event():
    global STARTUP_COMPLETE
    global DB_POOL

    log(
        f"GENIOSA {APP_VERSION} SHUTDOWN"
    )

    stop_polling()

    if DB_POOL is not None:

        try:
            DB_POOL.closeall()
            log(
                "DATABASE CONNECTION POOL CLOSED"
            )

        except Exception as exc:
            log_error(
                "DATABASE POOL CLOSE ERROR",
                exc
            )

        finally:
            DB_POOL = None

    STARTUP_COMPLETE = False

    log(
        "GENIOSA SHUTDOWN COMPLETE"
    )


# ============================================================
# LOCAL EXECUTION
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
