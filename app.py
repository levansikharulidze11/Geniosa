# ============================================================
# GENIOSA 4.0 — PART 1/12
# Core configuration, imports, paths, environment,
# Telegram, Gemini, PostgreSQL and FastAPI
# ============================================================

import os
import re
import json
import time
import uuid
import base64
import logging
import mimetypes
import threading
import traceback

from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

import requests
import psycopg2
import psycopg2.extras

from fastapi import FastAPI
from fastapi.responses import JSONResponse


# ------------------------------------------------------------
# Application identity
# ------------------------------------------------------------

APP_NAME = "Geniosa"
APP_VERSION = "4.0"

APP_ENV = os.getenv(
    "APP_ENV",
    "production",
)

TIMEZONE = os.getenv(
    "TIMEZONE",
    "Asia/Tbilisi",
)


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    APP_NAME
)


# ------------------------------------------------------------
# Telegram configuration
# ------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

try:
    TELEGRAM_REQUEST_TIMEOUT = int(
        os.getenv(
            "TELEGRAM_REQUEST_TIMEOUT",
            "60",
        )
    )
except Exception:
    TELEGRAM_REQUEST_TIMEOUT = 60

TELEGRAM_REQUEST_TIMEOUT = max(
    10,
    min(
        TELEGRAM_REQUEST_TIMEOUT,
        600,
    ),
)

TELEGRAM_API_BASE = os.getenv(
    "TELEGRAM_API_BASE",
    "https://api.telegram.org",
).strip().rstrip("/")

TELEGRAM_FILE_BASE = os.getenv(
    "TELEGRAM_FILE_BASE",
    "https://api.telegram.org/file",
).strip().rstrip("/")

try:
    TELEGRAM_POLL_INTERVAL = float(
        os.getenv(
            "TELEGRAM_POLL_INTERVAL",
            "1",
        )
    )
except Exception:
    TELEGRAM_POLL_INTERVAL = 1.0

TELEGRAM_POLL_INTERVAL = max(
    0.1,
    min(
        TELEGRAM_POLL_INTERVAL,
        30.0,
    ),
)

try:
    TELEGRAM_POLL_TIMEOUT = int(
        os.getenv(
            "TELEGRAM_POLL_TIMEOUT",
            "30",
        )
    )
except Exception:
    TELEGRAM_POLL_TIMEOUT = 30

TELEGRAM_POLL_TIMEOUT = max(
    1,
    min(
        TELEGRAM_POLL_TIMEOUT,
        50,
    ),
)


# ------------------------------------------------------------
# Gemini configuration
# ------------------------------------------------------------

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    "",
).strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite",
).strip()

GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE",
    "https://generativelanguage.googleapis.com/v1beta",
).strip().rstrip("/")

try:
    GEMINI_TIMEOUT = int(
        os.getenv(
            "GEMINI_TIMEOUT",
            "120",
        )
    )
except Exception:
    GEMINI_TIMEOUT = 120

GEMINI_TIMEOUT = max(
    10,
    min(
        GEMINI_TIMEOUT,
        600,
    ),
)


# ------------------------------------------------------------
# PostgreSQL configuration
# ------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "",
).strip()

try:
    DATABASE_CONNECT_TIMEOUT = int(
        os.getenv(
            "DATABASE_CONNECT_TIMEOUT",
            "10",
        )
    )
except Exception:
    DATABASE_CONNECT_TIMEOUT = 10

DATABASE_CONNECT_TIMEOUT = max(
    3,
    min(
        DATABASE_CONNECT_TIMEOUT,
        60,
    ),
)


# ------------------------------------------------------------
# Owner / access configuration
# ------------------------------------------------------------

OWNER_ID_RAW = os.getenv(
    "OWNER_ID",
    "",
).strip()

try:
    OWNER_ID = (
        int(OWNER_ID_RAW)
        if OWNER_ID_RAW
        else None
    )
except Exception:
    OWNER_ID = None


ALLOWED_CHAT_IDS_RAW = os.getenv(
    "ALLOWED_CHAT_IDS",
    "",
).strip()


def _parse_chat_ids(
    raw_value: str,
) -> set:
    """
    Parses comma-separated Telegram chat IDs.
    """

    result = set()

    if not raw_value:
        return result

    for item in raw_value.split(","):

        item = item.strip()

        if not item:
            continue

        try:
            result.add(
                int(item)
            )
        except Exception:
            logger.warning(
                "Invalid chat ID in ALLOWED_CHAT_IDS: %s",
                item,
            )

    return result


ALLOWED_CHAT_IDS = _parse_chat_ids(
    ALLOWED_CHAT_IDS_RAW
)


# ------------------------------------------------------------
# Access control
# ------------------------------------------------------------

def is_chat_allowed(
    chat_id: Any,
) -> bool:
    """
    Determines whether a Telegram chat is allowed
    to use Geniosa.

    Priority:
    1. OWNER_ID
    2. ALLOWED_CHAT_IDS
    3. If no restrictions are configured, allow access.
    """

    try:
        normalized_chat_id = int(
            chat_id
        )
    except Exception:
        return False

    if (
        OWNER_ID is not None
        and normalized_chat_id == OWNER_ID
    ):
        return True

    if (
        ALLOWED_CHAT_IDS
        and normalized_chat_id in ALLOWED_CHAT_IDS
    ):
        return True

    if (
        OWNER_ID is None
        and not ALLOWED_CHAT_IDS
    ):
        return True

    return False


# ------------------------------------------------------------
# Storage paths
# IMPORTANT:
# All filesystem paths are pathlib.Path objects.
# ------------------------------------------------------------

BASE_DIR = Path(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

STORAGE_DIR = (
    BASE_DIR / "storage"
)

UPLOADS_DIR = (
    STORAGE_DIR / "uploads"
)

GENERATED_DIR = (
    STORAGE_DIR / "generated"
)

TEMP_DIR = (
    STORAGE_DIR / "temp"
)

DOWNLOADS_DIR = (
    TEMP_DIR / "downloads"
)


# ------------------------------------------------------------
# Create required directories
# ------------------------------------------------------------

for directory in (
    STORAGE_DIR,
    UPLOADS_DIR,
    GENERATED_DIR,
    TEMP_DIR,
    DOWNLOADS_DIR,
):

    try:

        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

    except Exception as exc:

        logger.error(
            "Could not create directory %s: %s",
            directory,
            exc,
        )


# ------------------------------------------------------------
# Document configuration
# ------------------------------------------------------------

try:
    MAX_DOCUMENT_SIZE_MB = float(
        os.getenv(
            "MAX_DOCUMENT_SIZE_MB",
            "25",
        )
    )
except Exception:
    MAX_DOCUMENT_SIZE_MB = 25.0

MAX_DOCUMENT_SIZE_MB = max(
    1.0,
    min(
        MAX_DOCUMENT_SIZE_MB,
        50.0,
    ),
)

MAX_DOCUMENT_SIZE_BYTES = int(
    MAX_DOCUMENT_SIZE_MB
    * 1024
    * 1024
)


# ------------------------------------------------------------
# PostgreSQL polling lock
# ------------------------------------------------------------

POLLING_LOCK_ID = int(
    os.getenv(
        "POLLING_LOCK_ID",
        "406391204",
    )
)

POLLING_STOP = threading.Event()

POLLING_THREAD = None

POLLING_THREAD_LOCK = (
    threading.Lock()
)

POLLING_LOCK_CONNECTION = None

POLLING_LOCK_ACQUIRED = False

LAST_UPDATE_ID = 0


# ------------------------------------------------------------
# FastAPI application
# ------------------------------------------------------------

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
)


# ------------------------------------------------------------
# Environment status
# ------------------------------------------------------------

def get_environment_status() -> Dict[str, Any]:
    """
    Returns current environment configuration status.
    """

    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "timezone": TIMEZONE,
        "telegram": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini": bool(
            GEMINI_API_KEY
        ),
        "database": bool(
            DATABASE_URL
        ),
        "owner_configured": (
            OWNER_ID is not None
        ),
        "allowed_chat_ids_configured": bool(
            ALLOWED_CHAT_IDS
        ),
    }


def validate_environment() -> Dict[str, Any]:
    """
    Validates required environment variables.

    Returns booleans rather than raising during startup,
    allowing Render logs to clearly show what is missing.
    """

    status = get_environment_status()

    if not status["telegram"]:
        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    if not status["gemini"]:
        logger.warning(
            "GEMINI_API_KEY is not configured."
        )

    if not status["database"]:
        logger.warning(
            "DATABASE_URL is not configured."
        )

    return status


# ------------------------------------------------------------
# PostgreSQL connection
# ------------------------------------------------------------

def get_db_connection():
    """
    Creates a PostgreSQL connection.
    """

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured."
        )

    connection = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=DATABASE_CONNECT_TIMEOUT,
    )

    return connection


def database_available() -> bool:
    """
    Checks whether PostgreSQL is reachable.
    """

    connection = None

    try:

        connection = get_db_connection()

        with connection.cursor() as cursor:

            cursor.execute(
                "SELECT 1"
            )

            cursor.fetchone()

        return True

    except Exception as exc:

        logger.warning(
            "Database availability check failed: %s",
            exc,
        )

        return False

    finally:

        if connection:

            try:
                connection.close()
            except Exception:
                pass


# ------------------------------------------------------------
# Telegram API
# ------------------------------------------------------------

def telegram_api_url(
    method: str,
) -> str:
    """
    Builds a Telegram Bot API URL.
    """

    method = str(
        method or ""
    ).strip().lstrip("/")

    if not method:
        raise ValueError(
            "Telegram API method is required."
        )

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    return (
        f"{TELEGRAM_API_BASE}"
        f"/bot{TELEGRAM_BOT_TOKEN}"
        f"/{method}"
    )


def telegram_request(
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Sends a request to the Telegram Bot API.
    """

    url = telegram_api_url(
        method
    )

    if timeout is None:
        timeout = TELEGRAM_REQUEST_TIMEOUT

    timeout = max(
        5,
        min(
            int(timeout),
            600,
        ),
    )

    response = requests.post(
        url,
        json=payload or {},
        timeout=timeout,
    )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Telegram returned invalid JSON."
        ) from exc

    if not isinstance(
        data,
        dict,
    ):
        raise RuntimeError(
            "Telegram returned an invalid response."
        )

    if not data.get(
        "ok",
        False,
    ):
        description = data.get(
            "description",
            "Unknown Telegram API error.",
        )

        raise RuntimeError(
            f"Telegram API error: {description}"
        )

    return data


def send_telegram_message(
    chat_id: Any,
    text: str,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
    **kwargs: Any,
) -> bool:
    """
    Sends a text message through Telegram.
    """

    if chat_id is None:
        return False

    message = str(
        text or ""
    ).strip()

    if not message:
        return False

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": message[:4096],
        "disable_web_page_preview": (
            disable_web_page_preview
        ),
    }

    if parse_mode:
        payload["parse_mode"] = parse_mode

    for key, value in kwargs.items():

        if value is not None:
            payload[key] = value

    try:

        telegram_request(
            "sendMessage",
            payload,
        )

        return True

    except Exception as exc:

        logger.warning(
            "Could not send Telegram message: %s",
            exc,
        )

        return False


def send_chat_action(
    chat_id: Any,
    action: str = "typing",
) -> bool:
    """
    Sends a Telegram chat action.
    """

    if chat_id is None:
        return False

    try:

        telegram_request(
            "sendChatAction",
            {
                "chat_id": chat_id,
                "action": action,
            },
        )

        return True

    except Exception as exc:

        logger.debug(
            "Could not send Telegram chat action: %s",
            exc,
        )

        return False


def telegram_get_me() -> Optional[Dict[str, Any]]:
    """
    Returns Telegram bot information.
    """

    try:

        data = telegram_request(
            "getMe"
        )

        result = data.get(
            "result"
        )

        if isinstance(
            result,
            dict,
        ):
            return result

    except Exception as exc:

        logger.warning(
            "Telegram getMe failed: %s",
            exc,
        )

    return None


# ------------------------------------------------------------
# Gemini API
# ------------------------------------------------------------

def gemini_api_url(
    model: Optional[str] = None,
) -> str:
    """
    Builds Gemini generateContent endpoint URL.
    """

    selected_model = (
        str(
            model or GEMINI_MODEL
        ).strip()
    )

    if not selected_model:
        selected_model = GEMINI_MODEL

    return (
        f"{GEMINI_API_BASE}"
        f"/models/{selected_model}"
        f":generateContent"
    )


# ------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------

def safe_text(
    value: Any,
    default: str = "",
) -> str:
    """
    Converts a value into safe text.
    """

    if value is None:
        return default

    try:
        result = str(
            value
        )
    except Exception:
        return default

    result = (
        result
        .replace("\x00", "")
        .strip()
    )

    return (
        result
        if result
        else default
    )


def normalize_text(
    value: Any,
) -> str:
    """
    Normalizes whitespace and line endings.
    """

    text = safe_text(
        value
    )

    if not text:
        return ""

    text = text.replace(
        "\r\n",
        "\n",
    )

    text = text.replace(
        "\r",
        "\n",
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


def utc_now() -> datetime:
    """
    Returns timezone-aware UTC datetime.
    """

    return datetime.now(
        timezone.utc
    )


def generate_uuid() -> str:
    """
    Generates a UUID string.
    """

    return str(
        uuid.uuid4()
    )


# ------------------------------------------------------------
# FastAPI routes
# ------------------------------------------------------------

@app.get("/")
def root():
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "status": "online",
    }


@app.head("/")
def root_head():
    return JSONResponse(
        content=None,
        status_code=200,
    )


@app.get("/status")
def status():
    return get_environment_status()


@app.get("/health/database")
def health_database():
    available = database_available()

    return {
        "database": (
            "ok"
            if available
            else "error"
        ),
    }


@app.get("/health")
def health():
    database_ok = database_available()

    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
        "database": database_ok,
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
    }


# ------------------------------------------------------------
# Startup configuration logging
# ------------------------------------------------------------

try:

    environment_status = (
        validate_environment()
    )

    logger.info(
        "%s %s configuration loaded.",
        APP_NAME,
        APP_VERSION,
    )

    logger.info(
        "Environment: %s",
        APP_ENV,
    )

    logger.info(
        "Storage directory: %s",
        STORAGE_DIR,
    )

    logger.info(
        "Uploads directory: %s",
        UPLOADS_DIR,
    )

    logger.info(
        "Generated directory: %s",
        GENERATED_DIR,
    )

    logger.info(
        "Temp directory: %s",
        TEMP_DIR,
    )

    logger.info(
        "Downloads directory: %s",
        DOWNLOADS_DIR,
    )

except Exception as exc:

    logger.exception(
        "Startup configuration logging failed: %s",
        exc,
    )
# ============================================================
# GENIOSA 4.0 — PART 1/12 LOADED
# ============================================================

print(
    "GENIOSA 4.0 — PART 1/12 LOADED"
)
)


# ============================================================
# GENIOSA 4.0 — PART 2/12
# PostgreSQL database layer, initialization and migrations
# ============================================================


# ============================================================
# 2.1 — DATABASE CONNECTION ALIAS
# ============================================================

def db():
    """
    Main PostgreSQL connection function used by Geniosa.
    """

    return get_db_connection()


# ============================================================
# 2.1.1 — DATABASE AVAILABILITY COMPATIBILITY FUNCTION
# ============================================================

def database_is_available() -> bool:
    """
    Compatibility wrapper used by other Geniosa parts.
    """

    return database_available()


# ============================================================
# 2.1.2 — GENERIC DATABASE EXECUTOR
# ============================================================

def db_execute(
    query: str,
    params: Optional[tuple] = None,
    fetchone: bool = False,
    fetchall: bool = False,
    commit: bool = False,
) -> Any:
    """
    Executes a PostgreSQL query using RealDictCursor.

    fetchone=True  -> returns one row.
    fetchall=True  -> returns all rows.
    commit=True    -> commits transaction.

    Database errors are logged and re-raised so callers
    can handle them explicitly.
    """

    connection = None
    cursor = None

    try:

        connection = db()

        cursor = connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        )

        cursor.execute(
            query,
            params or (),
        )

        result = None

        if fetchone:

            result = cursor.fetchone()

        elif fetchall:

            result = cursor.fetchall()

        if commit:

            connection.commit()

        return result

    except Exception as exc:

        if connection is not None:

            try:
                connection.rollback()

            except Exception:
                pass

        logger.error(
            "Database query failed: %s | Query: %s",
            exc,
            safe_text(query)[:1000],
        )

        raise

    finally:

        if cursor is not None:

            try:
                cursor.close()

            except Exception:
                pass

        if connection is not None:

            try:
                connection.close()

            except Exception:
                pass


# ============================================================
# 2.2 — SAFE COLUMN MIGRATION HELPER
# ============================================================

def _add_missing_columns(
    cursor,
    table_name: str,
    columns: Dict[str, str],
) -> None:
    """
    უსაფრთხოდ ამატებს ცხრილში დაკარგულ სვეტებს.

    მნიშვნელოვანი:
    არსებული მონაცემების მქონე ცხრილში ახალი NOT NULL
    column-ის პირდაპირ დამატებამ შეიძლება migration
    გააფუჭოს. ამიტომ migration-ის დროს სვეტები ემატება
    nullable ფორმით, ხოლო საჭირო default მნიშვნელობები
    ცალკე ივსება ქვემოთ.
    """

    for column_name, column_type in columns.items():

        cursor.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN IF NOT EXISTS
            {column_name} {column_type}
            """
        )


# ============================================================
# 2.3 — DATABASE INITIALIZATION
# ============================================================

def init_db() -> bool:
    """
    Creates all Geniosa database tables and applies
    safe, non-destructive schema migrations.

    Existing database data is preserved.

    This function is intentionally idempotent and can
    safely be executed repeatedly on application startup.
    """

    connection = None
    cursor = None

    try:

        connection = db()

        cursor = connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        )

        # ====================================================
        # 2.3.1 — MESSAGES
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                role TEXT NOT NULL,

                text TEXT,

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        message_columns = {

            "chat_id": "TEXT",

            "role": "TEXT",

            "text": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "messages",
            message_columns,
        )

        # ----------------------------------------------------
        # Legacy message migration
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'messages'
            """
        )

        existing_message_columns = {
            row["column_name"]
            for row in (cursor.fetchall() or [])
        }

        legacy_message_columns = (
            "content",
            "message_text",
            "message",
            "body",
        )

        legacy_message_column = None

        for candidate in legacy_message_columns:

            if candidate in existing_message_columns:

                legacy_message_column = candidate
                break

        if legacy_message_column:

            cursor.execute(
                f"""
                UPDATE messages
                SET text = {legacy_message_column}
                WHERE text IS NULL
                  AND {legacy_message_column} IS NOT NULL
                """
            )

            logger.info(
                "Messages migration: copied legacy `%s` into `text`.",
                legacy_message_column,
            )

        cursor.execute(
            """
            UPDATE messages
            SET text = ''
            WHERE text IS NULL
            """
        )

        cursor.execute(
            """
            UPDATE messages
            SET role = 'user'
            WHERE role IS NULL
               OR BTRIM(role) = ''
            """
        )

        cursor.execute(
            """
            UPDATE messages
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        # ====================================================
        # 2.3.2 — BUSINESS MEMORY
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS business_memory (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                memory TEXT NOT NULL,

                category TEXT DEFAULT 'general',

                importance INTEGER DEFAULT 5,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        business_memory_columns = {

            "chat_id": "TEXT",

            "memory": "TEXT",

            "category": (
                "TEXT DEFAULT 'general'"
            ),

            "importance": (
                "INTEGER DEFAULT 5"
            ),

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "business_memory",
            business_memory_columns,
        )

        cursor.execute(
            """
            UPDATE business_memory
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE business_memory
            SET memory = ''
            WHERE memory IS NULL
            """
        )

        cursor.execute(
            """
            UPDATE business_memory
            SET category = 'general'
            WHERE category IS NULL
               OR BTRIM(category) = ''
            """
        )

        cursor.execute(
            """
            UPDATE business_memory
            SET importance = 5
            WHERE importance IS NULL
            """
        )

        cursor.execute(
            """
            UPDATE business_memory
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.3.3 — PROJECTS
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                name TEXT NOT NULL,

                industry TEXT,

                location TEXT,

                description TEXT,

                land_area DOUBLE PRECISION,

                saleable_area DOUBLE PRECISION,

                construction_area DOUBLE PRECISION,

                total_area DOUBLE PRECISION,

                land_cost DOUBLE PRECISION,

                construction_cost DOUBLE PRECISION,

                operating_cost DOUBLE PRECISION,

                financing_cost DOUBLE PRECISION,

                other_cost DOUBLE PRECISION,

                total_cost DOUBLE PRECISION,

                revenue DOUBLE PRECISION,

                expected_revenue DOUBLE PRECISION,

                net_profit DOUBLE PRECISION,

                expected_profit DOUBLE PRECISION,

                investor_capital DOUBLE PRECISION,

                investor_profit DOUBLE PRECISION,

                investor_share DOUBLE PRECISION,

                notes TEXT,

                status TEXT DEFAULT 'active',

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        project_columns = {

            "chat_id": "TEXT",

            "name": "TEXT",

            "industry": "TEXT",

            "location": "TEXT",

            "description": "TEXT",

            "land_area": "DOUBLE PRECISION",

            "saleable_area": "DOUBLE PRECISION",

            "construction_area": "DOUBLE PRECISION",

            "total_area": "DOUBLE PRECISION",

            "land_cost": "DOUBLE PRECISION",

            "construction_cost": "DOUBLE PRECISION",

            "operating_cost": "DOUBLE PRECISION",

            "financing_cost": "DOUBLE PRECISION",

            "other_cost": "DOUBLE PRECISION",

            "total_cost": "DOUBLE PRECISION",

            "revenue": "DOUBLE PRECISION",

            "expected_revenue": "DOUBLE PRECISION",

            "net_profit": "DOUBLE PRECISION",

            "expected_profit": "DOUBLE PRECISION",

            "investor_capital": "DOUBLE PRECISION",

            "investor_profit": "DOUBLE PRECISION",

            "investor_share": "DOUBLE PRECISION",

            "notes": "TEXT",

            "status": (
                "TEXT DEFAULT 'active'"
            ),

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "projects",
            project_columns,
        )

        cursor.execute(
            """
            UPDATE projects
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE projects
            SET name = 'Unnamed Project'
            WHERE name IS NULL
               OR BTRIM(name) = ''
            """
        )

        cursor.execute(
            """
            UPDATE projects
            SET status = 'active'
            WHERE status IS NULL
               OR BTRIM(status) = ''
            """
        )

        cursor.execute(
            """
            UPDATE projects
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.3.4 — DOCUMENTS
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                project_id BIGINT,

                filename TEXT NOT NULL,

                file_type TEXT,

                extracted_text TEXT,

                analysis TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        document_columns = {

            "chat_id": "TEXT",

            "project_id": "BIGINT",

            "filename": "TEXT",

            "file_type": "TEXT",

            "extracted_text": "TEXT",

            "analysis": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "documents",
            document_columns,
        )

        cursor.execute(
            """
            UPDATE documents
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE documents
            SET filename = 'unknown'
            WHERE filename IS NULL
               OR BTRIM(filename) = ''
            """
        )

        cursor.execute(
            """
            UPDATE documents
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.3.5 — INVESTORS
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS investors (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                name TEXT NOT NULL,

                company TEXT,

                country TEXT,

                contact TEXT,

                investment_capacity DOUBLE PRECISION,

                preferred_sector TEXT,

                status TEXT DEFAULT 'new',

                notes TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        investor_columns = {

            "chat_id": "TEXT",

            "name": "TEXT",

            "company": "TEXT",

            "country": "TEXT",

            "contact": "TEXT",

            "investment_capacity": (
                "DOUBLE PRECISION"
            ),

            "preferred_sector": "TEXT",

            "status": (
                "TEXT DEFAULT 'new'"
            ),

            "notes": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "investors",
            investor_columns,
        )

        cursor.execute(
            """
            UPDATE investors
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE investors
            SET name = 'Unnamed Investor'
            WHERE name IS NULL
               OR BTRIM(name) = ''
            """
        )

        cursor.execute(
            """
            UPDATE investors
            SET status = 'new'
            WHERE status IS NULL
               OR BTRIM(status) = ''
            """
        )

        cursor.execute(
            """
            UPDATE investors
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.3.6 — DEALS
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS deals (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                project_id BIGINT,

                investor_id BIGINT,

                stage TEXT DEFAULT 'new',

                proposed_amount DOUBLE PRECISION,

                proposed_share DOUBLE PRECISION,

                valuation DOUBLE PRECISION,

                notes TEXT,

                next_step TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        deal_columns = {

            "chat_id": "TEXT",

            "project_id": "BIGINT",

            "investor_id": "BIGINT",

            "stage": (
                "TEXT DEFAULT 'new'"
            ),

            "proposed_amount": (
                "DOUBLE PRECISION"
            ),

            "proposed_share": (
                "DOUBLE PRECISION"
            ),

            "valuation": (
                "DOUBLE PRECISION"
            ),

            "notes": "TEXT",

            "next_step": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "deals",
            deal_columns,
        )

        cursor.execute(
            """
            UPDATE deals
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE deals
            SET stage = 'new'
            WHERE stage IS NULL
               OR BTRIM(stage) = ''
            """
        )

        cursor.execute(
            """
            UPDATE deals
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.3.7 — RESEARCH
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS research (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                project_id BIGINT,

                query TEXT NOT NULL,

                result TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        research_columns = {

            "chat_id": "TEXT",

            "project_id": "BIGINT",

            "query": "TEXT",

            "result": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "research",
            research_columns,
        )

        cursor.execute(
            """
            UPDATE research
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE research
            SET query = ''
            WHERE query IS NULL
            """
        )

        # ====================================================
        # 2.3.8 — FINANCIAL ANALYSES
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS financial_analyses (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                project_id BIGINT,

                analysis_type TEXT,

                input_data TEXT,

                result_data TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        financial_columns = {

            "chat_id": "TEXT",

            "project_id": "BIGINT",

            "analysis_type": "TEXT",

            "input_data": "TEXT",

            "result_data": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "financial_analyses",
            financial_columns,
        )

        cursor.execute(
            """
            UPDATE financial_analyses
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        # ====================================================
        # 2.3.9 — GENERATED ASSETS
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_assets (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                project_id BIGINT,

                asset_type TEXT,

                filename TEXT NOT NULL,

                description TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        generated_asset_columns = {

            "chat_id": "TEXT",

            "project_id": "BIGINT",

            "asset_type": "TEXT",

            "filename": "TEXT",

            "description": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "generated_assets",
            generated_asset_columns,
        )

        cursor.execute(
            """
            UPDATE generated_assets
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE generated_assets
            SET filename = 'unknown'
            WHERE filename IS NULL
               OR BTRIM(filename) = ''
            """
        )

        # ====================================================
        # 2.3.10 — SECURITIES
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS securities (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                symbol TEXT NOT NULL,

                name TEXT,

                asset_type TEXT,

                exchange TEXT,

                currency TEXT,

                quantity DOUBLE PRECISION,

                average_price DOUBLE PRECISION,

                notes TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        securities_columns = {

            "chat_id": "TEXT",

            "symbol": "TEXT",

            "name": "TEXT",

            "asset_type": "TEXT",

            "exchange": "TEXT",

            "currency": "TEXT",

            "quantity": "DOUBLE PRECISION",

            "average_price": (
                "DOUBLE PRECISION"
            ),

            "notes": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "securities",
            securities_columns,
        )

        cursor.execute(
            """
            UPDATE securities
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE securities
            SET symbol = 'UNKNOWN'
            WHERE symbol IS NULL
               OR BTRIM(symbol) = ''
            """
        )

        cursor.execute(
            """
            UPDATE securities
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.3.11 — CRYPTO ASSETS
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS crypto_assets (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                symbol TEXT NOT NULL,

                name TEXT,

                quantity DOUBLE PRECISION,

                average_price DOUBLE PRECISION,

                wallet TEXT,

                notes TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP

            )
            """
        )

        crypto_columns = {

            "chat_id": "TEXT",

            "symbol": "TEXT",

            "name": "TEXT",

            "quantity": "DOUBLE PRECISION",

            "average_price": (
                "DOUBLE PRECISION"
            ),

            "wallet": "TEXT",

            "notes": "TEXT",

            "created_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        _add_missing_columns(
            cursor,
            "crypto_assets",
            crypto_columns,
        )

        cursor.execute(
            """
            UPDATE crypto_assets
            SET chat_id = '0'
            WHERE chat_id IS NULL
               OR BTRIM(chat_id) = ''
            """
        )

        cursor.execute(
            """
            UPDATE crypto_assets
            SET symbol = 'UNKNOWN'
            WHERE symbol IS NULL
               OR BTRIM(symbol) = ''
            """
        )

        cursor.execute(
            """
            UPDATE crypto_assets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE updated_at IS NULL
            """
        )

        # ====================================================
        # 2.4 — INDEXES
        # ====================================================

        indexes = (

            (
                "idx_messages_chat_id",
                "messages(chat_id)",
            ),

            (
                "idx_messages_created_at",
                "messages(created_at)",
            ),

            (
                "idx_business_memory_chat_id",
                "business_memory(chat_id)",
            ),

            (
                "idx_business_memory_category",
                "business_memory(category)",
            ),

            (
                "idx_projects_chat_id",
                "projects(chat_id)",
            ),

            (
                "idx_projects_status",
                "projects(status)",
            ),

            (
                "idx_documents_chat_id",
                "documents(chat_id)",
            ),

            (
                "idx_documents_project_id",
                "documents(project_id)",
            ),

            (
                "idx_investors_chat_id",
                "investors(chat_id)",
            ),

            (
                "idx_investors_status",
                "investors(status)",
            ),

            (
                "idx_deals_chat_id",
                "deals(chat_id)",
            ),

            (
                "idx_deals_project_id",
                "deals(project_id)",
            ),

            (
                "idx_deals_investor_id",
                "deals(investor_id)",
            ),

            (
                "idx_research_chat_id",
                "research(chat_id)",
            ),

            (
                "idx_research_project_id",
                "research(project_id)",
            ),

            (
                "idx_financial_analyses_chat_id",
                "financial_analyses(chat_id)",
            ),

            (
                "idx_financial_analyses_project_id",
                "financial_analyses(project_id)",
            ),

            (
                "idx_generated_assets_chat_id",
                "generated_assets(chat_id)",
            ),

            (
                "idx_generated_assets_project_id",
                "generated_assets(project_id)",
            ),

            (
                "idx_securities_chat_id",
                "securities(chat_id)",
            ),

            (
                "idx_crypto_assets_chat_id",
                "crypto_assets(chat_id)",
            ),

        )

        for index_name, index_definition in indexes:

            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS
                {index_name}
                ON {index_definition}
                """
            )

        # ====================================================
        # 2.5 — COMMIT
        # ====================================================

        connection.commit()

        logger.info(
            "Geniosa PostgreSQL database initialized successfully."
        )

        return True

    except Exception as exc:

        if connection is not None:

            try:
                connection.rollback()

            except Exception:
                pass

        logger.exception(
            "Database initialization failed: %s",
            exc,
        )

        return False

    finally:

        if cursor is not None:

            try:
                cursor.close()

            except Exception:
                pass

        if connection is not None:

            try:
                connection.close()

            except Exception:
                pass


# ============================================================
# 2.6 — DATABASE STARTUP TEST
# ============================================================

def ensure_database_ready() -> bool:
    """
    Initializes PostgreSQL and verifies availability.
    """

    if not DATABASE_URL:

        logger.error(
            "DATABASE_URL is missing."
        )

        return False

    if not init_db():

        logger.error(
            "Database initialization failed."
        )

        return False

    if not database_is_available():

        logger.error(
            "Database availability check failed."
        )

        return False

    logger.info(
        "Geniosa database is ready."
    )

    return True


# ============================================================
# 2.7 — PART 2 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 2/12 LOADED"
) 
# ============================================================
# GENIOSA 4.0 — PART 3/12
# Messages, persistent business memory and AI context
# ============================================================


# ============================================================
# 3.1 — INTERNAL HELPERS
# ============================================================

def _normalize_chat_id(
    chat_id: Any
) -> str:

    if chat_id is None:
        return ""

    return str(
        chat_id
    ).strip()


def _safe_limit(
    value: Any,
    default: int = 20,
    minimum: int = 1,
    maximum: int = 100,
) -> int:

    try:
        value = int(value)
    except Exception:
        value = default

    return max(
        minimum,
        min(
            value,
            maximum,
        )
    )


# ============================================================
# 3.2 — SAVE MESSAGE
# ============================================================

def save_message(
    chat_id: Any,
    role: str,
    text: Optional[str] = None,
    content: Optional[str] = None,
) -> Optional[int]:

    normalized_chat_id = _normalize_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        logger.warning(
            "save_message called without chat_id"
        )
        return None

    message_text = text

    if message_text is None:
        message_text = content

    if message_text is None:
        message_text = ""

    message_text = str(
        message_text
    ).strip()

    role = str(
        role or "user"
    ).strip().lower()

    if role not in {
        "user",
        "assistant",
        "system",
    }:
        role = "user"

    try:

        result = db_execute(
            """
            INSERT INTO messages (
                chat_id,
                role,
                text
            )
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                normalized_chat_id,
                role,
                message_text,
            ),
            fetchone=True,
            commit=True,
        )

        if not result:
            return None

        message_id = result.get(
            "id"
        )

        if message_id is None:
            return None

        return int(
            message_id
        )

    except Exception as exc:

        logger.error(
            "Failed to save message: %s",
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 3.3 — GET RECENT MESSAGES
# ============================================================

def get_recent_messages(
    chat_id: Any,
    limit: int = 20,
) -> List[Dict[str, Any]]:

    normalized_chat_id = _normalize_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return []

    safe_limit = _safe_limit(
        limit,
        default=20,
        minimum=1,
        maximum=100,
    )

    try:

        rows = db_execute(
            f"""
            SELECT
                id,
                chat_id,
                role,
                text,
                created_at
            FROM messages
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT {safe_limit}
            """,
            (
                normalized_chat_id,
            ),
            fetchall=True,
        )

        rows = rows or []

        return list(
            reversed(rows)
        )

    except Exception as exc:

        logger.error(
            "Failed to get recent messages: %s",
            exc,
            exc_info=True,
        )

        return []


# ============================================================
# 3.4 — FORMAT CONVERSATION HISTORY
# ============================================================

def format_conversation_history(
    chat_id: Any,
    limit: int = 20,
) -> str:

    messages = get_recent_messages(
        chat_id,
        limit=limit,
    )

    if not messages:
        return (
            "NO RECENT CONVERSATION HISTORY."
        )

    lines = [
        "RECENT CONVERSATION:"
    ]

    for message in messages:

        if not isinstance(
            message,
            dict,
        ):
            continue

        role = str(
            message.get(
                "role",
                "user",
            )
        ).strip().upper()

        if role not in {
            "USER",
            "ASSISTANT",
            "SYSTEM",
        }:
            role = "USER"

        text_value = str(
            message.get(
                "text",
                "",
            ) or ""
        ).strip()

        if not text_value:
            continue

        lines.append(
            f"{role}: {text_value}"
        )

    if len(lines) == 1:
        return (
            "NO RECENT CONVERSATION HISTORY."
        )

    return "\n".join(
        lines
    )


# ============================================================
# 3.5 — SAVE BUSINESS MEMORY
# ============================================================

def save_memory(
    chat_id: Any,
    memory: str,
    category: str = "general",
    importance: int = 5,
) -> Optional[int]:

    normalized_chat_id = _normalize_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        logger.warning(
            "save_memory called without chat_id"
        )
        return None

    memory = str(
        memory or ""
    ).strip()

    category = str(
        category or "general"
    ).strip()

    if not category:
        category = "general"

    try:
        importance = int(
            importance
        )
    except Exception:
        importance = 5

    importance = max(
        1,
        min(
            importance,
            10,
        )
    )

    if not memory:
        return None

    try:

        result = db_execute(
            """
            INSERT INTO business_memory (
                chat_id,
                memory,
                category,
                importance
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                normalized_chat_id,
                memory,
                category,
                importance,
            ),
            fetchone=True,
            commit=True,
        )

        if not result:
            return None

        memory_id = result.get(
            "id"
        )

        if memory_id is None:
            return None

        return int(
            memory_id
        )

    except Exception as exc:

        logger.error(
            "Failed to save business memory: %s",
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 3.6 — GET BUSINESS MEMORIES
# ============================================================

def get_memories(
    chat_id: Any,
    limit: int = 30,
) -> List[Dict[str, Any]]:

    normalized_chat_id = _normalize_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return []

    safe_limit = _safe_limit(
        limit,
        default=30,
        minimum=1,
        maximum=100,
    )

    try:

        rows = db_execute(
            f"""
            SELECT
                id,
                chat_id,
                memory,
                category,
                importance,
                created_at,
                updated_at
            FROM business_memory
            WHERE chat_id = %s
            ORDER BY
                importance DESC,
                updated_at DESC,
                id DESC
            LIMIT {safe_limit}
            """,
            (
                normalized_chat_id,
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get business memories: %s",
            exc,
            exc_info=True,
        )

        return []


# ============================================================
# 3.7 — BUILD MEMORY CONTEXT
# ============================================================

def build_memory_context(
    chat_id: Any,
    limit: int = 30,
) -> str:

    memories = get_memories(
        chat_id,
        limit=limit,
    )

    if not memories:
        return (
            "PERSISTENT BUSINESS MEMORY:\nNONE"
        )

    lines = [
        "PERSISTENT BUSINESS MEMORY:"
    ]

    for item in memories:

        if not isinstance(
            item,
            dict,
        ):
            continue

        memory = str(
            item.get(
                "memory",
                "",
            ) or ""
        ).strip()

        if not memory:
            continue

        category = str(
            item.get(
                "category",
                "general",
            ) or "general"
        ).strip()

        if not category:
            category = "general"

        importance = item.get(
            "importance",
            5,
        )

        try:
            importance = int(
                importance
            )
        except Exception:
            importance = 5

        importance = max(
            1,
            min(
                importance,
                10,
            )
        )

        lines.append(
            f"- [{category}] "
            f"(importance {importance}/10) "
            f"{memory}"
        )

    if len(lines) == 1:
        lines.append(
            "NONE"
        )

    return "\n".join(
        lines
    )


# ============================================================
# 3.8 — DELETE BUSINESS MEMORY
# ============================================================

def delete_memory(
    chat_id: Any,
    memory_id: int,
) -> bool:

    normalized_chat_id = _normalize_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return False

    try:

        normalized_memory_id = int(
            memory_id
        )

    except Exception:

        logger.warning(
            "Invalid memory_id: %r",
            memory_id,
        )

        return False

    if normalized_memory_id <= 0:
        return False

    try:

        result = db_execute(
            """
            DELETE FROM business_memory
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                normalized_memory_id,
                normalized_chat_id,
            ),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to delete memory %s: %s",
            normalized_memory_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 3.9 — MEMORY SUMMARY
# ============================================================

def memories_summary(
    chat_id: Any,
    limit: int = 50,
) -> str:

    memories = get_memories(
        chat_id,
        limit=_safe_limit(
            limit,
            default=50,
            minimum=1,
            maximum=100,
        ),
    )

    if not memories:
        return (
            "🧠 ბიზნეს-მეხსიერება ცარიელია."
        )

    lines = [
        "🧠 GENIOSA — ბიზნეს-მეხსიერება",
        "",
    ]

    for item in memories:

        if not isinstance(
            item,
            dict,
        ):
            continue

        memory_id = item.get(
            "id"
        )

        category = str(
            item.get(
                "category",
                "general",
            ) or "general"
        ).strip()

        importance = item.get(
            "importance",
            5,
        )

        try:
            importance = int(
                importance
            )
        except Exception:
            importance = 5

        memory = str(
            item.get(
                "memory",
                "",
            ) or ""
        ).strip()

        if not memory:
            continue

        lines.append(
            f"#{memory_id} | "
            f"{category} | "
            f"{importance}/10"
        )

        lines.append(
            memory
        )

        lines.append(
            ""
        )

    if len(lines) <= 2:
        return (
            "🧠 ბიზნეს-მეხსიერება ცარიელია."
        )

    return "\n".join(
        lines
    ).strip()


# ============================================================
# 3.10 — INDUSTRY NAMES
# ============================================================

INDUSTRY_NAMES = {

    "construction": "მშენებლობა",

    "development": "დეველოპმენტი",

    "real_estate": "უძრავი ქონება",

    "hotel": "სასტუმრო ბიზნესი",

    "tourism": "ტურიზმი",

    "restaurant": "რესტორანი",

    "casino": "კაზინო / Gaming",

    "finance": "ფინანსები",

    "technology": "ტექნოლოგიები",

    "retail": "რიტეილი",

    "energy": "ენერგეტიკა",

    "infrastructure": "ინფრასტრუქტურა",

    "other": "სხვა",

}


def industry_name(
    industry: Optional[str]
) -> str:

    if industry is None:
        return "არ არის მითითებული"

    value = str(
        industry
    ).strip()

    if not value:
        return "არ არის მითითებული"

    return INDUSTRY_NAMES.get(
        value.lower(),
        value,
    )


# ============================================================
# 3.11 — PROJECT TO AI CONTEXT
# ============================================================

def project_to_ai_context(
    project: Optional[Dict[str, Any]]
) -> str:

    if not project:
        return (
            "NO PROJECT DATA."
        )

    if not isinstance(
        project,
        dict,
    ):
        return (
            "NO PROJECT DATA."
        )

    excluded = {
        "id",
        "chat_id",
        "created_at",
        "updated_at",
    }

    lines = []

    for key, value in project.items():

        if key in excluded:
            continue

        if value is None:
            continue

        if isinstance(
            value,
            str,
        ):

            value = value.strip()

            if not value:
                continue

        lines.append(
            f"{key}: {value}"
        )

    if not lines:
        return (
            "NO PROJECT DATA."
        )

    return "\n".join(
        lines
    )


# ============================================================
# 3.12 — BUILD PROJECTS CONTEXT
# ============================================================

def build_projects_context(
    chat_id: Any,
    limit: int = 20,
) -> str:

    normalized_chat_id = _normalize_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return (
            "ACTIVE PROJECTS:\nNONE"
        )

    safe_limit = _safe_limit(
        limit,
        default=20,
        minimum=1,
        maximum=50,
    )

    try:

        rows = db_execute(
            f"""
            SELECT
                id,
                name,
                industry,
                location,
                expected_revenue,
                expected_profit,
                total_cost,
                status
            FROM projects
            WHERE chat_id = %s
            ORDER BY
                updated_at DESC NULLS LAST,
                id DESC
            LIMIT {safe_limit}
            """,
            (
                normalized_chat_id,
            ),
            fetchall=True,
        )

        rows = rows or []

    except Exception as exc:

        logger.error(
            "Failed to build projects context: %s",
            exc,
            exc_info=True,
        )

        return (
            "ACTIVE PROJECTS:\nUNAVAILABLE"
        )

    if not rows:
        return (
            "ACTIVE PROJECTS:\nNONE"
        )

    lines = [
        "ACTIVE PROJECTS:"
    ]

    for project in rows:

        if not isinstance(
            project,
            dict,
        ):
            continue

        project_id = project.get(
            "id"
        )

        name = str(
            project.get(
                "name",
                "Unnamed",
            ) or "Unnamed"
        ).strip()

        if not name:
            name = "Unnamed"

        industry = industry_name(
            project.get(
                "industry"
            )
        )

        location = str(
            project.get(
                "location"
            ) or "N/A"
        ).strip()

        if not location:
            location = "N/A"

        expected_profit = project.get(
            "expected_profit"
        )

        total_cost = project.get(
            "total_cost"
        )

        expected_revenue = project.get(
            "expected_revenue"
        )

        status = str(
            project.get(
                "status"
            ) or "active"
        ).strip()

        if not status:
            status = "active"

        lines.append(
            f"- ID {project_id}: "
            f"{name} | "
            f"{industry} | "
            f"{location} | "
            f"status={status} | "
            f"cost={total_cost} | "
            f"revenue={expected_revenue} | "
            f"profit={expected_profit}"
        )

    if len(lines) == 1:
        return (
            "ACTIVE PROJECTS:\nNONE"
        )

    return "\n".join(
        lines
    )


# ============================================================
# 3.13 — BUILD BUSINESS CONTEXT
# ============================================================

def build_business_context(
    chat_id: Any
) -> str:

    sections = [

        build_memory_context(
            chat_id,
            limit=30,
        ),

        "",

        format_conversation_history(
            chat_id,
            limit=20,
        ),

        "",

        build_projects_context(
            chat_id,
            limit=20,
        ),

    ]

    return "\n".join(
        sections
    ).strip()


# ============================================================
# 3.14 — SAFE TEXT NORMALIZATION
# ============================================================

def clean_context_text(
    value: Any,
    max_length: int = 12000,
) -> str:

    try:
        max_length = int(
            max_length
        )
    except Exception:
        max_length = 12000

    max_length = max(
        1000,
        max_length,
    )

    if value is None:
        return ""

    text_value = str(
        value
    ).strip()

    if len(text_value) <= max_length:
        return text_value

    return (
        text_value[:max_length]
        + "\n"
        "[GENIOSA CONTEXT TRUNCATED]"
    )


# ============================================================
# 3.15 — FULL AI CONTEXT
# ============================================================

def build_full_ai_context(
    chat_id: Any
) -> str:

    context = build_business_context(
        chat_id
    )

    return clean_context_text(
        context,
        max_length=30000,
    )


# ============================================================
# 3.16 — PART 3 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 3/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 4/12
# Projects CRM — create, read, update, delete and context
# ============================================================


# ============================================================
# 4.1 — PROJECT CRM LIMITS
# ============================================================

MAX_PROJECT_RECORDS = 100


# ============================================================
# 4.2 — INTERNAL PROJECT HELPERS
# ============================================================

def _project_chat_id(
    chat_id: Any
) -> str:

    if chat_id is None:
        return ""

    return str(
        chat_id
    ).strip()


def _project_id(
    project_id: Any
) -> Optional[int]:

    try:
        value = int(
            project_id
        )
    except Exception:
        return None

    if value <= 0:
        return None

    return value


def _optional_project_text(
    value: Any
) -> Optional[str]:

    if value is None:
        return None

    text_value = str(
        value
    ).strip()

    return text_value or None


def _optional_project_number(
    value: Any
) -> Optional[float]:

    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    try:
        number = float(
            value
        )
    except Exception:
        return None

    if number != number:
        return None

    if number in (
        float("inf"),
        float("-inf"),
    ):
        return None

    return number


# ============================================================
# 4.3 — CREATE PROJECT
# ============================================================

def create_project(
    chat_id: Any,
    name: str,
    industry: Optional[str] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    land_area: Optional[float] = None,
    saleable_area: Optional[float] = None,
    construction_area: Optional[float] = None,
    total_area: Optional[float] = None,
    land_cost: Optional[float] = None,
    construction_cost: Optional[float] = None,
    operating_cost: Optional[float] = None,
    financing_cost: Optional[float] = None,
    other_cost: Optional[float] = None,
    total_cost: Optional[float] = None,
    revenue: Optional[float] = None,
    expected_revenue: Optional[float] = None,
    net_profit: Optional[float] = None,
    expected_profit: Optional[float] = None,
    investor_capital: Optional[float] = None,
    investor_profit: Optional[float] = None,
    investor_share: Optional[float] = None,
    notes: Optional[str] = None,
    status: str = "active",
) -> Optional[int]:

    """
    Create a new project in the CRM.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        logger.warning(
            "create_project called without chat_id"
        )
        return None

    project_name = str(
        name or ""
    ).strip()

    if not project_name:
        return None

    normalized_industry = (
        _optional_project_text(
            industry
        )
    )

    normalized_location = (
        _optional_project_text(
            location
        )
    )

    normalized_description = (
        _optional_project_text(
            description
        )
    )

    normalized_notes = (
        _optional_project_text(
            notes
        )
    )

    normalized_status = (
        normalize_project_status(
            status
        )
    )

    # --------------------------------------------------------
    # Numeric normalization
    # --------------------------------------------------------

    numeric_fields = {
        "land_area": land_area,
        "saleable_area": saleable_area,
        "construction_area": construction_area,
        "total_area": total_area,
        "land_cost": land_cost,
        "construction_cost": construction_cost,
        "operating_cost": operating_cost,
        "financing_cost": financing_cost,
        "other_cost": other_cost,
        "total_cost": total_cost,
        "revenue": revenue,
        "expected_revenue": expected_revenue,
        "net_profit": net_profit,
        "expected_profit": expected_profit,
        "investor_capital": investor_capital,
        "investor_profit": investor_profit,
        "investor_share": investor_share,
    }

    normalized_numbers = {}

    for field_name, field_value in numeric_fields.items():

        normalized_numbers[field_name] = (
            _optional_project_number(
                field_value
            )
        )

    # --------------------------------------------------------
    # Investor share validation
    # --------------------------------------------------------

    investor_share_value = (
        normalized_numbers.get(
            "investor_share"
        )
    )

    if investor_share_value is not None:

        if not 0 <= investor_share_value <= 100:

            logger.warning(
                "Invalid investor_share=%s for project '%s'",
                investor_share_value,
                project_name,
            )

            return None

    try:

        result = db_execute(
            """
            INSERT INTO projects (
                chat_id,
                name,
                industry,
                location,
                description,
                land_area,
                saleable_area,
                construction_area,
                total_area,
                land_cost,
                construction_cost,
                operating_cost,
                financing_cost,
                other_cost,
                total_cost,
                revenue,
                expected_revenue,
                net_profit,
                expected_profit,
                investor_capital,
                investor_profit,
                investor_share,
                notes,
                status
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s
            )
            RETURNING id
            """,
            (
                normalized_chat_id,
                project_name,
                normalized_industry,
                normalized_location,
                normalized_description,

                normalized_numbers["land_area"],
                normalized_numbers["saleable_area"],
                normalized_numbers["construction_area"],
                normalized_numbers["total_area"],

                normalized_numbers["land_cost"],
                normalized_numbers["construction_cost"],
                normalized_numbers["operating_cost"],
                normalized_numbers["financing_cost"],
                normalized_numbers["other_cost"],
                normalized_numbers["total_cost"],

                normalized_numbers["revenue"],
                normalized_numbers["expected_revenue"],

                normalized_numbers["net_profit"],
                normalized_numbers["expected_profit"],

                normalized_numbers["investor_capital"],
                normalized_numbers["investor_profit"],
                normalized_numbers["investor_share"],

                normalized_notes,
                normalized_status,
            ),
            fetchone=True,
            commit=True,
        )

        if not result:
            return None

        project_id = result.get(
            "id"
        )

        if project_id is None:
            return None

        return int(
            project_id
        )

    except Exception as exc:

        logger.error(
            "Failed to create project: %s",
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 4.4 — GET PROJECTS
# ============================================================

def get_projects(
    chat_id: Any,
    limit: int = 50,
) -> List[Dict[str, Any]]:

    """
    Return projects belonging to the current chat.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return []

    try:

        safe_limit = _safe_limit(
            limit,
            default=50,
            minimum=1,
            maximum=MAX_PROJECT_RECORDS,
        )

    except Exception:

        safe_limit = 50

    try:

        rows = db_execute(
            f"""
            SELECT *
            FROM projects
            WHERE chat_id = %s
            ORDER BY
                updated_at DESC NULLS LAST,
                id DESC
            LIMIT {safe_limit}
            """,
            (
                normalized_chat_id,
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get projects: %s",
            exc,
            exc_info=True,
        )

        return []


# ============================================================
# 4.5 — GET SINGLE PROJECT
# ============================================================

def get_project(
    chat_id: Any,
    project_id: int,
) -> Optional[Dict[str, Any]]:

    """
    Return one project belonging to the current chat.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    normalized_project_id = _project_id(
        project_id
    )

    if not normalized_chat_id:
        return None

    if normalized_project_id is None:
        return None

    try:

        result = db_execute(
            """
            SELECT *
            FROM projects
            WHERE id = %s
              AND chat_id = %s
            LIMIT 1
            """,
            (
                normalized_project_id,
                normalized_chat_id,
            ),
            fetchone=True,
        )

        return result

    except Exception as exc:

        logger.error(
            "Failed to get project %s: %s",
            normalized_project_id,
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 4.6 — FIND PROJECT BY NAME
# ============================================================

def find_project_by_name(
    chat_id: Any,
    name: str,
) -> Optional[Dict[str, Any]]:

    """
    Find a project using a partial name match.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    search_name = str(
        name or ""
    ).strip()

    if not normalized_chat_id:
        return None

    if not search_name:
        return None

    try:

        result = db_execute(
            """
            SELECT *
            FROM projects
            WHERE chat_id = %s
              AND name ILIKE %s
            ORDER BY
                updated_at DESC NULLS LAST,
                id DESC
            LIMIT 1
            """,
            (
                normalized_chat_id,
                f"%{search_name}%",
            ),
            fetchone=True,
        )

        return result

    except Exception as exc:

        logger.error(
            "Failed to find project '%s': %s",
            search_name,
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 4.7 — UPDATE PROJECT
# ============================================================

def update_project(
    chat_id: Any,
    project_id: int,
    **fields,
) -> bool:

    """
    Update allowed project fields.

    Only explicitly whitelisted columns can be modified.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    normalized_project_id = _project_id(
        project_id
    )

    if not normalized_chat_id:
        return False

    if normalized_project_id is None:
        return False

    allowed_fields = {

        "name",

        "industry",

        "location",

        "description",

        "land_area",

        "saleable_area",

        "construction_area",

        "total_area",

        "land_cost",

        "construction_cost",

        "operating_cost",

        "financing_cost",

        "other_cost",

        "total_cost",

        "revenue",

        "expected_revenue",

        "net_profit",

        "expected_profit",

        "investor_capital",

        "investor_profit",

        "investor_share",

        "notes",

        "status",

    }

    updates = []
    values = []

    for field_name, field_value in fields.items():

        if field_name not in allowed_fields:
            continue

        # ----------------------------------------------------
        # Text fields
        # ----------------------------------------------------

        if field_name in {
            "name",
            "industry",
            "location",
            "description",
            "notes",
        }:

            if field_name == "name":

                normalized_value = str(
                    field_value or ""
                ).strip()

                if not normalized_value:
                    continue

                field_value = normalized_value

            else:

                field_value = (
                    _optional_project_text(
                        field_value
                    )
                )

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        elif field_name == "status":

            field_value = (
                normalize_project_status(
                    field_value
                )
            )

        # ----------------------------------------------------
        # Numeric fields
        # ----------------------------------------------------

        else:

            field_value = (
                _optional_project_number(
                    field_value
                )
            )

        # ----------------------------------------------------
        # Investor share
        # ----------------------------------------------------

        if field_name == "investor_share":

            if field_value is not None:

                if not 0 <= field_value <= 100:

                    logger.warning(
                        "Invalid investor_share=%s "
                        "for project %s",
                        field_value,
                        normalized_project_id,
                    )

                    return False

        updates.append(
            f"{field_name} = %s"
        )

        values.append(
            field_value
        )

    if not updates:
        return False

    updates.append(
        "updated_at = CURRENT_TIMESTAMP"
    )

    values.extend([
        normalized_project_id,
        normalized_chat_id,
    ])

    query = f"""
        UPDATE projects
        SET {", ".join(updates)}
        WHERE id = %s
          AND chat_id = %s
        RETURNING id
    """

    try:

        result = db_execute(
            query,
            tuple(values),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to update project %s: %s",
            normalized_project_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 4.8 — DELETE PROJECT
# ============================================================

def delete_project(
    chat_id: Any,
    project_id: int,
) -> bool:

    """
    Delete one project belonging to the current chat.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    normalized_project_id = _project_id(
        project_id
    )

    if not normalized_chat_id:
        return False

    if normalized_project_id is None:
        return False

    try:

        result = db_execute(
            """
            DELETE FROM projects
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                normalized_project_id,
                normalized_chat_id,
            ),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to delete project %s: %s",
            normalized_project_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 4.9 — PROJECT SUMMARY
# ============================================================

def project_summary(
    project: Optional[Dict[str, Any]]
) -> str:

    """
    Convert a project record into a readable Telegram summary.
    """

    if not project:
        return "❌ პროექტი ვერ მოიძებნა."

    project_id = project.get(
        "id"
    )

    name = str(
        project.get(
            "name"
        ) or "უსახელო პროექტ"
    ).strip()

    industry = industry_name(
        project.get(
            "industry"
        )
    )

    location = str(
        project.get(
            "location"
        ) or "არ არის მითითებული"
    ).strip()

    description = str(
        project.get(
            "description"
        ) or ""
    ).strip()

    status = normalize_project_status(
        project.get(
            "status"
        )
    )

    lines = [

        f"🏗️ პროექტი #{project_id}",

        f"📌 სახელი: {name}",

        f"🏢 სფერო: {industry}",

        f"📍 მდებარეობა: {location}",

        f"📊 სტატუსი: {status}",

    ]

    if description:

        lines.extend([
            "",
            "📝 აღწერა:",
            description,
        ])

    # --------------------------------------------------------
    # Areas
    # --------------------------------------------------------

    area_lines = []

    area_fields = [

        (
            "land_area",
            "🌐 მიწის ფართობი",
            "მ²",
        ),

        (
            "construction_area",
            "🏗️ სამშენებლო ფართობი",
            "მ²",
        ),

        (
            "saleable_area",
            "🏷️ გასაყიდი ფართობი",
            "მ²",
        ),

        (
            "total_area",
            "📐 სრული ფართობი",
            "მ²",
        ),

    ]

    for field_name, label, unit in area_fields:

        value = project.get(
            field_name
        )

        if value is None:
            continue

        try:

            display_value = format_number(
                value
            )

        except Exception:

            display_value = str(
                value
            )

        area_lines.append(
            f"{label}: "
            f"{display_value} {unit}"
        )

    if area_lines:

        lines.extend([
            "",
            "📐 ფართობები:",
            *area_lines,
        ])

    # --------------------------------------------------------
    # Financial information
    # --------------------------------------------------------

    financial_fields = [

        (
            "land_cost",
            "🌐 მიწის ღირებულება",
        ),

        (
            "construction_cost",
            "🏗️ მშენებლობის ღირებულება",
        ),

        (
            "operating_cost",
            "⚙️ საოპერაციო ხარჯი",
        ),

        (
            "financing_cost",
            "🏦 დაფინანსების ხარჯი",
        ),

        (
            "other_cost",
            "📦 სხვა ხარჯი",
        ),

        (
            "total_cost",
            "💰 სრული ღირებულება",
        ),

        (
            "expected_revenue",
            "📈 მოსალოდნელი შემოსავალი",
        ),

        (
            "expected_profit",
            "💵 მოსალოდნელი მოგება",
        ),

    ]

    financial_lines = []

    for field_name, label in financial_fields:

        value = project.get(
            field_name
        )

        if value is None:
            continue

        try:

            display_value = format_money(
                value
            )

        except Exception:

            display_value = str(
                value
            )

        financial_lines.append(
            f"{label}: "
            f"{display_value}"
        )

    if financial_lines:

        lines.extend([
            "",
            "💰 ფინანსები:",
            *financial_lines,
        ])

    # --------------------------------------------------------
    # Investor structure
    # --------------------------------------------------------

    investor_lines = []

    investor_fields = [

        (
            "investor_capital",
            "💼 ინვესტორის კაპიტალი",
            "money",
        ),

        (
            "investor_profit",
            "💵 ინვესტორის მოგება",
            "money",
        ),

        (
            "investor_share",
            "📊 ინვესტორის წილი",
            "percent",
        ),

    ]

    for field_name, label, value_type in investor_fields:

        value = project.get(
            field_name
        )

        if value is None:
            continue

        try:

            if value_type == "money":

                display_value = format_money(
                    value
                )

            else:

                display_value = (
                    f"{format_number(value)}%"
                )

        except Exception:

            display_value = str(
                value
            )

        investor_lines.append(
            f"{label}: {display_value}"
        )

    if investor_lines:

        lines.extend([
            "",
            "🤝 ინვესტიცია:",
            *investor_lines,
        ])

    # --------------------------------------------------------
    # Notes
    # --------------------------------------------------------

    notes = str(
        project.get(
            "notes"
        ) or ""
    ).strip()

    if notes:

        lines.extend([
            "",
            "📎 შენიშვნები:",
            notes,
        ])

    return "\n".join(
        lines
    )


# ============================================================
# 4.10 — PROJECTS SUMMARY
# ============================================================

def projects_summary(
    chat_id: Any
) -> str:

    """
    Compact project list for Telegram.
    """

    projects = get_projects(
        chat_id,
        limit=MAX_PROJECT_RECORDS,
    )

    if not projects:

        return (
            "🏗️ პროექტები ჯერ არ არის დამატებული."
        )

    lines = [
        "🏗️ GENIOSA — პროექტები",
        "",
    ]

    for project in projects:

        if not isinstance(
            project,
            dict,
        ):
            continue

        project_id = project.get(
            "id"
        )

        name = str(
            project.get(
                "name"
            ) or "უსახელო"
        ).strip()

        location = str(
            project.get(
                "location"
            ) or "—"
        ).strip()

        status = normalize_project_status(
            project.get(
                "status"
            )
        )

        expected_profit = project.get(
            "expected_profit"
        )

        if expected_profit is not None:

            try:

                profit_text = format_money(
                    expected_profit
                )

            except Exception:

                profit_text = str(
                    expected_profit
                )

        else:

            profit_text = "—"

        lines.append(
            f"#{project_id} — {name}"
        )

        lines.append(
            f"📍 {location} | "
            f"სტატუსი: {status}"
        )

        lines.append(
            f"💵 მოსალოდნელი მოგება: "
            f"{profit_text}"
        )

        lines.append("")

    if len(lines) <= 2:

        return (
            "🏗️ პროექტები ჯერ არ არის დამატებული."
        )

    return "\n".join(
        lines
    ).strip()


# ============================================================
# 4.11 — PROJECT DEAL COUNT
# ============================================================

def get_project_deal_count(
    chat_id: Any,
    project_id: int,
) -> int:

    """
    Count deals connected to a project.

    Full deal operations are implemented in PART 5.
    """

    normalized_chat_id = _project_chat_id(
        chat_id
    )

    normalized_project_id = _project_id(
        project_id
    )

    if not normalized_chat_id:
        return 0

    if normalized_project_id is None:
        return 0

    try:

        result = db_execute(
            """
            SELECT COUNT(*) AS total
            FROM deals
            WHERE chat_id = %s
              AND project_id = %s
            """,
            (
                normalized_chat_id,
                normalized_project_id,
            ),
            fetchone=True,
        )

        if not result:
            return 0

        try:

            return int(
                result.get(
                    "total",
                    0,
                ) or 0
            )

        except Exception:

            return 0

    except Exception as exc:

        logger.error(
            "Failed to count project deals: %s",
            exc,
            exc_info=True,
        )

        return 0


# ============================================================
# 4.12 — PROJECT CONTEXT
# ============================================================

def build_project_context(
    chat_id: Any,
    project_id: int,
) -> str:

    """
    Build detailed AI context for a specific project.
    """

    project = get_project(
        chat_id,
        project_id,
    )

    if not project:
        return "PROJECT NOT FOUND."

    context = project_to_ai_context(
        project
    )

    deal_count = get_project_deal_count(
        chat_id,
        project_id,
    )

    return (
        "SELECTED PROJECT:\n"
        f"{context}\n\n"
        f"CONNECTED DEALS: {deal_count}"
    )


# ============================================================
# 4.13 — PROJECT STATUS NORMALIZATION
# ============================================================

def normalize_project_status(
    value: Optional[str]
) -> str:

    """
    Normalize common project status values.
    """

    if value is None:
        return "active"

    text_value = str(
        value
    ).strip().lower()

    if not text_value:
        return "active"

    mapping = {

        "აქტიური": "active",

        "active": "active",

        "აქტიურია": "active",

        "დასრულებული": "completed",

        "completed": "completed",

        "finished": "completed",

        "დაპაუზებული": "paused",

        "paused": "paused",

        "გაჩერებული": "paused",

        "on hold": "paused",

        "დაგეგმილი": "planned",

        "planned": "planned",

        "ახალი": "new",

        "new": "new",

    }

    return mapping.get(
        text_value,
        text_value,
    )


# ============================================================
# 4.14 — PROJECT TOTAL COST
# ============================================================

def calculate_project_total_cost(
    project: Optional[Dict[str, Any]]
) -> float:

    """
    Calculate total project cost.

    If total_cost is explicitly stored and valid,
    it is treated as the authoritative value.
    Otherwise cost components are summed.
    """

    if not project:
        return 0.0

    existing_total = project.get(
        "total_cost"
    )

    if existing_total is not None:

        try:

            total_value = float(
                existing_total
            )

            if total_value == total_value:

                return total_value

        except Exception:

            pass

    fields = [

        "land_cost",

        "construction_cost",

        "operating_cost",

        "financing_cost",

        "other_cost",

    ]

    total = 0.0

    for field_name in fields:

        value = project.get(
            field_name
        )

        if value is None:
            continue

        try:

            number = float(
                value
            )

            if number != number:
                continue

            total += number

        except Exception:

            continue

    return total


# ============================================================
# 4.15 — CALCULATE PROJECT REVENUE
# ============================================================

def calculate_project_revenue(
    project: Optional[Dict[str, Any]]
) -> float:

    """
    Return expected project revenue.
    """

    if not project:
        return 0.0

    revenue = project.get(
        "expected_revenue"
    )

    if revenue is None:

        revenue = project.get(
            "revenue"
        )

    if revenue is None:
        return 0.0

    try:

        value = float(
            revenue
        )

        if value != value:
            return 0.0

        return value

    except Exception:

        return 0.0


# ============================================================
# 4.16 — CALCULATE PROJECT PROFIT
# ============================================================

def calculate_project_profit(
    project: Optional[Dict[str, Any]]
) -> float:

    """
    Calculate project profit from expected revenue
    minus total cost.

    If expected_profit is explicitly stored and valid,
    it is treated as the authoritative value.
    """

    if not project:
        return 0.0

    existing_profit = project.get(
        "expected_profit"
    )

    if existing_profit is not None:

        try:

            profit_value = float(
                existing_profit
            )

            if profit_value == profit_value:

                return profit_value

        except Exception:

            pass

    revenue = calculate_project_revenue(
        project
    )

    total_cost = calculate_project_total_cost(
        project
    )

    return revenue - total_cost


# ============================================================
# 4.17 — PROJECT PROFIT MARGIN
# ============================================================

def calculate_project_margin(
    project: Optional[Dict[str, Any]]
) -> float:

    """
    Calculate expected profit margin as a percentage.
    """

    if not project:
        return 0.0

    revenue = calculate_project_revenue(
        project
    )

    if revenue == 0:
        return 0.0

    profit = calculate_project_profit(
        project
    )

    return (
        profit / revenue
    ) * 100.0


# ============================================================
# 4.18 — PART 4 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 4/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 5/12
# Investors & Deals CRM
# ============================================================


# ============================================================
# 5.1 — CRM LIMITS
# ============================================================

MAX_INVESTOR_RECORDS = 100
MAX_DEAL_RECORDS = 200


# ============================================================
# 5.2 — INTERNAL CRM HELPERS
# ============================================================

def _crm_chat_id(
    chat_id: Any
) -> str:

    if chat_id is None:
        return ""

    return str(
        chat_id
    ).strip()


def _crm_record_id(
    record_id: Any
) -> Optional[int]:

    try:
        value = int(
            record_id
        )
    except Exception:
        return None

    if value <= 0:
        return None

    return value


def _crm_optional_text(
    value: Any
) -> Optional[str]:

    if value is None:
        return None

    text_value = str(
        value
    ).strip()

    return text_value or None


def _crm_optional_number(
    value: Any
) -> Optional[float]:

    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    try:
        number = float(
            value
        )
    except Exception:
        return None

    if number != number:
        return None

    if number in (
        float("inf"),
        float("-inf"),
    ):
        return None

    return number


def _crm_safe_limit(
    value: Any,
    default: int,
    maximum: int,
) -> int:

    try:
        value = int(
            value
        )
    except Exception:
        value = default

    return max(
        1,
        min(
            value,
            maximum,
        )
    )


# ============================================================
# 5.3 — CREATE INVESTOR
# ============================================================

def create_investor(
    chat_id: Any,
    name: str,
    company: Optional[str] = None,
    country: Optional[str] = None,
    contact: Optional[str] = None,
    investment_capacity: Optional[float] = None,
    preferred_sector: Optional[str] = None,
    status: str = "new",
    notes: Optional[str] = None,
) -> Optional[int]:

    """
    Create a new investor in the CRM.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return None

    investor_name = str(
        name or ""
    ).strip()

    if not investor_name:
        return None

    company = _crm_optional_text(
        company
    )

    country = _crm_optional_text(
        country
    )

    contact = _crm_optional_text(
        contact
    )

    preferred_sector = _crm_optional_text(
        preferred_sector
    )

    notes = _crm_optional_text(
        notes
    )

    status = str(
        status or "new"
    ).strip().lower()

    if not status:
        status = "new"

    investment_capacity = (
        _crm_optional_number(
            investment_capacity
        )
    )

    if (
        investment_capacity is not None
        and investment_capacity < 0
    ):
        return None

    try:

        result = db_execute(
            """
            INSERT INTO investors (
                chat_id,
                name,
                company,
                country,
                contact,
                investment_capacity,
                preferred_sector,
                status,
                notes
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                normalized_chat_id,
                investor_name,
                company,
                country,
                contact,
                investment_capacity,
                preferred_sector,
                status,
                notes,
            ),
            fetchone=True,
            commit=True,
        )

        if not result:
            return None

        investor_id = result.get(
            "id"
        )

        if investor_id is None:
            return None

        return int(
            investor_id
        )

    except Exception as exc:

        logger.error(
            "Failed to create investor: %s",
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 5.4 — GET INVESTORS
# ============================================================

def get_investors(
    chat_id: Any,
    limit: int = 100,
) -> List[Dict[str, Any]]:

    """
    Return investors belonging to the current chat.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return []

    safe_limit = _crm_safe_limit(
        limit,
        default=100,
        maximum=MAX_INVESTOR_RECORDS,
    )

    try:

        rows = db_execute(
            f"""
            SELECT *
            FROM investors
            WHERE chat_id = %s
            ORDER BY
                updated_at DESC NULLS LAST,
                id DESC
            LIMIT {safe_limit}
            """,
            (
                normalized_chat_id,
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get investors: %s",
            exc,
            exc_info=True,
        )

        return []


# ============================================================
# 5.5 — GET SINGLE INVESTOR
# ============================================================

def get_investor(
    chat_id: Any,
    investor_id: int,
) -> Optional[Dict[str, Any]]:

    """
    Return one investor belonging to the current chat.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_investor_id = _crm_record_id(
        investor_id
    )

    if not normalized_chat_id:
        return None

    if normalized_investor_id is None:
        return None

    try:

        return db_execute(
            """
            SELECT *
            FROM investors
            WHERE id = %s
              AND chat_id = %s
            LIMIT 1
            """,
            (
                normalized_investor_id,
                normalized_chat_id,
            ),
            fetchone=True,
        )

    except Exception as exc:

        logger.error(
            "Failed to get investor %s: %s",
            normalized_investor_id,
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 5.6 — FIND INVESTOR
# ============================================================

def find_investor(
    chat_id: Any,
    search_text: str,
) -> Optional[Dict[str, Any]]:

    """
    Search investor by name, company, country or contact.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    value = str(
        search_text or ""
    ).strip()

    if not normalized_chat_id:
        return None

    if not value:
        return None

    pattern = f"%{value}%"

    try:

        return db_execute(
            """
            SELECT *
            FROM investors
            WHERE chat_id = %s
              AND (
                    name ILIKE %s
                    OR company ILIKE %s
                    OR country ILIKE %s
                    OR contact ILIKE %s
                  )
            ORDER BY
                updated_at DESC NULLS LAST,
                id DESC
            LIMIT 1
            """,
            (
                normalized_chat_id,
                pattern,
                pattern,
                pattern,
            ),
            fetchone=True,
        )

    except Exception as exc:

        logger.error(
            "Failed to find investor '%s': %s",
            value,
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 5.7 — UPDATE INVESTOR
# ============================================================

def update_investor(
    chat_id: Any,
    investor_id: int,
    **fields,
) -> bool:

    """
    Update allowed investor fields.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_investor_id = _crm_record_id(
        investor_id
    )

    if not normalized_chat_id:
        return False

    if normalized_investor_id is None:
        return False

    allowed_fields = {
        "name",
        "company",
        "country",
        "contact",
        "investment_capacity",
        "preferred_sector",
        "status",
        "notes",
    }

    updates = []
    values = []

    for field_name, field_value in fields.items():

        if field_name not in allowed_fields:
            continue

        if field_name == "name":

            field_value = str(
                field_value or ""
            ).strip()

            if not field_value:
                continue

        elif field_name in {
            "company",
            "country",
            "contact",
            "preferred_sector",
            "notes",
        }:

            field_value = _crm_optional_text(
                field_value
            )

        elif field_name == "investment_capacity":

            field_value = _crm_optional_number(
                field_value
            )

            if (
                field_value is not None
                and field_value < 0
            ):
                return False

        elif field_name == "status":

            field_value = str(
                field_value or "new"
            ).strip().lower()

            if not field_value:
                field_value = "new"

        updates.append(
            f"{field_name} = %s"
        )

        values.append(
            field_value
        )

    if not updates:
        return False

    updates.append(
        "updated_at = CURRENT_TIMESTAMP"
    )

    values.extend([
        normalized_investor_id,
        normalized_chat_id,
    ])

    query = f"""
        UPDATE investors
        SET {", ".join(updates)}
        WHERE id = %s
          AND chat_id = %s
        RETURNING id
    """

    try:

        result = db_execute(
            query,
            tuple(values),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to update investor %s: %s",
            normalized_investor_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 5.8 — DELETE INVESTOR
# ============================================================

def delete_investor(
    chat_id: Any,
    investor_id: int,
) -> bool:

    """
    Delete one investor belonging to the current chat.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_investor_id = _crm_record_id(
        investor_id
    )

    if not normalized_chat_id:
        return False

    if normalized_investor_id is None:
        return False

    try:

        result = db_execute(
            """
            DELETE FROM investors
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                normalized_investor_id,
                normalized_chat_id,
            ),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to delete investor %s: %s",
            normalized_investor_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 5.9 — FORMAT INVESTOR
# ============================================================

def format_investor(
    investor: Optional[Dict[str, Any]]
) -> str:

    """
    Format an investor for Telegram.
    """

    if not investor:
        return "❌ ინვესტორი ვერ მოიძებნა."

    investor_id = investor.get(
        "id"
    )

    name = str(
        investor.get(
            "name"
        ) or "უცნობი"
    ).strip()

    company = _crm_optional_text(
        investor.get(
            "company"
        )
    )

    country = _crm_optional_text(
        investor.get(
            "country"
        )
    )

    contact = _crm_optional_text(
        investor.get(
            "contact"
        )
    )

    capacity = investor.get(
        "investment_capacity"
    )

    sector = _crm_optional_text(
        investor.get(
            "preferred_sector"
        )
    )

    status = str(
        investor.get(
            "status"
        ) or "new"
    ).strip()

    notes = _crm_optional_text(
        investor.get(
            "notes"
        )
    )

    lines = [
        f"👤 ინვესტორი #{investor_id}",
        f"📌 სახელი: {name}",
    ]

    if company:
        lines.append(
            f"🏢 კომპანია: {company}"
        )

    if country:
        lines.append(
            f"🌍 ქვეყანა: {country}"
        )

    if contact:
        lines.append(
            f"📞 კონტაქტი: {contact}"
        )

    if capacity is not None:

        try:

            capacity_text = format_money(
                capacity
            )

        except Exception:

            capacity_text = str(
                capacity
            )

        lines.append(
            f"💰 საინვესტიციო შესაძლებლობა: "
            f"{capacity_text}"
        )

    if sector:
        lines.append(
            f"🏷️ სასურველი სექტორი: {sector}"
        )

    lines.append(
        f"📊 სტატუსი: {status}"
    )

    if notes:

        lines.extend([
            "",
            "📎 შენიშვნები:",
            notes,
        ])

    return "\n".join(
        lines
    )


# ============================================================
# 5.10 — INVESTORS SUMMARY
# ============================================================

def investors_summary(
    chat_id: Any
) -> str:

    """
    Compact investor list.
    """

    investors = get_investors(
        chat_id,
        limit=MAX_INVESTOR_RECORDS,
    )

    if not investors:

        return (
            "👤 ინვესტორები ჯერ არ არის დამატებული."
        )

    lines = [
        "👤 GENIOSA — ინვესტორები",
        "",
    ]

    for investor in investors:

        if not isinstance(
            investor,
            dict,
        ):
            continue

        investor_id = investor.get(
            "id"
        )

        name = investor.get(
            "name"
        ) or "უცნობი"

        company = investor.get(
            "company"
        ) or "—"

        country = investor.get(
            "country"
        ) or "—"

        status = investor.get(
            "status"
        ) or "new"

        capacity = investor.get(
            "investment_capacity"
        )

        if capacity is not None:

            try:

                capacity_text = format_money(
                    capacity
                )

            except Exception:

                capacity_text = str(
                    capacity
                )

        else:

            capacity_text = "—"

        lines.append(
            f"#{investor_id} — {name}"
        )

        lines.append(
            f"🏢 {company} | "
            f"🌍 {country}"
        )

        lines.append(
            f"📊 {status} | "
            f"💰 {capacity_text}"
        )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ============================================================
# 5.11 — CREATE DEAL
# ============================================================

def create_deal(
    chat_id: Any,
    project_id: Optional[int] = None,
    investor_id: Optional[int] = None,
    stage: str = "new",
    proposed_amount: Optional[float] = None,
    proposed_share: Optional[float] = None,
    valuation: Optional[float] = None,
    notes: Optional[str] = None,
    next_step: Optional[str] = None,
) -> Optional[int]:

    """
    Create a deal connecting a project and/or investor.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return None

    project_id_value = None
    investor_id_value = None

    # --------------------------------------------------------
    # Validate project
    # --------------------------------------------------------

    if project_id is not None:

        project_id_value = _crm_record_id(
            project_id
        )

        if project_id_value is None:
            return None

        project = get_project(
            normalized_chat_id,
            project_id_value,
        )

        if not project:
            return None

    # --------------------------------------------------------
    # Validate investor
    # --------------------------------------------------------

    if investor_id is not None:

        investor_id_value = _crm_record_id(
            investor_id
        )

        if investor_id_value is None:
            return None

        investor = get_investor(
            normalized_chat_id,
            investor_id_value,
        )

        if not investor:
            return None

    # --------------------------------------------------------
    # Normalize numeric values
    # --------------------------------------------------------

    proposed_amount = _crm_optional_number(
        proposed_amount
    )

    proposed_share = _crm_optional_number(
        proposed_share
    )

    valuation = _crm_optional_number(
        valuation
    )

    if (
        proposed_amount is not None
        and proposed_amount < 0
    ):
        return None

    if (
        proposed_share is not None
        and not 0 <= proposed_share <= 100
    ):
        return None

    if (
        valuation is not None
        and valuation < 0
    ):
        return None

    stage = str(
        stage or "new"
    ).strip().lower()

    if not stage:
        stage = "new"

    notes = _crm_optional_text(
        notes
    )

    next_step = _crm_optional_text(
        next_step
    )

    try:

        result = db_execute(
            """
            INSERT INTO deals (
                chat_id,
                project_id,
                investor_id,
                stage,
                proposed_amount,
                proposed_share,
                valuation,
                notes,
                next_step
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            RETURNING id
            """,
            (
                normalized_chat_id,
                project_id_value,
                investor_id_value,
                stage,
                proposed_amount,
                proposed_share,
                valuation,
                notes,
                next_step,
            ),
            fetchone=True,
            commit=True,
        )

        if not result:
            return None

        deal_id = result.get(
            "id"
        )

        if deal_id is None:
            return None

        return int(
            deal_id
        )

    except Exception as exc:

        logger.error(
            "Failed to create deal: %s",
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 5.12 — GET DEALS
# ============================================================

def get_deals(
    chat_id: Any,
    limit: int = 100,
) -> List[Dict[str, Any]]:

    """
    Return deals with related project and investor names.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    if not normalized_chat_id:
        return []

    safe_limit = _crm_safe_limit(
        limit,
        default=100,
        maximum=MAX_DEAL_RECORDS,
    )

    try:

        rows = db_execute(
            f"""
            SELECT
                d.*,
                p.name AS project_name,
                i.name AS investor_name,
                i.company AS investor_company
            FROM deals d
            LEFT JOIN projects p
                ON p.id = d.project_id
               AND p.chat_id = d.chat_id
            LEFT JOIN investors i
                ON i.id = d.investor_id
               AND i.chat_id = d.chat_id
            WHERE d.chat_id = %s
            ORDER BY
                d.updated_at DESC NULLS LAST,
                d.id DESC
            LIMIT {safe_limit}
            """,
            (
                normalized_chat_id,
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get deals: %s",
            exc,
            exc_info=True,
        )

        return []


# ============================================================
# 5.13 — GET SINGLE DEAL
# ============================================================

def get_deal(
    chat_id: Any,
    deal_id: int,
) -> Optional[Dict[str, Any]]:

    """
    Return one deal with related CRM data.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_deal_id = _crm_record_id(
        deal_id
    )

    if not normalized_chat_id:
        return None

    if normalized_deal_id is None:
        return None

    try:

        return db_execute(
            """
            SELECT
                d.*,
                p.name AS project_name,
                i.name AS investor_name,
                i.company AS investor_company
            FROM deals d
            LEFT JOIN projects p
                ON p.id = d.project_id
               AND p.chat_id = d.chat_id
            LEFT JOIN investors i
                ON i.id = d.investor_id
               AND i.chat_id = d.chat_id
            WHERE d.id = %s
              AND d.chat_id = %s
            LIMIT 1
            """,
            (
                normalized_deal_id,
                normalized_chat_id,
            ),
            fetchone=True,
        )

    except Exception as exc:

        logger.error(
            "Failed to get deal %s: %s",
            normalized_deal_id,
            exc,
            exc_info=True,
        )

        return None


# ============================================================
# 5.14 — UPDATE DEAL
# ============================================================

def update_deal(
    chat_id: Any,
    deal_id: int,
    **fields,
) -> bool:

    """
    Update allowed deal fields.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_deal_id = _crm_record_id(
        deal_id
    )

    if not normalized_chat_id:
        return False

    if normalized_deal_id is None:
        return False

    allowed_fields = {
        "project_id",
        "investor_id",
        "stage",
        "proposed_amount",
        "proposed_share",
        "valuation",
        "notes",
        "next_step",
    }

    updates = []
    values = []

    for field_name, field_value in fields.items():

        if field_name not in allowed_fields:
            continue

        if field_name == "project_id":

            if field_value is not None:

                normalized_project_id = (
                    _crm_record_id(
                        field_value
                    )
                )

                if normalized_project_id is None:
                    continue

                if not get_project(
                    normalized_chat_id,
                    normalized_project_id,
                ):
                    continue

                field_value = normalized_project_id

        elif field_name == "investor_id":

            if field_value is not None:

                normalized_investor_id = (
                    _crm_record_id(
                        field_value
                    )
                )

                if normalized_investor_id is None:
                    continue

                if not get_investor(
                    normalized_chat_id,
                    normalized_investor_id,
                ):
                    continue

                field_value = normalized_investor_id

        elif field_name == "stage":

            field_value = str(
                field_value or "new"
            ).strip().lower()

            if not field_value:
                field_value = "new"

        elif field_name in {
            "proposed_amount",
            "proposed_share",
            "valuation",
        }:

            field_value = _crm_optional_number(
                field_value
            )

            if field_name == "proposed_amount":

                if (
                    field_value is not None
                    and field_value < 0
                ):
                    return False

            if field_name == "proposed_share":

                if (
                    field_value is not None
                    and not 0 <= field_value <= 100
                ):
                    return False

            if field_name == "valuation":

                if (
                    field_value is not None
                    and field_value < 0
                ):
                    return False

        elif field_name in {
            "notes",
            "next_step",
        }:

            field_value = _crm_optional_text(
                field_value
            )

        updates.append(
            f"{field_name} = %s"
        )

        values.append(
            field_value
        )

    if not updates:
        return False

    updates.append(
        "updated_at = CURRENT_TIMESTAMP"
    )

    values.extend([
        normalized_deal_id,
        normalized_chat_id,
    ])

    query = f"""
        UPDATE deals
        SET {", ".join(updates)}
        WHERE id = %s
          AND chat_id = %s
        RETURNING id
    """

    try:

        result = db_execute(
            query,
            tuple(values),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to update deal %s: %s",
            normalized_deal_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 5.15 — DELETE DEAL
# ============================================================

def delete_deal(
    chat_id: Any,
    deal_id: int,
) -> bool:

    """
    Delete one deal belonging to the current chat.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_deal_id = _crm_record_id(
        deal_id
    )

    if not normalized_chat_id:
        return False

    if normalized_deal_id is None:
        return False

    try:

        result = db_execute(
            """
            DELETE FROM deals
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                normalized_deal_id,
                normalized_chat_id,
            ),
            fetchone=True,
            commit=True,
        )

        return bool(
            result
        )

    except Exception as exc:

        logger.error(
            "Failed to delete deal %s: %s",
            normalized_deal_id,
            exc,
            exc_info=True,
        )

        return False


# ============================================================
# 5.16 — FORMAT DEAL
# ============================================================

def format_deal(
    deal: Optional[Dict[str, Any]]
) -> str:

    """
    Format a deal for Telegram.
    """

    if not deal:
        return "❌ გარიგება ვერ მოიძებნა."

    deal_id = deal.get(
        "id"
    )

    project_name = deal.get(
        "project_name"
    ) or "არ არის დაკავშირებული"

    investor_name = deal.get(
        "investor_name"
    ) or "არ არის დაკავშირებული"

    investor_company = deal.get(
        "investor_company"
    )

    stage = deal.get(
        "stage"
    ) or "new"

    proposed_amount = deal.get(
        "proposed_amount"
    )

    proposed_share = deal.get(
        "proposed_share"
    )

    valuation = deal.get(
        "valuation"
    )

    notes = deal.get(
        "notes"
    )

    next_step = deal.get(
        "next_step"
    )

    lines = [
        f"🤝 გარიგება #{deal_id}",
        f"🏗️ პროექტი: {project_name}",
        f"👤 ინვესტორი: {investor_name}",
    ]

    if investor_company:

        lines.append(
            f"🏢 კომპანია: {investor_company}"
        )

    lines.append(
        f"📊 ეტაპი: {stage}"
    )

    if proposed_amount is not None:

        try:

            amount_text = format_money(
                proposed_amount
            )

        except Exception:

            amount_text = str(
                proposed_amount
            )

        lines.append(
            f"💰 შეთავაზებული თანხა: "
            f"{amount_text}"
        )

    if proposed_share is not None:

        try:

            share_text = format_number(
                proposed_share
            )

        except Exception:

            share_text = str(
                proposed_share
            )

        lines.append(
            f"📈 შეთავაზებული წილი: "
            f"{share_text}%"
        )

    if valuation is not None:

        try:

            valuation_text = format_money(
                valuation
            )

        except Exception:

            valuation_text = str(
                valuation
            )

        lines.append(
            f"🏷️ შეფასება: "
            f"{valuation_text}"
        )

    if next_step:

        lines.extend([
            "",
            f"➡️ შემდეგი ნაბიჯი: {next_step}",
        ])

    if notes:

        lines.extend([
            "",
            "📎 შენიშვნები:",
            str(notes),
        ])

    return "\n".join(
        lines
    )


# ============================================================
# 5.17 — DEALS SUMMARY
# ============================================================

def deals_summary(
    chat_id: Any
) -> str:

    """
    Compact deal pipeline.
    """

    deals = get_deals(
        chat_id,
        limit=MAX_DEAL_RECORDS,
    )

    if not deals:

        return (
            "🤝 გარიგებები ჯერ არ არის დამატებული."
        )

    lines = [
        "🤝 GENIOSA — გარიგებები",
        "",
    ]

    for deal in deals:

        if not isinstance(
            deal,
            dict,
        ):
            continue

        deal_id = deal.get(
            "id"
        )

        project_name = deal.get(
            "project_name"
        ) or "პროექტი —"

        investor_name = deal.get(
            "investor_name"
        ) or "ინვესტორი —"

        stage = deal.get(
            "stage"
        ) or "new"

        amount = deal.get(
            "proposed_amount"
        )

        if amount is not None:

            try:

                amount_text = format_money(
                    amount
                )

            except Exception:

                amount_text = str(
                    amount
                )

        else:

            amount_text = "—"

        lines.append(
            f"#{deal_id} — "
            f"{project_name}"
        )

        lines.append(
            f"👤 {investor_name} | "
            f"📊 {stage}"
        )

        lines.append(
            f"💰 {amount_text}"
        )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ============================================================
# 5.18 — GET PROJECT DEALS
# ============================================================

def get_project_deals(
    chat_id: Any,
    project_id: int,
) -> List[Dict[str, Any]]:

    """
    Return all deals connected to one project.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    normalized_project_id = _crm_record_id(
        project_id
    )

    if not normalized_chat_id:
        return []

    if normalized_project_id is None:
        return []

    try:

        rows = db_execute(
            """
            SELECT
                d.*,
                i.name AS investor_name,
                i.company AS investor_company
            FROM deals d
            LEFT JOIN investors i
                ON i.id = d.investor_id
               AND i.chat_id = d.chat_id
            WHERE d.chat_id = %s
              AND d.project_id = %s
            ORDER BY
                d.updated_at DESC NULLS LAST,
                d.id DESC
            """,
            (
                normalized_chat_id,
                normalized_project_id,
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get project deals: %s",
            exc,
            exc_info=True,
        )

        return []


# ============================================================
# 5.19 — BUILD CRM CONTEXT
# ============================================================

def build_crm_context(
    chat_id: Any
) -> str:

    """
    Build compact investor and deal context for Gemini.
    """

    normalized_chat_id = _crm_chat_id(
        chat_id
    )

    if not normalized_chat_id:

        return (
            "INVESTOR CRM:\nNONE\n\n"
            "DEAL PIPELINE:\nNONE"
        )

    investors = get_investors(
        normalized_chat_id,
        limit=50,
    )

    deals = get_deals(
        normalized_chat_id,
        limit=50,
    )

    lines = [
        "INVESTOR CRM:"
    ]

    if not investors:

        lines.append(
            "NONE"
        )

    else:

        for investor in investors:

            if not isinstance(
                investor,
                dict,
            ):
                continue

            investor_id = investor.get(
                "id"
            )

            name = investor.get(
                "name"
            ) or "Unknown"

            company = investor.get(
                "company"
            ) or "N/A"

            country = investor.get(
                "country"
            ) or "N/A"

            status = investor.get(
                "status"
            ) or "new"

            capacity = investor.get(
                "investment_capacity"
            )

            lines.append(
                f"- ID {investor_id}: "
                f"{name} | "
                f"company={company} | "
                f"country={country} | "
                f"status={status} | "
                f"capacity={capacity}"
            )

    lines.extend([
        "",
        "DEAL PIPELINE:",
    ])

    if not deals:

        lines.append(
            "NONE"
        )

    else:

        for deal in deals:

            if not isinstance(
                deal,
                dict,
            ):
                continue

            deal_id = deal.get(
                "id"
            )

            project_name = deal.get(
                "project_name"
            ) or "N/A"

            investor_name = deal.get(
                "investor_name"
            ) or "N/A"

            stage = deal.get(
                "stage"
            ) or "new"

            amount = deal.get(
                "proposed_amount"
            )

            share = deal.get(
                "proposed_share"
            )

            lines.append(
                f"- ID {deal_id}: "
                f"project={project_name} | "
                f"investor={investor_name} | "
                f"stage={stage} | "
                f"amount={amount} | "
                f"share={share}"
            )

    return "\n".join(
        lines
    )


# ============================================================
# 5.20 — ENHANCED FULL AI CONTEXT
# ============================================================

def build_full_ai_context(
    chat_id: Any
) -> str:

    """
    Combine memory, conversation, projects,
    investors and deals.

    This is the final full-context builder.
    """

    sections = [

        build_memory_context(
            chat_id,
            limit=30,
        ),

        "",

        format_conversation_history(
            chat_id,
            limit=20,
        ),

        "",

        build_projects_context(
            chat_id,
            limit=20,
        ),

        "",

        build_crm_context(
            chat_id
        ),

    ]

    combined_context = "\n".join(
        sections
    ).strip()

    return clean_context_text(
        combined_context,
        max_length=40000,
    )


# ============================================================
# 5.21 — PART 5 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 5/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 6/12
# Document processing, extraction and document CRM
# ============================================================

from pathlib import Path
import csv
import io


# ------------------------------------------------------------
# DOCUMENT LIMITS / SAFETY DEFAULTS
# ------------------------------------------------------------

SUPPORTED_DOCUMENT_TYPES = set(
    globals().get(
        "SUPPORTED_DOCUMENT_TYPES",
        {
            "pdf",
            "docx",
            "xlsx",
            "xlsm",
            "pptx",
            "txt",
            "csv",
        },
    )
)

MAX_EXCEL_ROWS_PER_SHEET = int(
    globals().get(
        "MAX_EXCEL_ROWS_PER_SHEET",
        5000,
    )
)

MAX_DOCUMENT_AI_TEXT = int(
    globals().get(
        "MAX_DOCUMENT_AI_TEXT",
        120000,
    )
)

MAX_DOCUMENT_RECORDS = int(
    globals().get(
        "MAX_DOCUMENT_RECORDS",
        100,
    )
)

MAX_DOCUMENT_FILE_SIZE_BYTES = int(
    globals().get(
        "MAX_DOCUMENT_SIZE_BYTES",
        25 * 1024 * 1024,
    )
)


# ------------------------------------------------------------
# OPTIONAL LIBRARY IMPORTS
# ------------------------------------------------------------

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


try:
    from docx import Document
except Exception:
    Document = None


try:
    from openpyxl import load_workbook
except Exception:
    load_workbook = None


try:
    from pptx import Presentation
except Exception:
    Presentation = None


# ------------------------------------------------------------
# INTERNAL DOCUMENT HELPERS
# ------------------------------------------------------------

def _safe_document_limit(
    value: Any,
    default: int,
    maximum: int,
) -> int:
    """
    Safely normalizes a document-related integer limit.
    """
    try:
        result = int(value)
    except Exception:
        result = default

    return max(
        1,
        min(result, maximum),
    )


def _truncate_document_text(
    value: Any,
    maximum: Optional[int] = None,
) -> str:
    """
    Safely converts and truncates extracted text.
    """
    text_value = str(value or "").strip()

    limit = _safe_document_limit(
        maximum
        if maximum is not None
        else MAX_DOCUMENT_AI_TEXT,
        MAX_DOCUMENT_AI_TEXT,
        max(
            MAX_DOCUMENT_AI_TEXT,
            1,
        ),
    )

    if len(text_value) > limit:
        return text_value[:limit]

    return text_value


def _normalize_document_filename(
    filename: Any,
) -> str:
    """
    Prevents directory traversal through filenames.
    """
    raw_name = str(
        filename or "document"
    ).strip()

    if not raw_name:
        raw_name = "document"

    safe_name = Path(raw_name).name

    if not safe_name:
        safe_name = "document"

    return safe_name[:255]


def _document_dependencies_error(
    dependency_name: str,
) -> RuntimeError:
    """
    Creates a consistent dependency error.
    """
    return RuntimeError(
        f"Required document library is not installed: "
        f"{dependency_name}"
    )


# ------------------------------------------------------------
# FILE TYPE DETECTION
# ------------------------------------------------------------

def detect_file_type(
    filename: str,
) -> str:
    """
    Determines supported document type from file extension.
    """
    name = str(
        filename or ""
    ).strip().lower()

    if "." not in name:
        return "unknown"

    extension = name.rsplit(
        ".",
        1,
    )[-1].strip()

    if extension in SUPPORTED_DOCUMENT_TYPES:
        return extension

    return "unknown"


# ------------------------------------------------------------
# PDF
# ------------------------------------------------------------

def extract_pdf_text(
    filepath: str,
) -> str:
    """
    Extracts text from a PDF file.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"PDF file not found: {filepath}"
        )

    if PdfReader is None:
        raise _document_dependencies_error(
            "pypdf"
        )

    reader = PdfReader(str(path))
    pages = []
    total_length = 0

    for index, page in enumerate(
        reader.pages,
        start=1,
    ):
        try:
            page_text = (
                page.extract_text()
                or ""
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract PDF page %s: %s",
                index,
                exc,
            )
            page_text = ""

        page_text = str(
            page_text
        ).strip()

        if not page_text:
            continue

        remaining = (
            MAX_DOCUMENT_AI_TEXT
            - total_length
        )

        if remaining <= 0:
            pages.append(
                "[DOCUMENT TEXT LIMIT REACHED]"
            )
            break

        page_block = (
            f"--- PAGE {index} ---\n"
            f"{page_text}"
        )

        if len(page_block) > remaining:
            page_block = (
                page_block[:remaining]
                + "\n[TEXT LIMIT REACHED]"
            )

        pages.append(page_block)
        total_length += len(page_block)

        if total_length >= MAX_DOCUMENT_AI_TEXT:
            break

    return "\n\n".join(
        pages
    ).strip()


# ------------------------------------------------------------
# DOCX
# ------------------------------------------------------------

def extract_docx_text(
    filepath: str,
) -> str:
    """
    Extracts paragraphs and tables from DOCX.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"DOCX file not found: {filepath}"
        )

    if Document is None:
        raise _document_dependencies_error(
            "python-docx"
        )

    document = Document(str(path))
    sections = []
    current_length = 0

    for paragraph in document.paragraphs:
        text = str(
            paragraph.text or ""
        ).strip()

        if not text:
            continue

        remaining = (
            MAX_DOCUMENT_AI_TEXT
            - current_length
        )

        if remaining <= 0:
            break

        text = text[:remaining]
        sections.append(text)
        current_length += len(text)

    if current_length < MAX_DOCUMENT_AI_TEXT:
        for table_index, table in enumerate(
            document.tables,
            start=1,
        ):
            rows = []

            for row in table.rows:
                cells = []

                for cell in row.cells:
                    value = str(
                        cell.text or ""
                    ).strip()

                    cells.append(value)

                rows.append(
                    " | ".join(cells)
                )

            if not rows:
                continue

            table_block = (
                f"--- TABLE {table_index} ---\n"
                + "\n".join(rows)
            )

            remaining = (
                MAX_DOCUMENT_AI_TEXT
                - current_length
            )

            if remaining <= 0:
                break

            table_block = table_block[
                :remaining
            ]

            sections.append(
                table_block
            )

            current_length += len(
                table_block
            )

    return "\n\n".join(
        sections
    ).strip()


# ------------------------------------------------------------
# EXCEL
# ------------------------------------------------------------

def extract_excel_text(
    filepath: str,
) -> str:
    """
    Extracts readable worksheet data from XLSX/XLSM.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"Excel file not found: {filepath}"
        )

    if load_workbook is None:
        raise _document_dependencies_error(
            "openpyxl"
        )

    workbook = load_workbook(
        filename=str(path),
        read_only=True,
        data_only=False,
    )

    sections = []
    total_length = 0

    try:
        for worksheet in workbook.worksheets:
            rows = []
            row_count = 0

            for row in worksheet.iter_rows(
                values_only=True,
            ):
                row_count += 1

                if (
                    row_count
                    > MAX_EXCEL_ROWS_PER_SHEET
                ):
                    rows.append(
                        "[ROW LIMIT REACHED]"
                    )
                    break

                values = []

                for value in row:
                    if value is None:
                        values.append("")
                    else:
                        values.append(
                            str(value).strip()
                        )

                if any(values):
                    rows.append(
                        " | ".join(values)
                    )

                current_preview = "\n".join(
                    rows
                )

                if (
                    total_length
                    + len(current_preview)
                    >= MAX_DOCUMENT_AI_TEXT
                ):
                    rows.append(
                        "[DOCUMENT TEXT LIMIT REACHED]"
                    )
                    break

            if not rows:
                continue

            sheet_block = (
                f"--- SHEET: {worksheet.title} ---\n"
                + "\n".join(rows)
            )

            remaining = (
                MAX_DOCUMENT_AI_TEXT
                - total_length
            )

            if remaining <= 0:
                sections.append(
                    "[DOCUMENT TEXT LIMIT REACHED]"
                )
                break

            sheet_block = sheet_block[
                :remaining
            ]

            sections.append(
                sheet_block
            )

            total_length += len(
                sheet_block
            )

            if (
                total_length
                >= MAX_DOCUMENT_AI_TEXT
            ):
                break

    finally:
        workbook.close()

    return "\n\n".join(
        sections
    ).strip()


# ------------------------------------------------------------
# POWERPOINT
# ------------------------------------------------------------

def extract_pptx_text(
    filepath: str,
) -> str:
    """
    Extracts text from PowerPoint slides.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"PPTX file not found: {filepath}"
        )

    if Presentation is None:
        raise _document_dependencies_error(
            "python-pptx"
        )

    presentation = Presentation(
        str(path)
    )

    sections = []
    total_length = 0

    for slide_number, slide in enumerate(
        presentation.slides,
        start=1,
    ):
        texts = []

        for shape in slide.shapes:
            try:
                if not hasattr(
                    shape,
                    "text",
                ):
                    continue

                value = str(
                    shape.text or ""
                ).strip()

                if value:
                    texts.append(value)

            except Exception as exc:
                logger.warning(
                    "Failed to read PPTX shape "
                    "on slide %s: %s",
                    slide_number,
                    exc,
                )

        if not texts:
            continue

        slide_block = (
            f"--- SLIDE {slide_number} ---\n"
            + "\n".join(texts)
        )

        remaining = (
            MAX_DOCUMENT_AI_TEXT
            - total_length
        )

        if remaining <= 0:
            sections.append(
                "[DOCUMENT TEXT LIMIT REACHED]"
            )
            break

        slide_block = slide_block[
            :remaining
        ]

        sections.append(
            slide_block
        )

        total_length += len(
            slide_block
        )

        if (
            total_length
            >= MAX_DOCUMENT_AI_TEXT
        ):
            break

    return "\n\n".join(
        sections
    ).strip()


# ------------------------------------------------------------
# TXT
# ------------------------------------------------------------

def extract_txt_text(
    filepath: str,
) -> str:
    """
    Reads common text encodings.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"Text file not found: {filepath}"
        )

    encodings = [
        "utf-8",
        "utf-8-sig",
        "cp1251",
        "latin-1",
    ]

    last_error = None

    for encoding in encodings:
        try:
            text_value = path.read_text(
                encoding=encoding,
                errors="strict",
            )

            return _truncate_document_text(
                text_value
            )

        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error

    return ""


# ------------------------------------------------------------
# CSV
# ------------------------------------------------------------

def extract_csv_text(
    filepath: str,
) -> str:
    """
    Reads CSV content with common encodings.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"CSV file not found: {filepath}"
        )

    encodings = [
        "utf-8",
        "utf-8-sig",
        "cp1251",
        "latin-1",
    ]

    last_error = None

    for encoding in encodings:
        try:
            raw_text = path.read_text(
                encoding=encoding,
                errors="strict",
            )

            output = io.StringIO()
            writer = csv.writer(
                output,
                lineterminator="\n",
            )

            reader = csv.reader(
                io.StringIO(raw_text)
            )

            row_count = 0

            for row in reader:
                row_count += 1

                if (
                    row_count
                    > MAX_EXCEL_ROWS_PER_SHEET
                ):
                    writer.writerow(
                        ["[ROW LIMIT REACHED]"]
                    )
                    break

                writer.writerow(
                    [
                        str(value).strip()
                        for value in row
                    ]
                )

                if (
                    output.tell()
                    >= MAX_DOCUMENT_AI_TEXT
                ):
                    writer.writerow(
                        [
                            "[DOCUMENT TEXT LIMIT REACHED]"
                        ]
                    )
                    break

            return _truncate_document_text(
                output.getvalue()
            )

        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error

    return ""


# ------------------------------------------------------------
# UNIFIED EXTRACTION
# ------------------------------------------------------------

def extract_file_text(
    filepath: str,
    file_type: Optional[str] = None,
) -> str:
    """
    Unified document text extraction.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {filepath}"
        )

    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = 0

    if (
        file_size
        > MAX_DOCUMENT_FILE_SIZE_BYTES
    ):
        raise ValueError(
            "Document exceeds the maximum "
            "allowed file size."
        )

    detected_type = (
        file_type
        or detect_file_type(path.name)
    )

    detected_type = str(
        detected_type or ""
    ).lower().strip()

    if (
        detected_type
        not in SUPPORTED_DOCUMENT_TYPES
    ):
        raise ValueError(
            f"Unsupported document type: "
            f"{detected_type}"
        )

    if detected_type == "pdf":
        text_value = extract_pdf_text(
            str(path)
        )

    elif detected_type == "docx":
        text_value = extract_docx_text(
            str(path)
        )

    elif detected_type in {
        "xlsx",
        "xlsm",
    }:
        text_value = extract_excel_text(
            str(path)
        )

    elif detected_type == "pptx":
        text_value = extract_pptx_text(
            str(path)
        )

    elif detected_type == "txt":
        text_value = extract_txt_text(
            str(path)
        )

    elif detected_type == "csv":
        text_value = extract_csv_text(
            str(path)
        )

    else:
        raise ValueError(
            f"Unsupported document type: "
            f"{detected_type}"
        )

    return _truncate_document_text(
        text_value
    )


# ------------------------------------------------------------
# DOCUMENT CRM — CREATE
# ------------------------------------------------------------

def save_document_record(
    chat_id: Any,
    filename: str,
    file_type: Optional[str] = None,
    extracted_text: Optional[str] = None,
    project_id: Optional[int] = None,
) -> Optional[int]:
    """
    Saves uploaded document metadata and extracted text.
    """
    safe_filename = _normalize_document_filename(
        filename
    )

    detected_type = (
        str(
            file_type
            or detect_file_type(
                safe_filename
            )
        )
        .lower()
        .strip()
    )

    if (
        detected_type
        not in SUPPORTED_DOCUMENT_TYPES
    ):
        detected_type = detect_file_type(
            safe_filename
        )

    text_value = _truncate_document_text(
        extracted_text or ""
    )

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )

            if project_id_value <= 0:
                return None

        except Exception:
            return None

        if not get_project(
            chat_id,
            project_id_value,
        ):
            return None

    try:
        result = db_execute(
            """
            INSERT INTO documents (
                chat_id,
                project_id,
                filename,
                file_type,
                extracted_text
            )
            VALUES (
                %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                str(chat_id),
                project_id_value,
                safe_filename,
                detected_type,
                text_value,
            ),
            fetchone=True,
            commit=True,
        )

        if result:
            return int(
                result["id"]
            )

    except Exception as exc:
        logger.error(
            "Failed to save document record: %s",
            exc,
        )

    return None


# ------------------------------------------------------------
# DOCUMENT CRM — UPDATE ANALYSIS
# ------------------------------------------------------------

def update_document_analysis(
    chat_id: Any,
    document_id: int,
    analysis: str,
) -> bool:
    """
    Saves AI analysis for a document.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return False

        analysis_value = str(
            analysis or ""
        ).strip()

        if len(analysis_value) > MAX_DOCUMENT_AI_TEXT:
            analysis_value = analysis_value[
                :MAX_DOCUMENT_AI_TEXT
            ]

        result = db_execute(
            """
            UPDATE documents
            SET
                analysis = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                analysis_value,
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:
        logger.error(
            "Failed to update document analysis %s: %s",
            document_id,
            exc,
        )
        return False


# ------------------------------------------------------------
# DOCUMENT CRM — PROJECT LINK
# ------------------------------------------------------------

def update_document_project(
    chat_id: Any,
    document_id: int,
    project_id: Optional[int],
) -> bool:
    """
    Links or unlinks a document to a project.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return False

    except Exception:
        return False

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )

            if project_id_value <= 0:
                return False

        except Exception:
            return False

        if not get_project(
            chat_id,
            project_id_value,
        ):
            return False

    try:
        result = db_execute(
            """
            UPDATE documents
            SET
                project_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                project_id_value,
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:
        logger.error(
            "Failed to update document project %s: %s",
            document_id,
            exc,
        )
        return False


# ------------------------------------------------------------
# DOCUMENT CRM — LIST
# ------------------------------------------------------------

def get_documents(
    chat_id: Any,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Returns documents belonging to a chat.
    """
    try:
        safe_limit = _safe_document_limit(
            limit,
            50,
            MAX_DOCUMENT_RECORDS,
        )

        rows = db_execute(
            f"""
            SELECT
                d.*,
                p.name AS project_name
            FROM documents d
            LEFT JOIN projects p
                ON p.id = d.project_id
               AND p.chat_id = d.chat_id
            WHERE d.chat_id = %s
            ORDER BY
                d.updated_at DESC,
                d.id DESC
            LIMIT {safe_limit}
            """,
            (
                str(chat_id),
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:
        logger.error(
            "Failed to get documents: %s",
            exc,
        )
        return []


# ------------------------------------------------------------
# DOCUMENT CRM — GET ONE
# ------------------------------------------------------------

def get_document(
    chat_id: Any,
    document_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Returns one document owned by the chat.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return None

        return db_execute(
            """
            SELECT
                d.*,
                p.name AS project_name
            FROM documents d
            LEFT JOIN projects p
                ON p.id = d.project_id
               AND p.chat_id = d.chat_id
            WHERE d.id = %s
              AND d.chat_id = %s
            LIMIT 1
            """,
            (
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
        )

    except Exception as exc:
        logger.error(
            "Failed to get document %s: %s",
            document_id,
            exc,
        )
        return None


# ------------------------------------------------------------
# DOCUMENT CRM — DELETE
# ------------------------------------------------------------

def delete_document(
    chat_id: Any,
    document_id: int,
) -> bool:
    """
    Deletes one document record.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return False

        result = db_execute(
            """
            DELETE FROM documents
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:
        logger.error(
            "Failed to delete document %s: %s",
            document_id,
            exc,
        )
        return False


# ------------------------------------------------------------
# DOCUMENT CRM — SUMMARY
# ------------------------------------------------------------

def documents_summary(
    chat_id: Any,
) -> str:
    """
    Human-readable document list.
    """
    documents = get_documents(
        chat_id,
        limit=MAX_DOCUMENT_RECORDS,
    )

    if not documents:
        return (
            "📄 GENIOSA — დოკუმენტები\n\n"
            "დოკუმენტები ჯერ არ არის ატვირთული."
        )

    lines = [
        "📄 GENIOSA — დოკუმენტები",
        "",
    ]

    for document in documents:
        document_id = document.get(
            "id"
        )

        filename = (
            document.get("filename")
            or "უცნობი ფაილი"
        )

        file_type = (
            document.get("file_type")
            or "unknown"
        )

        project_name = (
            document.get("project_name")
            or "პროექტთან მიბმული არაა"
        )

        extracted_text = (
            document.get("extracted_text")
            or ""
        )

        analysis = (
            document.get("analysis")
            or ""
        )

        text_status = (
            "ამოღებულია"
            if str(
                extracted_text
            ).strip()
            else "ტექსტი არ არის"
        )

        analysis_status = (
            "გაანალიზებულია"
            if str(
                analysis
            ).strip()
            else "არ არის გაანალიზებული"
        )

        lines.append(
            f"#{document_id} — {filename}"
        )

        lines.append(
            f"📁 ტიპი: {file_type}"
        )

        lines.append(
            f"🏗️ პროექტი: {project_name}"
        )

        lines.append(
            f"📝 ტექსტი: {text_status}"
        )

        lines.append(
            f"🤖 AI: {analysis_status}"
        )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ------------------------------------------------------------
# DOCUMENT → AI CONTEXT
# ------------------------------------------------------------

def prepare_document_for_ai(
    document: Dict[str, Any],
) -> str:
    """
    Prepares a document's extracted content
    for Gemini analysis.
    """
    if not document:
        return ""

    filename = (
        document.get("filename")
        or "document"
    )

    file_type = (
        document.get("file_type")
        or "unknown"
    )

    project_name = (
        document.get("project_name")
        or "not linked"
    )

    extracted_text = _truncate_document_text(
        document.get("extracted_text")
        or ""
    )

    if not extracted_text:
        extracted_text = (
            "[No readable text was extracted "
            "from this document.]"
        )

    return (
        "DOCUMENT INFORMATION\n"
        f"Filename: {filename}\n"
        f"Type: {file_type}\n"
        f"Project: {project_name}\n\n"
        "DOCUMENT CONTENT\n"
        f"{extracted_text}"
    )


# ------------------------------------------------------------
# DOCUMENT AI PROMPT
# ------------------------------------------------------------

def build_document_analysis_prompt(
    document: Dict[str, Any],
    chat_id: Any,
) -> str:
    """
    Builds a structured AI prompt for document analysis.
    """
    document_context = (
        prepare_document_for_ai(
            document
        )
    )

    business_context = (
        build_full_ai_context(
            chat_id
        )
    )

    business_context = _truncate_document_text(
        business_context,
        maximum=40000,
    )

    return f"""
You are Geniosa 4.0, a professional business,
investment, real-estate and financial intelligence
assistant.

Analyze the uploaded document carefully.

Your analysis must be practical, structured and
decision-oriented.

Identify when applicable:

1. Executive summary
2. Main facts and figures
3. Project or company information
4. Financial data
5. Revenue and cost assumptions
6. Investment requirements
7. Profitability
8. Risks
9. Opportunities
10. Missing or suspicious information
11. Important contradictions
12. Recommended next steps

If the document contains financial information,
calculate or verify the important figures when
possible.

Do not invent facts that are not present.

Clearly distinguish:
- information directly stated in the document
- reasonable calculations
- assumptions
- recommendations

Answer in the same language as the user's request.
If the user writes in Georgian, answer in Georgian.
If the user writes in Russian, answer in Russian.
If the user writes in English, answer in English.

{document_context}

EXISTING GENIOSA BUSINESS CONTEXT
{business_context}
""".strip()


# ------------------------------------------------------------
# DOCUMENT TEXT PREVIEW
# ------------------------------------------------------------

def document_text_preview(
    document: Optional[Dict[str, Any]],
    max_length: int = 1500,
) -> str:
    """
    Creates a short preview of extracted document text.
    """
    if not document:
        return (
            "❌ დოკუმენტი ვერ მოიძებნა."
        )

    text_value = str(
        document.get(
            "extracted_text"
        )
        or ""
    ).strip()

    if not text_value:
        return (
            "📄 დოკუმენტიდან ტექსტის ამოღება "
            "ვერ მოხერხდა."
        )

    clean_value = re.sub(
        r"\s+",
        " ",
        text_value,
    ).strip()

    try:
        safe_length = int(
            max_length
        )
    except Exception:
        safe_length = 1500

    safe_length = max(
        100,
        min(
            safe_length,
            10000,
        ),
    )

    if len(clean_value) > safe_length:
        return (
            clean_value[
                :safe_length
            ].rstrip()
            + "..."
        )

    return clean_value


# ------------------------------------------------------------
# DOCUMENT ANALYSIS SUMMARY
# ------------------------------------------------------------

def document_analysis_summary(
    document: Optional[Dict[str, Any]],
) -> str:
    """
    Returns a concise document analysis result.
    """
    if not document:
        return (
            "❌ დოკუმენტი ვერ მოიძებნა."
        )

    document_id = document.get(
        "id"
    )

    filename = (
        document.get("filename")
        or "უცნობი ფაილი"
    )

    analysis = str(
        document.get("analysis")
        or ""
    ).strip()

    if not analysis:
        return (
            f"📄 დოკუმენტი #{document_id}\n"
            f"📎 {filename}\n\n"
            "🤖 AI ანალიზი ჯერ არ არის შესრულებული."
        )

    return (
        f"📄 დოკუმენტი #{document_id}\n"
        f"📎 {filename}\n\n"
        f"{analysis}"
    )


print(
    "GENIOSA 4.0 — PART 6/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 6/12
# Document processing, extraction and document CRM
# ============================================================

from pathlib import Path
import csv
import io


# ------------------------------------------------------------
# DOCUMENT LIMITS / SAFETY DEFAULTS
# ------------------------------------------------------------

SUPPORTED_DOCUMENT_TYPES = set(
    globals().get(
        "SUPPORTED_DOCUMENT_TYPES",
        {
            "pdf",
            "docx",
            "xlsx",
            "xlsm",
            "pptx",
            "txt",
            "csv",
        },
    )
)

MAX_EXCEL_ROWS_PER_SHEET = int(
    globals().get(
        "MAX_EXCEL_ROWS_PER_SHEET",
        5000,
    )
)

MAX_DOCUMENT_AI_TEXT = int(
    globals().get(
        "MAX_DOCUMENT_AI_TEXT",
        120000,
    )
)

MAX_DOCUMENT_RECORDS = int(
    globals().get(
        "MAX_DOCUMENT_RECORDS",
        100,
    )
)

MAX_DOCUMENT_FILE_SIZE_BYTES = int(
    globals().get(
        "MAX_DOCUMENT_SIZE_BYTES",
        25 * 1024 * 1024,
    )
)


# ------------------------------------------------------------
# OPTIONAL LIBRARY IMPORTS
# ------------------------------------------------------------

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


try:
    from docx import Document
except Exception:
    Document = None


try:
    from openpyxl import load_workbook
except Exception:
    load_workbook = None


try:
    from pptx import Presentation
except Exception:
    Presentation = None


# ------------------------------------------------------------
# INTERNAL DOCUMENT HELPERS
# ------------------------------------------------------------

def _safe_document_limit(
    value: Any,
    default: int,
    maximum: int,
) -> int:
    """
    Safely normalizes a document-related integer limit.
    """
    try:
        result = int(value)
    except Exception:
        result = default

    return max(
        1,
        min(result, maximum),
    )


def _truncate_document_text(
    value: Any,
    maximum: Optional[int] = None,
) -> str:
    """
    Safely converts and truncates extracted text.
    """
    text_value = str(value or "").strip()

    limit = _safe_document_limit(
        maximum
        if maximum is not None
        else MAX_DOCUMENT_AI_TEXT,
        MAX_DOCUMENT_AI_TEXT,
        max(
            MAX_DOCUMENT_AI_TEXT,
            1,
        ),
    )

    if len(text_value) > limit:
        return text_value[:limit]

    return text_value


def _normalize_document_filename(
    filename: Any,
) -> str:
    """
    Prevents directory traversal through filenames.
    """
    raw_name = str(
        filename or "document"
    ).strip()

    if not raw_name:
        raw_name = "document"

    safe_name = Path(raw_name).name

    if not safe_name:
        safe_name = "document"

    return safe_name[:255]


def _document_dependencies_error(
    dependency_name: str,
) -> RuntimeError:
    """
    Creates a consistent dependency error.
    """
    return RuntimeError(
        f"Required document library is not installed: "
        f"{dependency_name}"
    )


# ------------------------------------------------------------
# FILE TYPE DETECTION
# ------------------------------------------------------------

def detect_file_type(
    filename: str,
) -> str:
    """
    Determines supported document type from file extension.
    """
    name = str(
        filename or ""
    ).strip().lower()

    if "." not in name:
        return "unknown"

    extension = name.rsplit(
        ".",
        1,
    )[-1].strip()

    if extension in SUPPORTED_DOCUMENT_TYPES:
        return extension

    return "unknown"


# ------------------------------------------------------------
# PDF
# ------------------------------------------------------------

def extract_pdf_text(
    filepath: str,
) -> str:
    """
    Extracts text from a PDF file.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"PDF file not found: {filepath}"
        )

    if PdfReader is None:
        raise _document_dependencies_error(
            "pypdf"
        )

    reader = PdfReader(str(path))
    pages = []
    total_length = 0

    for index, page in enumerate(
        reader.pages,
        start=1,
    ):
        try:
            page_text = (
                page.extract_text()
                or ""
            )
        except Exception as exc:
            logger.warning(
                "Failed to extract PDF page %s: %s",
                index,
                exc,
            )
            page_text = ""

        page_text = str(
            page_text
        ).strip()

        if not page_text:
            continue

        remaining = (
            MAX_DOCUMENT_AI_TEXT
            - total_length
        )

        if remaining <= 0:
            pages.append(
                "[DOCUMENT TEXT LIMIT REACHED]"
            )
            break

        page_block = (
            f"--- PAGE {index} ---\n"
            f"{page_text}"
        )

        if len(page_block) > remaining:
            page_block = (
                page_block[:remaining]
                + "\n[TEXT LIMIT REACHED]"
            )

        pages.append(page_block)
        total_length += len(page_block)

        if total_length >= MAX_DOCUMENT_AI_TEXT:
            break

    return "\n\n".join(
        pages
    ).strip()


# ------------------------------------------------------------
# DOCX
# ------------------------------------------------------------

def extract_docx_text(
    filepath: str,
) -> str:
    """
    Extracts paragraphs and tables from DOCX.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"DOCX file not found: {filepath}"
        )

    if Document is None:
        raise _document_dependencies_error(
            "python-docx"
        )

    document = Document(str(path))
    sections = []
    current_length = 0

    for paragraph in document.paragraphs:
        text = str(
            paragraph.text or ""
        ).strip()

        if not text:
            continue

        remaining = (
            MAX_DOCUMENT_AI_TEXT
            - current_length
        )

        if remaining <= 0:
            break

        text = text[:remaining]
        sections.append(text)
        current_length += len(text)

    if current_length < MAX_DOCUMENT_AI_TEXT:
        for table_index, table in enumerate(
            document.tables,
            start=1,
        ):
            rows = []

            for row in table.rows:
                cells = []

                for cell in row.cells:
                    value = str(
                        cell.text or ""
                    ).strip()

                    cells.append(value)

                rows.append(
                    " | ".join(cells)
                )

            if not rows:
                continue

            table_block = (
                f"--- TABLE {table_index} ---\n"
                + "\n".join(rows)
            )

            remaining = (
                MAX_DOCUMENT_AI_TEXT
                - current_length
            )

            if remaining <= 0:
                break

            table_block = table_block[
                :remaining
            ]

            sections.append(
                table_block
            )

            current_length += len(
                table_block
            )

    return "\n\n".join(
        sections
    ).strip()


# ------------------------------------------------------------
# EXCEL
# ------------------------------------------------------------

def extract_excel_text(
    filepath: str,
) -> str:
    """
    Extracts readable worksheet data from XLSX/XLSM.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"Excel file not found: {filepath}"
        )

    if load_workbook is None:
        raise _document_dependencies_error(
            "openpyxl"
        )

    workbook = load_workbook(
        filename=str(path),
        read_only=True,
        data_only=False,
    )

    sections = []
    total_length = 0

    try:
        for worksheet in workbook.worksheets:
            rows = []
            row_count = 0

            for row in worksheet.iter_rows(
                values_only=True,
            ):
                row_count += 1

                if (
                    row_count
                    > MAX_EXCEL_ROWS_PER_SHEET
                ):
                    rows.append(
                        "[ROW LIMIT REACHED]"
                    )
                    break

                values = []

                for value in row:
                    if value is None:
                        values.append("")
                    else:
                        values.append(
                            str(value).strip()
                        )

                if any(values):
                    rows.append(
                        " | ".join(values)
                    )

                current_preview = "\n".join(
                    rows
                )

                if (
                    total_length
                    + len(current_preview)
                    >= MAX_DOCUMENT_AI_TEXT
                ):
                    rows.append(
                        "[DOCUMENT TEXT LIMIT REACHED]"
                    )
                    break

            if not rows:
                continue

            sheet_block = (
                f"--- SHEET: {worksheet.title} ---\n"
                + "\n".join(rows)
            )

            remaining = (
                MAX_DOCUMENT_AI_TEXT
                - total_length
            )

            if remaining <= 0:
                sections.append(
                    "[DOCUMENT TEXT LIMIT REACHED]"
                )
                break

            sheet_block = sheet_block[
                :remaining
            ]

            sections.append(
                sheet_block
            )

            total_length += len(
                sheet_block
            )

            if (
                total_length
                >= MAX_DOCUMENT_AI_TEXT
            ):
                break

    finally:
        workbook.close()

    return "\n\n".join(
        sections
    ).strip()


# ------------------------------------------------------------
# POWERPOINT
# ------------------------------------------------------------

def extract_pptx_text(
    filepath: str,
) -> str:
    """
    Extracts text from PowerPoint slides.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"PPTX file not found: {filepath}"
        )

    if Presentation is None:
        raise _document_dependencies_error(
            "python-pptx"
        )

    presentation = Presentation(
        str(path)
    )

    sections = []
    total_length = 0

    for slide_number, slide in enumerate(
        presentation.slides,
        start=1,
    ):
        texts = []

        for shape in slide.shapes:
            try:
                if not hasattr(
                    shape,
                    "text",
                ):
                    continue

                value = str(
                    shape.text or ""
                ).strip()

                if value:
                    texts.append(value)

            except Exception as exc:
                logger.warning(
                    "Failed to read PPTX shape "
                    "on slide %s: %s",
                    slide_number,
                    exc,
                )

        if not texts:
            continue

        slide_block = (
            f"--- SLIDE {slide_number} ---\n"
            + "\n".join(texts)
        )

        remaining = (
            MAX_DOCUMENT_AI_TEXT
            - total_length
        )

        if remaining <= 0:
            sections.append(
                "[DOCUMENT TEXT LIMIT REACHED]"
            )
            break

        slide_block = slide_block[
            :remaining
        ]

        sections.append(
            slide_block
        )

        total_length += len(
            slide_block
        )

        if (
            total_length
            >= MAX_DOCUMENT_AI_TEXT
        ):
            break

    return "\n\n".join(
        sections
    ).strip()


# ------------------------------------------------------------
# TXT
# ------------------------------------------------------------

def extract_txt_text(
    filepath: str,
) -> str:
    """
    Reads common text encodings.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"Text file not found: {filepath}"
        )

    encodings = [
        "utf-8",
        "utf-8-sig",
        "cp1251",
        "latin-1",
    ]

    last_error = None

    for encoding in encodings:
        try:
            text_value = path.read_text(
                encoding=encoding,
                errors="strict",
            )

            return _truncate_document_text(
                text_value
            )

        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error

    return ""


# ------------------------------------------------------------
# CSV
# ------------------------------------------------------------

def extract_csv_text(
    filepath: str,
) -> str:
    """
    Reads CSV content with common encodings.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"CSV file not found: {filepath}"
        )

    encodings = [
        "utf-8",
        "utf-8-sig",
        "cp1251",
        "latin-1",
    ]

    last_error = None

    for encoding in encodings:
        try:
            raw_text = path.read_text(
                encoding=encoding,
                errors="strict",
            )

            output = io.StringIO()
            writer = csv.writer(
                output,
                lineterminator="\n",
            )

            reader = csv.reader(
                io.StringIO(raw_text)
            )

            row_count = 0

            for row in reader:
                row_count += 1

                if (
                    row_count
                    > MAX_EXCEL_ROWS_PER_SHEET
                ):
                    writer.writerow(
                        ["[ROW LIMIT REACHED]"]
                    )
                    break

                writer.writerow(
                    [
                        str(value).strip()
                        for value in row
                    ]
                )

                if (
                    output.tell()
                    >= MAX_DOCUMENT_AI_TEXT
                ):
                    writer.writerow(
                        [
                            "[DOCUMENT TEXT LIMIT REACHED]"
                        ]
                    )
                    break

            return _truncate_document_text(
                output.getvalue()
            )

        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error

    return ""


# ------------------------------------------------------------
# UNIFIED EXTRACTION
# ------------------------------------------------------------

def extract_file_text(
    filepath: str,
    file_type: Optional[str] = None,
) -> str:
    """
    Unified document text extraction.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {filepath}"
        )

    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = 0

    if (
        file_size
        > MAX_DOCUMENT_FILE_SIZE_BYTES
    ):
        raise ValueError(
            "Document exceeds the maximum "
            "allowed file size."
        )

    detected_type = (
        file_type
        or detect_file_type(path.name)
    )

    detected_type = str(
        detected_type or ""
    ).lower().strip()

    if (
        detected_type
        not in SUPPORTED_DOCUMENT_TYPES
    ):
        raise ValueError(
            f"Unsupported document type: "
            f"{detected_type}"
        )

    if detected_type == "pdf":
        text_value = extract_pdf_text(
            str(path)
        )

    elif detected_type == "docx":
        text_value = extract_docx_text(
            str(path)
        )

    elif detected_type in {
        "xlsx",
        "xlsm",
    }:
        text_value = extract_excel_text(
            str(path)
        )

    elif detected_type == "pptx":
        text_value = extract_pptx_text(
            str(path)
        )

    elif detected_type == "txt":
        text_value = extract_txt_text(
            str(path)
        )

    elif detected_type == "csv":
        text_value = extract_csv_text(
            str(path)
        )

    else:
        raise ValueError(
            f"Unsupported document type: "
            f"{detected_type}"
        )

    return _truncate_document_text(
        text_value
    )


# ------------------------------------------------------------
# DOCUMENT CRM — CREATE
# ------------------------------------------------------------

def save_document_record(
    chat_id: Any,
    filename: str,
    file_type: Optional[str] = None,
    extracted_text: Optional[str] = None,
    project_id: Optional[int] = None,
) -> Optional[int]:
    """
    Saves uploaded document metadata and extracted text.
    """
    safe_filename = _normalize_document_filename(
        filename
    )

    detected_type = (
        str(
            file_type
            or detect_file_type(
                safe_filename
            )
        )
        .lower()
        .strip()
    )

    if (
        detected_type
        not in SUPPORTED_DOCUMENT_TYPES
    ):
        detected_type = detect_file_type(
            safe_filename
        )

    text_value = _truncate_document_text(
        extracted_text or ""
    )

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )

            if project_id_value <= 0:
                return None

        except Exception:
            return None

        if not get_project(
            chat_id,
            project_id_value,
        ):
            return None

    try:
        result = db_execute(
            """
            INSERT INTO documents (
                chat_id,
                project_id,
                filename,
                file_type,
                extracted_text
            )
            VALUES (
                %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                str(chat_id),
                project_id_value,
                safe_filename,
                detected_type,
                text_value,
            ),
            fetchone=True,
            commit=True,
        )

        if result:
            return int(
                result["id"]
            )

    except Exception as exc:
        logger.error(
            "Failed to save document record: %s",
            exc,
        )

    return None


# ------------------------------------------------------------
# DOCUMENT CRM — UPDATE ANALYSIS
# ------------------------------------------------------------

def update_document_analysis(
    chat_id: Any,
    document_id: int,
    analysis: str,
) -> bool:
    """
    Saves AI analysis for a document.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return False

        analysis_value = str(
            analysis or ""
        ).strip()

        if len(analysis_value) > MAX_DOCUMENT_AI_TEXT:
            analysis_value = analysis_value[
                :MAX_DOCUMENT_AI_TEXT
            ]

        result = db_execute(
            """
            UPDATE documents
            SET
                analysis = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                analysis_value,
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:
        logger.error(
            "Failed to update document analysis %s: %s",
            document_id,
            exc,
        )
        return False


# ------------------------------------------------------------
# DOCUMENT CRM — PROJECT LINK
# ------------------------------------------------------------

def update_document_project(
    chat_id: Any,
    document_id: int,
    project_id: Optional[int],
) -> bool:
    """
    Links or unlinks a document to a project.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return False

    except Exception:
        return False

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )

            if project_id_value <= 0:
                return False

        except Exception:
            return False

        if not get_project(
            chat_id,
            project_id_value,
        ):
            return False

    try:
        result = db_execute(
            """
            UPDATE documents
            SET
                project_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                project_id_value,
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:
        logger.error(
            "Failed to update document project %s: %s",
            document_id,
            exc,
        )
        return False


# ------------------------------------------------------------
# DOCUMENT CRM — LIST
# ------------------------------------------------------------

def get_documents(
    chat_id: Any,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Returns documents belonging to a chat.
    """
    try:
        safe_limit = _safe_document_limit(
            limit,
            50,
            MAX_DOCUMENT_RECORDS,
        )

        rows = db_execute(
            f"""
            SELECT
                d.*,
                p.name AS project_name
            FROM documents d
            LEFT JOIN projects p
                ON p.id = d.project_id
               AND p.chat_id = d.chat_id
            WHERE d.chat_id = %s
            ORDER BY
                d.updated_at DESC,
                d.id DESC
            LIMIT {safe_limit}
            """,
            (
                str(chat_id),
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:
        logger.error(
            "Failed to get documents: %s",
            exc,
        )
        return []


# ------------------------------------------------------------
# DOCUMENT CRM — GET ONE
# ------------------------------------------------------------

def get_document(
    chat_id: Any,
    document_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Returns one document owned by the chat.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return None

        return db_execute(
            """
            SELECT
                d.*,
                p.name AS project_name
            FROM documents d
            LEFT JOIN projects p
                ON p.id = d.project_id
               AND p.chat_id = d.chat_id
            WHERE d.id = %s
              AND d.chat_id = %s
            LIMIT 1
            """,
            (
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
        )

    except Exception as exc:
        logger.error(
            "Failed to get document %s: %s",
            document_id,
            exc,
        )
        return None


# ------------------------------------------------------------
# DOCUMENT CRM — DELETE
# ------------------------------------------------------------

def delete_document(
    chat_id: Any,
    document_id: int,
) -> bool:
    """
    Deletes one document record.
    """
    try:
        safe_document_id = int(
            document_id
        )

        if safe_document_id <= 0:
            return False

        result = db_execute(
            """
            DELETE FROM documents
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                safe_document_id,
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:
        logger.error(
            "Failed to delete document %s: %s",
            document_id,
            exc,
        )
        return False


# ------------------------------------------------------------
# DOCUMENT CRM — SUMMARY
# ------------------------------------------------------------

def documents_summary(
    chat_id: Any,
) -> str:
    """
    Human-readable document list.
    """
    documents = get_documents(
        chat_id,
        limit=MAX_DOCUMENT_RECORDS,
    )

    if not documents:
        return (
            "📄 GENIOSA — დოკუმენტები\n\n"
            "დოკუმენტები ჯერ არ არის ატვირთული."
        )

    lines = [
        "📄 GENIOSA — დოკუმენტები",
        "",
    ]

    for document in documents:
        document_id = document.get(
            "id"
        )

        filename = (
            document.get("filename")
            or "უცნობი ფაილი"
        )

        file_type = (
            document.get("file_type")
            or "unknown"
        )

        project_name = (
            document.get("project_name")
            or "პროექტთან მიბმული არაა"
        )

        extracted_text = (
            document.get("extracted_text")
            or ""
        )

        analysis = (
            document.get("analysis")
            or ""
        )

        text_status = (
            "ამოღებულია"
            if str(
                extracted_text
            ).strip()
            else "ტექსტი არ არის"
        )

        analysis_status = (
            "გაანალიზებულია"
            if str(
                analysis
            ).strip()
            else "არ არის გაანალიზებული"
        )

        lines.append(
            f"#{document_id} — {filename}"
        )

        lines.append(
            f"📁 ტიპი: {file_type}"
        )

        lines.append(
            f"🏗️ პროექტი: {project_name}"
        )

        lines.append(
            f"📝 ტექსტი: {text_status}"
        )

        lines.append(
            f"🤖 AI: {analysis_status}"
        )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ------------------------------------------------------------
# DOCUMENT → AI CONTEXT
# ------------------------------------------------------------

def prepare_document_for_ai(
    document: Dict[str, Any],
) -> str:
    """
    Prepares a document's extracted content
    for Gemini analysis.
    """
    if not document:
        return ""

    filename = (
        document.get("filename")
        or "document"
    )

    file_type = (
        document.get("file_type")
        or "unknown"
    )

    project_name = (
        document.get("project_name")
        or "not linked"
    )

    extracted_text = _truncate_document_text(
        document.get("extracted_text")
        or ""
    )

    if not extracted_text:
        extracted_text = (
            "[No readable text was extracted "
            "from this document.]"
        )

    return (
        "DOCUMENT INFORMATION\n"
        f"Filename: {filename}\n"
        f"Type: {file_type}\n"
        f"Project: {project_name}\n\n"
        "DOCUMENT CONTENT\n"
        f"{extracted_text}"
    )


# ------------------------------------------------------------
# DOCUMENT AI PROMPT
# ------------------------------------------------------------

def build_document_analysis_prompt(
    document: Dict[str, Any],
    chat_id: Any,
) -> str:
    """
    Builds a structured AI prompt for document analysis.
    """
    document_context = (
        prepare_document_for_ai(
            document
        )
    )

    business_context = (
        build_full_ai_context(
            chat_id
        )
    )

    business_context = _truncate_document_text(
        business_context,
        maximum=40000,
    )

    return f"""
You are Geniosa 4.0, a professional business,
investment, real-estate and financial intelligence
assistant.

Analyze the uploaded document carefully.

Your analysis must be practical, structured and
decision-oriented.

Identify when applicable:

1. Executive summary
2. Main facts and figures
3. Project or company information
4. Financial data
5. Revenue and cost assumptions
6. Investment requirements
7. Profitability
8. Risks
9. Opportunities
10. Missing or suspicious information
11. Important contradictions
12. Recommended next steps

If the document contains financial information,
calculate or verify the important figures when
possible.

Do not invent facts that are not present.

Clearly distinguish:
- information directly stated in the document
- reasonable calculations
- assumptions
- recommendations

Answer in the same language as the user's request.
If the user writes in Georgian, answer in Georgian.
If the user writes in Russian, answer in Russian.
If the user writes in English, answer in English.

{document_context}

EXISTING GENIOSA BUSINESS CONTEXT
{business_context}
""".strip()


# ------------------------------------------------------------
# DOCUMENT TEXT PREVIEW
# ------------------------------------------------------------

def document_text_preview(
    document: Optional[Dict[str, Any]],
    max_length: int = 1500,
) -> str:
    """
    Creates a short preview of extracted document text.
    """
    if not document:
        return (
            "❌ დოკუმენტი ვერ მოიძებნა."
        )

    text_value = str(
        document.get(
            "extracted_text"
        )
        or ""
    ).strip()

    if not text_value:
        return (
            "📄 დოკუმენტიდან ტექსტის ამოღება "
            "ვერ მოხერხდა."
        )

    clean_value = re.sub(
        r"\s+",
        " ",
        text_value,
    ).strip()

    try:
        safe_length = int(
            max_length
        )
    except Exception:
        safe_length = 1500

    safe_length = max(
        100,
        min(
            safe_length,
            10000,
        ),
    )

    if len(clean_value) > safe_length:
        return (
            clean_value[
                :safe_length
            ].rstrip()
            + "..."
        )

    return clean_value


# ------------------------------------------------------------
# DOCUMENT ANALYSIS SUMMARY
# ------------------------------------------------------------

def document_analysis_summary(
    document: Optional[Dict[str, Any]],
) -> str:
    """
    Returns a concise document analysis result.
    """
    if not document:
        return (
            "❌ დოკუმენტი ვერ მოიძებნა."
        )

    document_id = document.get(
        "id"
    )

    filename = (
        document.get("filename")
        or "უცნობი ფაილი"
    )

    analysis = str(
        document.get("analysis")
        or ""
    ).strip()

    if not analysis:
        return (
            f"📄 დოკუმენტი #{document_id}\n"
            f"📎 {filename}\n\n"
            "🤖 AI ანალიზი ჯერ არ არის შესრულებული."
        )

    return (
        f"📄 დოკუმენტი #{document_id}\n"
        f"📎 {filename}\n\n"
        f"{analysis}"
    )


print(
    "GENIOSA 4.0 — PART 6/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 7/12
# Gemini AI engine, prompts, context and memory automation
# ============================================================


def extract_gemini_text(
    response_data: Dict[str, Any],
) -> str:
    """
    Extracts generated text from Gemini API response.
    """
    if not isinstance(response_data, dict):
        return ""

    candidates = response_data.get("candidates") or []

    if not isinstance(candidates, list):
        return ""

    parts = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        content = candidate.get("content") or {}

        if not isinstance(content, dict):
            continue

        content_parts = content.get("parts") or []

        if not isinstance(content_parts, list):
            continue

        for part in content_parts:
            if not isinstance(part, dict):
                continue

            text_value = part.get("text")

            if text_value is None:
                continue

            text_value = str(
                text_value
            ).strip()

            if text_value:
                parts.append(
                    text_value
                )

    return "\n".join(parts).strip()


def extract_gemini_error(
    response_data: Any,
) -> str:
    """
    Extracts a readable Gemini API error.
    """
    if isinstance(response_data, dict):
        error_data = response_data.get("error")

        if isinstance(error_data, dict):
            message = error_data.get("message")

            if message:
                return str(
                    message
                ).strip()[:2000]

            status = error_data.get("status")

            if status:
                return str(
                    status
                ).strip()[:500]

            code = error_data.get("code")

            if code:
                return (
                    f"Gemini API error code: {code}"
                )

        direct_message = response_data.get(
            "message"
        )

        if direct_message:
            return str(
                direct_message
            ).strip()[:2000]

    if response_data:
        return str(
            response_data
        )[:2000]

    return "Unknown Gemini API error."


def _safe_generation_temperature(
    temperature: Any,
    default: float = 0.35,
) -> float:
    """
    Normalizes Gemini temperature.
    """
    try:
        value = float(
            temperature
        )

    except (
        TypeError,
        ValueError,
    ):
        value = default

    if not (-1000000 < value < 1000000):
        value = default

    return max(
        0.0,
        min(
            1.0,
            value,
        ),
    )


def _safe_max_output_tokens(
    value: Any,
    default: int = 8192,
) -> int:
    """
    Normalizes Gemini output token limit.
    """
    try:
        tokens = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        tokens = default

    return max(
        256,
        min(
            tokens,
            32768,
        ),
    )


def gemini_generate(
    prompt: str,
    model: Optional[str] = None,
    temperature: float = 0.35,
    max_output_tokens: int = 8192,
) -> str:
    """
    Sends a text prompt to Gemini and returns
    generated text.
    """
    if not GEMINI_API_KEY:
        return (
            "❌ GEMINI_API_KEY არ არის "
            "კონფიგურირებული."
        )

    prompt_text = str(
        prompt or ""
    ).strip()

    if not prompt_text:
        return (
            "❌ Gemini-სთვის გასაგზავნი "
            "ტექსტი ცარიელია."
        )

    selected_model = str(
        model
        or GEMINI_MODEL
        or "gemini-2.5-flash-lite"
    ).strip()

    if not selected_model:
        return (
            "❌ Gemini მოდელი "
            "კონფიგურირებული არ არის."
        )

    url = gemini_api_url(
        selected_model
    )

    generation_temperature = (
        _safe_generation_temperature(
            temperature
        )
    )

    output_tokens = (
        _safe_max_output_tokens(
            max_output_tokens
        )
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt_text
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": generation_temperature,
            "maxOutputTokens": output_tokens,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=GEMINI_TIMEOUT,
        )

        try:
            response_data = response.json()

        except Exception:
            response_data = {
                "error": (
                    response.text[:4000]
                    if response.text
                    else "Empty Gemini response."
                )
            }

        if not (
            200
            <= response.status_code
            < 300
        ):
            error_message = extract_gemini_error(
                response_data
            )

            logger.error(
                "Gemini API HTTP %s: %s",
                response.status_code,
                error_message,
            )

            return (
                "❌ Gemini API შეცდომა.\n"
                f"HTTP {response.status_code}\n"
                f"{error_message}"
            )

        generated_text = extract_gemini_text(
            response_data
        )

        if generated_text:
            return generated_text

        finish_reason = ""

        try:
            candidates = (
                response_data.get(
                    "candidates"
                ) or []
            )

            if candidates:
                first_candidate = candidates[0]

                if isinstance(
                    first_candidate,
                    dict,
                ):
                    finish_reason = str(
                        first_candidate.get(
                            "finishReason",
                            "",
                        )
                    ).strip()

        except Exception:
            finish_reason = ""

        if finish_reason:
            logger.warning(
                "Gemini returned no text. "
                "Finish reason: %s",
                finish_reason,
            )

        return (
            "❌ Gemini-მ ტექსტური პასუხი "
            "ვერ დააბრუნა."
        )

    except requests.Timeout:
        logger.error(
            "Gemini request timed out."
        )

        return (
            "❌ Gemini-სთან კავშირის დრო "
            "ამოიწურა. სცადე თავიდან."
        )

    except requests.RequestException as exc:
        logger.error(
            "Gemini request failed: %s",
            exc,
        )

        return (
            "❌ Gemini-სთან დაკავშირება "
            "ვერ მოხერხდა."
        )

    except Exception as exc:
        logger.exception(
            "Unexpected Gemini error: %s",
            exc,
        )

        return (
            "❌ AI დამუშავებისას მოხდა "
            "მოულოდნელი შეცდომა."
        )


def ai_system_prompt() -> str:
    """
    Core Geniosa AI identity and behavior instructions.
    """
    return """
You are GENIOSA 4.0.

You are a professional business,
investment, real-estate, construction,
financial-analysis and strategic-intelligence
assistant.

Your primary purpose is to help the user:

- analyze businesses and projects
- analyze investment opportunities
- evaluate real-estate projects
- calculate project economics
- prepare investor materials
- evaluate investors and deals
- analyze uploaded documents
- build financial models
- identify risks
- identify opportunities
- compare scenarios
- make practical business decisions

IMPORTANT RULES:

1. Never invent facts when reliable information
   is not available.

2. Clearly distinguish between:
   - known facts
   - calculations
   - assumptions
   - estimates
   - recommendations

3. When the user provides numbers,
   use those numbers unless there is a clear
   mathematical inconsistency.

4. If numbers conflict, point out the conflict
   and explain the correct calculation.

5. When calculating financial results,
   show the formula or logic when useful.

6. Be commercially practical.
   Do not give vague motivational answers.

7. For investment analysis, consider:
   capital required,
   investor return,
   ownership,
   profit distribution,
   financing cost,
   debt,
   cash flow,
   exit strategy,
   risks,
   sensitivity and downside scenarios.

8. For real-estate projects consider:
   land,
   construction,
   saleable area,
   construction cost,
   sales price,
   revenue,
   financing,
   operating expenses,
   profit,
   margin,
   timing and market positioning.

9. For investor discussions consider:
   investor profile,
   investment capacity,
   sector,
   geography,
   proposed share,
   valuation,
   deal stage and next step.

10. If the user asks for a recommendation,
    give a clear recommendation and explain why.

11. Answer in the language used by the user.
    Georgian -> Georgian.
    Russian -> Russian.
    English -> English.

12. Keep answers structured and readable.
    Use headings, numbered points and tables
    when they improve clarity.

13. Do not claim to have searched the internet,
    contacted an investor, sent an email,
    booked something or completed an external
    action unless that action was actually performed.

14. Treat Geniosa's stored business information
    as context, not as unquestionable truth.
    If something appears inconsistent,
    flag it.

15. Protect the user's data and do not expose
    internal database details unless explicitly
    requested.
""".strip()


def build_ai_prompt(
    chat_id: Any,
    user_message: str,
    extra_context: str = "",
) -> str:
    """
    Builds the complete prompt sent to Gemini.
    """
    message_text = str(
        user_message or ""
    ).strip()

    if not message_text:
        return (
            ai_system_prompt()
            + "\n\nUSER'S CURRENT REQUEST:\n"
            + "No request provided."
        )

    try:
        context = build_full_ai_context(
            chat_id
        )

    except Exception as exc:
        logger.error(
            "Failed to build AI context: %s",
            exc,
        )

        context = ""

    extra = str(
        extra_context or ""
    ).strip()

    prompt_parts = [
        ai_system_prompt(),
        "",
        "CURRENT GENIOSA BUSINESS CONTEXT:",
        context or "No stored context.",
        "",
    ]

    if extra:
        prompt_parts.extend([
            "ADDITIONAL TASK CONTEXT:",
            extra,
            "",
        ])

    prompt_parts.extend([
        "USER'S CURRENT REQUEST:",
        message_text,
        "",
        "Provide the best practical answer.",
    ])

    return "\n".join(
        prompt_parts
    ).strip()


def process_ai_text(
    text: str,
) -> str:
    """
    Cleans AI output before sending it to Telegram.
    """
    value = str(
        text or ""
    ).strip()

    if not value:
        return (
            "❌ AI-მ ცარიელი პასუხი დააბრუნა."
        )

    value = value.replace(
        "\x00",
        "",
    )

    value = re.sub(
        r"\n{4,}",
        "\n\n\n",
        value,
    )

    return value.strip()


def extract_memory_candidates(
    user_message: str,
    ai_response: str,
) -> List[Tuple[str, str, int]]:
    """
    Detects explicit long-term business facts
    that are useful to store in memory.

    Returns:
        [(memory, category, importance), ...]
    """
    user_text = str(
        user_message or ""
    ).strip()

    response_text = str(
        ai_response or ""
    ).strip()

    candidates = []

    if not user_text:
        return candidates

    normalized = user_text.lower()

    project_keywords = [
        "პროექტი",
        "project",
        "инвестор",
        "ინვესტორი",
        "investor",
        "კომპანია",
        "company",
        "კომპანიის",
        "შევინახოთ",
        "დაიმახსოვრე",
        "remember",
        "save this",
        "from now on",
        "ამიერიდან",
        "შემდეგშიც",
    ]

    explicit_memory = any(
        keyword in normalized
        for keyword in project_keywords
    )

    if not explicit_memory:
        return candidates

    memory_text = user_text

    if len(memory_text) > 2000:
        memory_text = (
            memory_text[:2000]
            .strip()
        )

    category = "business"

    if (
        "ინვესტორ" in normalized
        or "investor" in normalized
        or "инвестор" in normalized
    ):
        category = "investor"

    elif (
        "პროექტ" in normalized
        or "project" in normalized
    ):
        category = "project"

    elif (
        "კომპანი" in normalized
        or "company" in normalized
    ):
        category = "company"

    candidates.append(
        (
            memory_text,
            category,
            8,
        )
    )

    return candidates


def maybe_save_important_memory(
    chat_id: Any,
    user_message: str,
    ai_response: str,
) -> int:
    """
    Saves explicit or clearly useful long-term
    business information into Geniosa memory.
    """
    candidates = extract_memory_candidates(
        user_message,
        ai_response,
    )

    saved_count = 0

    for (
        memory_text,
        category,
        importance,
    ) in candidates:

        if not memory_text:
            continue

        try:
            saved = save_memory(
                chat_id=chat_id,
                memory=memory_text,
                category=category,
                importance=importance,
            )

            if saved is not False:
                saved_count += 1

        except Exception as exc:
            logger.error(
                "Failed to save AI memory: %s",
                exc,
            )

    return saved_count


def ask_geniosa(
    chat_id: Any,
    user_message: str,
    extra_context: str = "",
) -> str:
    """
    Main Geniosa AI entry point.
    """
    message_text = str(
        user_message or ""
    ).strip()

    if not message_text:
        return (
            "❌ შეტყობინება ცარიელია."
        )

    try:
        prompt = build_ai_prompt(
            chat_id=chat_id,
            user_message=message_text,
            extra_context=extra_context,
        )

        raw_response = gemini_generate(
            prompt=prompt,
            temperature=0.35,
            max_output_tokens=8192,
        )

        response_text = process_ai_text(
            raw_response
        )

        if (
            response_text.startswith(
                "❌ Gemini"
            )
            or response_text.startswith(
                "❌ AI"
            )
        ):
            return response_text

        maybe_save_important_memory(
            chat_id=chat_id,
            user_message=message_text,
            ai_response=response_text,
        )

        return response_text

    except Exception as exc:
        logger.exception(
            "ask_geniosa failed: %s",
            exc,
        )

        return (
            "❌ Geniosa-ს AI დამუშავებისას "
            "მოხდა შეცდომა."
        )


def analyze_document_with_ai(
    chat_id: Any,
    document_id: int,
) -> str:
    """
    Loads a stored document, sends it to Gemini,
    and stores the resulting analysis.
    """
    try:
        safe_document_id = int(
            document_id
        )

    except (
        TypeError,
        ValueError,
    ):
        return (
            "❌ დოკუმენტის ID არასწორია."
        )

    if safe_document_id <= 0:
        return (
            "❌ დოკუმენტის ID არასწორია."
        )

    try:
        document = get_document(
            chat_id,
            safe_document_id,
        )

    except Exception as exc:
        logger.exception(
            "Failed to load document %s: %s",
            safe_document_id,
            exc,
        )

        return (
            "❌ დოკუმენტის მონაცემების "
            "წაკითხვა ვერ მოხერხდა."
        )

    if not document:
        return (
            "❌ დოკუმენტი ვერ მოიძებნა."
        )

    extracted_text = str(
        document.get("extracted_text")
        or ""
    ).strip()

    if not extracted_text:
        return (
            "❌ დოკუმენტში წაკითხვადი ტექსტი "
            "ვერ მოიძებნა."
        )

    try:
        prompt = build_document_analysis_prompt(
            document=document,
            chat_id=chat_id,
        )

    except Exception as exc:
        logger.exception(
            "Failed to build document prompt "
            "for document %s: %s",
            safe_document_id,
            exc,
        )

        return (
            "❌ დოკუმენტის AI ანალიზის "
            "მომზადება ვერ მოხერხდა."
        )

    analysis = gemini_generate(
        prompt=prompt,
        temperature=0.20,
        max_output_tokens=12000,
    )

    analysis = process_ai_text(
        analysis
    )

    if analysis.startswith("❌"):
        return analysis

    try:
        saved = update_document_analysis(
            chat_id=chat_id,
            document_id=safe_document_id,
            analysis=analysis,
        )

        if not saved:
            logger.warning(
                "Document analysis generated but "
                "could not be saved. document_id=%s",
                safe_document_id,
            )

    except Exception as exc:
        logger.exception(
            "Failed to save document analysis "
            "for document %s: %s",
            safe_document_id,
            exc,
        )

    return analysis


print(
    "GENIOSA 4.0 — PART 7/12 LOADED"
)


# ============================================================
# GENIOSA 4.0 — PART 8/12
# Telegram files, photos and multimodal AI analysis
# ============================================================


DOWNLOAD_DIR = Path(
    globals().get(
        "DOWNLOAD_DIR",
        TEMP_DIR / "downloads",
    )
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


TELEGRAM_FILE_TIMEOUT = int(
    globals().get(
        "TELEGRAM_FILE_TIMEOUT",
        max(
            60,
            int(TELEGRAM_REQUEST_TIMEOUT),
        ),
    )
)


SUPPORTED_DOCUMENT_TYPES = set(
    globals().get(
        "SUPPORTED_DOCUMENT_TYPES",
        {
            "pdf",
            "docx",
            "xlsx",
            "pptx",
            "txt",
            "csv",
        },
    )
)


MAX_FILE_DOWNLOAD_BYTES = int(
    globals().get(
        "MAX_DOCUMENT_SIZE_BYTES",
        25 * 1024 * 1024,
    )
)


def telegram_api_request(
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Generic Telegram Bot API request.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "Telegram token is not configured."
        )
        return None

    method_name = str(
        method or ""
    ).strip()

    if not method_name:
        return None

    request_payload = (
        payload
        if isinstance(
            payload,
            dict,
        )
        else {}
    )

    request_timeout = (
        TELEGRAM_REQUEST_TIMEOUT
        if timeout is None
        else max(
            5,
            int(timeout),
        )
    )

    try:
        response = requests.post(
            telegram_api_url(
                method_name
            ),
            json=request_payload,
            timeout=request_timeout,
        )

        if response.status_code != 200:
            logger.error(
                "Telegram API HTTP %s for %s: %s",
                response.status_code,
                method_name,
                response.text[:2000],
            )
            return None

        try:
            data = response.json()

        except Exception as exc:
            logger.error(
                "Telegram returned invalid JSON "
                "for %s: %s",
                method_name,
                exc,
            )
            return None

        if not isinstance(
            data,
            dict,
        ):
            logger.error(
                "Telegram returned invalid response "
                "type for %s.",
                method_name,
            )
            return None

        if not data.get("ok"):
            logger.error(
                "Telegram API error for %s: %s",
                method_name,
                data,
            )
            return None

        return data

    except requests.Timeout:
        logger.error(
            "Telegram API timeout for %s",
            method_name,
        )
        return None

    except requests.RequestException as exc:
        logger.error(
            "Telegram API request failed for %s: %s",
            method_name,
            exc,
        )
        return None

    except Exception as exc:
        logger.exception(
            "Unexpected Telegram API error for %s: %s",
            method_name,
            exc,
        )
        return None


def telegram_get_file(
    file_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Gets Telegram file metadata.
    """
    safe_file_id = str(
        file_id or ""
    ).strip()

    if not safe_file_id:
        return None

    data = telegram_api_request(
        "getFile",
        {
            "file_id": safe_file_id,
        },
        timeout=TELEGRAM_REQUEST_TIMEOUT,
    )

    if not data:
        return None

    result = data.get(
        "result"
    )

    if not isinstance(
        result,
        dict,
    ):
        return None

    return result


def _safe_download_filename(
    original_name: str,
    fallback_name: str,
) -> str:
    """
    Produces a filesystem-safe filename.
    """
    value = str(
        original_name or ""
    ).strip()

    if not value:
        value = fallback_name

    value = Path(value).name

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value,
    )

    safe_name = safe_name.strip(
        "._"
    )

    if not safe_name:
        safe_name = fallback_name

    return safe_name[:180]


def telegram_download_file(
    file_id: str,
    destination_dir: Optional[Path] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    """
    Downloads a Telegram file into local storage.

    The download is streamed and limited by the configured
    maximum document size.
    """
    safe_file_id = str(
        file_id or ""
    ).strip()

    if not safe_file_id:
        return None

    file_info = telegram_get_file(
        safe_file_id
    )

    if not file_info:
        return None

    file_path = str(
        file_info.get(
            "file_path"
        ) or ""
    ).strip()

    if not file_path:
        logger.error(
            "Telegram returned no file_path."
        )
        return None

    telegram_file_size = file_info.get(
        "file_size"
    )

    try:
        if telegram_file_size is not None:
            if int(
                telegram_file_size
            ) > MAX_FILE_DOWNLOAD_BYTES:
                logger.warning(
                    "Telegram file exceeds "
                    "maximum allowed size: %s",
                    telegram_file_size,
                )
                return None

    except (
        TypeError,
        ValueError,
    ):
        pass

    target_dir = (
        Path(destination_dir)
        if destination_dir is not None
        else DOWNLOAD_DIR
    )

    try:
        target_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    except Exception as exc:
        logger.error(
            "Could not create download directory: %s",
            exc,
        )
        return None

    safe_name = _safe_download_filename(
        filename
        or Path(file_path).name,
        f"telegram_{safe_file_id}",
    )

    timestamp = int(
        time.time() * 1000
    )

    destination = (
        target_dir
        / f"{timestamp}_{safe_name}"
    )

    download_url = (
        f"{TELEGRAM_FILE_BASE.rstrip('/')}/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{file_path.lstrip('/')}"
    )

    temporary_destination = Path(
        f"{destination}.part"
    )

    total_downloaded = 0

    try:
        response = requests.get(
            download_url,
            timeout=TELEGRAM_FILE_TIMEOUT,
            stream=True,
        )

        if response.status_code != 200:
            logger.error(
                "Telegram file download HTTP %s: %s",
                response.status_code,
                response.text[:1000],
            )
            return None

        content_length = response.headers.get(
            "Content-Length"
        )

        try:
            if (
                content_length
                and int(content_length)
                > MAX_FILE_DOWNLOAD_BYTES
            ):
                logger.warning(
                    "Telegram response file is too large: %s",
                    content_length,
                )
                return None

        except (
            TypeError,
            ValueError,
        ):
            pass

        with temporary_destination.open(
            "wb"
        ) as output_file:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if not chunk:
                    continue

                total_downloaded += len(
                    chunk
                )

                if (
                    total_downloaded
                    > MAX_FILE_DOWNLOAD_BYTES
                ):
                    logger.warning(
                        "Telegram file exceeded "
                        "maximum size while downloading."
                    )

                    return None

                output_file.write(
                    chunk
                )

        if (
            not temporary_destination.exists()
            or total_downloaded <= 0
        ):
            return None

        temporary_destination.replace(
            destination
        )

        if not destination.exists():
            return None

        if destination.stat().st_size <= 0:
            return None

        return str(
            destination
        )

    except requests.Timeout:
        logger.error(
            "Telegram file download timed out."
        )
        return None

    except requests.RequestException as exc:
        logger.error(
            "Telegram file download failed: %s",
            exc,
        )
        return None

    except Exception as exc:
        logger.exception(
            "Unexpected file download error: %s",
            exc,
        )
        return None

    finally:
        try:
            if temporary_destination.exists():
                temporary_destination.unlink()
        except Exception:
            pass


def guess_mime_type(
    filepath: str,
) -> str:
    """
    Returns a practical MIME type for common
    Telegram/Gemini-supported media.
    """
    extension = Path(
        filepath
    ).suffix.lower()

    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".avif": "image/avif",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".json": "application/json",
        ".docx": (
            "application/vnd.openxmlformats-"
            "officedocument.wordprocessingml.document"
        ),
        ".xlsx": (
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        ),
        ".pptx": (
            "application/vnd.openxmlformats-"
            "officedocument.presentationml.presentation"
        ),
    }

    return mime_map.get(
        extension,
        "application/octet-stream",
    )


def encode_file_base64(
    filepath: str,
) -> Optional[str]:
    """
    Encodes a local file into base64.
    """
    path = Path(
        filepath
    )

    if not path.exists():
        return None

    try:
        if not path.is_file():
            return None

        data = path.read_bytes()

        if not data:
            return None

        if len(data) > MAX_FILE_DOWNLOAD_BYTES:
            logger.warning(
                "File exceeds maximum size "
                "for base64 encoding: %s",
                filepath,
            )
            return None

        return base64.b64encode(
            data
        ).decode("utf-8")

    except Exception as exc:
        logger.error(
            "Failed to base64 encode file: %s",
            exc,
        )
        return None


def gemini_generate_multimodal(
    prompt: str,
    filepath: str,
    mime_type: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.25,
    max_output_tokens: int = 8192,
) -> str:
    """
    Sends text + file bytes to Gemini.
    """
    if not GEMINI_API_KEY:
        return (
            "❌ GEMINI_API_KEY არ არის "
            "კონფიგურირებული."
        )

    safe_prompt = str(
        prompt or ""
    ).strip()

    if not safe_prompt:
        return (
            "❌ AI მოთხოვნა ცარიელია."
        )

    path = Path(
        filepath
    )

    if not path.exists():
        return (
            "❌ ფაილი ვერ მოიძებნა."
        )

    if not path.is_file():
        return (
            "❌ მითითებული ობიექტი ფაილი არ არის."
        )

    try:
        file_size = path.stat().st_size

        if file_size <= 0:
            return (
                "❌ ფაილი ცარიელია."
            )

        if file_size > MAX_FILE_DOWNLOAD_BYTES:
            return (
                "❌ ფაილი დასაშვებ ზომაზე "
                "დიდია."
            )

    except Exception:
        return (
            "❌ ფაილის ზომის შემოწმება "
            "ვერ მოხერხდა."
        )

    encoded_data = encode_file_base64(
        str(path)
    )

    if not encoded_data:
        return (
            "❌ ფაილის წაკითხვა "
            "ვერ მოხერხდა."
        )

    detected_mime = str(
        mime_type
        or guess_mime_type(
            str(path)
        )
    ).strip()

    if not detected_mime:
        detected_mime = (
            "application/octet-stream"
        )

    selected_model = str(
        model
        or GEMINI_MODEL
        or "gemini-2.5-flash-lite"
    ).strip()

    if not selected_model:
        return (
            "❌ Gemini მოდელი "
            "კონფიგურირებული არ არის."
        )

    url = gemini_api_url(
        selected_model
    )

    generation_temperature = (
        _safe_generation_temperature(
            temperature,
            default=0.25,
        )
    )

    output_tokens = (
        _safe_max_output_tokens(
            max_output_tokens,
            default=8192,
        )
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": safe_prompt
                    },
                    {
                        "inline_data": {
                            "mime_type": detected_mime,
                            "data": encoded_data,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": generation_temperature,
            "maxOutputTokens": output_tokens,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=GEMINI_TIMEOUT,
        )

        try:
            response_data = response.json()

        except Exception:
            response_data = {
                "error": (
                    response.text[:4000]
                    if response.text
                    else "Empty Gemini response."
                )
            }

        if not (
            200
            <= response.status_code
            < 300
        ):
            error_message = extract_gemini_error(
                response_data
            )

            logger.error(
                "Gemini multimodal HTTP %s: %s",
                response.status_code,
                error_message,
            )

            return (
                "❌ Gemini-ის მულტიმოდალური "
                "ანალიზი ვერ შესრულდა.\n"
                f"HTTP {response.status_code}\n"
                f"{error_message}"
            )

        generated_text = extract_gemini_text(
            response_data
        )

        if generated_text:
            return generated_text

        finish_reason = ""

        try:
            candidates = (
                response_data.get(
                    "candidates"
                ) or []
            )

            if candidates:
                first_candidate = candidates[0]

                if isinstance(
                    first_candidate,
                    dict,
                ):
                    finish_reason = str(
                        first_candidate.get(
                            "finishReason",
                            "",
                        )
                    ).strip()

        except Exception:
            pass

        if finish_reason:
            logger.warning(
                "Gemini multimodal returned "
                "no text. Finish reason: %s",
                finish_reason,
            )

        return (
            "❌ Gemini-მ მულტიმედიურ "
            "ფაილზე პასუხი ვერ დააბრუნა."
        )

    except requests.Timeout:
        logger.error(
            "Gemini multimodal request timed out."
        )

        return (
            "❌ სურათის ან ფაილის "
            "ანალიზისას დრო ამოიწურა."
        )

    except requests.RequestException as exc:
        logger.error(
            "Gemini multimodal request failed: %s",
            exc,
        )

        return (
            "❌ სურათის ან ფაილის "
            "ანალიზისას კავშირის შეცდომა მოხდა."
        )

    except Exception as exc:
        logger.exception(
            "Unexpected multimodal Gemini error: %s",
            exc,
        )

        return (
            "❌ სურათის ან ფაილის "
            "ანალიზისას მოულოდნელი შეცდომა მოხდა."
        )


def analyze_image_with_ai(
    chat_id: Any,
    filepath: str,
    user_caption: str = "",
) -> str:
    """
    Analyzes a Telegram image using Gemini.
    """
    path = Path(
        filepath
    )

    if not path.exists():
        return (
            "❌ სურათი ვერ მოიძებნა."
        )

    if not path.is_file():
        return (
            "❌ მითითებული ობიექტი "
            "სურათი არ არის."
        )

    try:
        if path.stat().st_size <= 0:
            return (
                "❌ სურათი ცარიელია."
            )

        if (
            path.stat().st_size
            > MAX_FILE_DOWNLOAD_BYTES
        ):
            return (
                "❌ სურათი დასაშვებ ზომაზე "
                "დიდია."
            )

    except Exception:
        return (
            "❌ სურათის ზომის შემოწმება "
            "ვერ მოხერხდა."
        )

    caption = str(
        user_caption or ""
    ).strip()

    try:
        context = build_full_ai_context(
            chat_id
        )

    except Exception as exc:
        logger.error(
            "Failed to build image AI context: %s",
            exc,
        )
        context = ""

    prompt = f"""
You are Geniosa 4.0, a professional
business, investment, construction,
real-estate and financial intelligence
assistant.

Analyze the attached image carefully.

The image may contain:
- a construction site
- architectural drawings
- plans
- property
- real estate
- financial tables
- charts
- contracts
- documents
- maps
- screenshots
- products
- business information

Identify all useful information that can
reasonably be extracted from the image.

If the image contains numbers, tables or
financial information, read them carefully
and clearly identify uncertainty where text
is unreadable.

Do not invent information.

If the image is related to a business or
construction project, provide practical
business implications and recommendations.

Answer in the language of the user's request.

USER CAPTION:
{caption or "No caption provided."}

GENIOSA BUSINESS CONTEXT:
{context or "No stored context."}
""".strip()

    result = gemini_generate_multimodal(
        prompt=prompt,
        filepath=str(path),
        mime_type=guess_mime_type(
            str(path)
        ),
        temperature=0.20,
        max_output_tokens=10000,
    )

    return process_ai_text(
        result
    )


def analyze_document_file(
    chat_id: Any,
    filepath: str,
    filename: Optional[str] = None,
    project_id: Optional[int] = None,
) -> Tuple[
    Optional[int],
    str,
]:
    """
    Extracts a document, stores it in CRM
    and returns document ID + extracted text.
    """
    path = Path(
        filepath
    )

    if not path.exists():
        return (
            None,
            "❌ ფაილი ვერ მოიძებნა.",
        )

    if not path.is_file():
        return (
            None,
            "❌ მითითებული ობიექტი "
            "ფაილი არ არის.",
        )

    try:
        file_size = path.stat().st_size

    except Exception:
        return (
            None,
            "❌ ფაილის ზომის წაკითხვა "
            "ვერ მოხერხდა.",
        )

    if file_size <= 0:
        return (
            None,
            "❌ ფაილი ცარიელია.",
        )

    if file_size > MAX_FILE_DOWNLOAD_BYTES:
        return (
            None,
            "❌ დოკუმენტი დასაშვებ ზომაზე "
            "დიდია.",
        )

    safe_filename = str(
        filename
        or path.name
    ).strip()

    if not safe_filename:
        safe_filename = path.name

    safe_filename = Path(
        safe_filename
    ).name

    file_type = detect_file_type(
        safe_filename
    )

    if file_type == "unknown":
        return (
            None,
            "❌ ამ ტიპის დოკუმენტი "
            "ჯერ არ არის მხარდაჭერილი.",
        )

    if (
        SUPPORTED_DOCUMENT_TYPES
        and file_type not in SUPPORTED_DOCUMENT_TYPES
    ):
        return (
            None,
            "❌ ამ ტიპის დოკუმენტი "
            "არ არის მხარდაჭერილი.",
        )

    try:
        extracted_text = extract_file_text(
            str(path),
            file_type=file_type,
        )

        document_id = save_document_record(
            chat_id=chat_id,
            filename=safe_filename,
            file_type=file_type,
            extracted_text=extracted_text,
            project_id=project_id,
        )

        if document_id is None:
            return (
                None,
                "❌ დოკუმენტის მონაცემთა ბაზაში "
                "შენახვა ვერ მოხერხდა.",
            )

        return (
            document_id,
            extracted_text,
        )

    except Exception as exc:
        logger.exception(
            "Failed to process document file: %s",
            exc,
        )

        return (
            None,
            "❌ დოკუმენტის დამუშავება "
            "ვერ მოხერხდა.",
        )


def file_size_mb(
    filepath: str,
) -> float:
    """
    Returns file size in megabytes.
    """
    try:
        size = Path(
            filepath
        ).stat().st_size

        if size < 0:
            return 0.0

        return float(
            size / (1024 * 1024)
        )

    except Exception:
        return 0.0


def cleanup_temp_file(
    filepath: Optional[str],
) -> None:
    """
    Safely removes temporary downloaded files.
    """
    if not filepath:
        return

    try:
        path = Path(
            filepath
        )

        if path.exists():
            if path.is_file():
                path.unlink()

    except Exception as exc:
        logger.warning(
            "Could not remove temporary file %s: %s",
            filepath,
            exc,
        )


print(
    "GENIOSA 4.0 — PART 8/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 9/12
# Financial analysis and investment calculations
# ============================================================


def safe_float(
    value: Any,
) -> Optional[float]:
    """
    Safely converts a value to float.
    Supports common financial number formats.
    """
    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    try:
        if isinstance(
            value,
            str,
        ):
            cleaned = value.strip()

            if not cleaned:
                return None

            cleaned = cleaned.replace(
                ",",
                "",
            )

            cleaned = cleaned.replace(
                "$",
                "",
            )

            cleaned = cleaned.replace(
                "€",
                "",
            )

            cleaned = cleaned.replace(
                "₾",
                "",
            )

            cleaned = cleaned.replace(
                "ლარი",
                "",
            )

            cleaned = cleaned.strip()

            if not cleaned:
                return None

            result = float(
                cleaned
            )

        else:
            result = float(
                value
            )

        if result != result:
            return None

        if result == float("inf"):
            return None

        if result == float("-inf"):
            return None

        return result

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None


def _safe_percentage(
    value: Any,
) -> Optional[float]:
    """
    Safely converts a value to a percentage.
    """
    result = safe_float(
        value
    )

    if result is None:
        return None

    if result < 0 or result > 100:
        return None

    return result


def calculate_revenue_from_area(
    saleable_area: Any,
    sale_price_per_m2: Any,
) -> Optional[float]:
    """
    Calculates revenue from saleable area
    and average selling price per m².
    """
    area = safe_float(
        saleable_area
    )

    price = safe_float(
        sale_price_per_m2
    )

    if area is None or price is None:
        return None

    if area < 0 or price < 0:
        return None

    return area * price


def calculate_construction_cost(
    construction_area: Any,
    construction_price_per_m2: Any,
) -> Optional[float]:
    """
    Calculates construction cost.
    """
    area = safe_float(
        construction_area
    )

    price = safe_float(
        construction_price_per_m2
    )

    if area is None or price is None:
        return None

    if area < 0 or price < 0:
        return None

    return area * price


def calculate_total_cost_from_components(
    land_cost: Any = 0,
    construction_cost: Any = 0,
    operating_cost: Any = 0,
    financing_cost: Any = 0,
    other_cost: Any = 0,
) -> float:
    """
    Calculates total project cost from
    individual cost components.

    Negative cost components are ignored.
    """
    components = [
        land_cost,
        construction_cost,
        operating_cost,
        financing_cost,
        other_cost,
    ]

    total = 0.0

    for component in components:
        value = safe_float(
            component
        )

        if value is not None and value > 0:
            total += value

    return total


def calculate_net_profit(
    revenue: Any,
    total_cost: Any,
) -> Optional[float]:
    """
    Calculates net profit.
    """
    revenue_value = safe_float(
        revenue
    )

    cost_value = safe_float(
        total_cost
    )

    if (
        revenue_value is None
        or cost_value is None
    ):
        return None

    return (
        revenue_value
        - cost_value
    )


def calculate_profit_margin(
    revenue: Any,
    profit: Any,
) -> Optional[float]:
    """
    Calculates profit margin as percentage
    of revenue.
    """
    revenue_value = safe_float(
        revenue
    )

    profit_value = safe_float(
        profit
    )

    if (
        revenue_value is None
        or profit_value is None
    ):
        return None

    if revenue_value <= 0:
        return None

    return (
        profit_value
        / revenue_value
    ) * 100


def calculate_roi(
    investment: Any,
    profit: Any,
) -> Optional[float]:
    """
    Calculates ROI based on invested capital.
    """
    investment_value = safe_float(
        investment
    )

    profit_value = safe_float(
        profit
    )

    if (
        investment_value is None
        or profit_value is None
    ):
        return None

    if investment_value <= 0:
        return None

    return (
        profit_value
        / investment_value
    ) * 100


def calculate_investor_profit(
    net_profit: Any,
    investor_share: Any,
) -> Optional[float]:
    """
    Calculates investor profit from net profit
    and investor profit-share percentage.
    """
    profit_value = safe_float(
        net_profit
    )

    share_value = _safe_percentage(
        investor_share
    )

    if (
        profit_value is None
        or share_value is None
    ):
        return None

    return (
        profit_value
        * share_value
        / 100
    )


def calculate_operator_profit(
    net_profit: Any,
    investor_share: Any,
) -> Optional[float]:
    """
    Calculates remaining operator/company profit.
    """
    profit_value = safe_float(
        net_profit
    )

    share_value = _safe_percentage(
        investor_share
    )

    if (
        profit_value is None
        or share_value is None
    ):
        return None

    operator_share = (
        100.0
        - share_value
    )

    return (
        profit_value
        * operator_share
        / 100
    )


def calculate_break_even_price(
    total_cost: Any,
    saleable_area: Any,
) -> Optional[float]:
    """
    Calculates break-even selling price per m².
    """
    cost_value = safe_float(
        total_cost
    )

    area_value = safe_float(
        saleable_area
    )

    if (
        cost_value is None
        or area_value is None
    ):
        return None

    if (
        cost_value < 0
        or area_value <= 0
    ):
        return None

    return (
        cost_value
        / area_value
    )


def calculate_required_revenue(
    total_cost: Any,
    target_profit_margin: Any,
) -> Optional[float]:
    """
    Calculates revenue required to achieve
    a target profit margin.

    Example:
        cost = 10M
        target margin = 20%
        required revenue = 12.5M
    """
    cost_value = safe_float(
        total_cost
    )

    margin_value = safe_float(
        target_profit_margin
    )

    if (
        cost_value is None
        or margin_value is None
    ):
        return None

    if cost_value < 0:
        return None

    if (
        margin_value < 0
        or margin_value >= 100
    ):
        return None

    denominator = (
        1
        - margin_value / 100
    )

    if denominator <= 0:
        return None

    return (
        cost_value
        / denominator
    )


def calculate_project_sensitivity(
    saleable_area: Any,
    total_cost: Any,
    base_price: Any,
    price_changes: Optional[
        List[float]
    ] = None,
) -> List[Dict[str, float]]:
    """
    Creates a sales-price sensitivity table.

    Negative prices are excluded.
    """
    area = safe_float(
        saleable_area
    )

    cost = safe_float(
        total_cost
    )

    price = safe_float(
        base_price
    )

    if (
        area is None
        or cost is None
        or price is None
    ):
        return []

    if (
        area <= 0
        or cost < 0
        or price < 0
    ):
        return []

    changes = (
        price_changes
        if price_changes is not None
        else [
            -20,
            -10,
            0,
            10,
            20,
        ]
    )

    results = []

    for change in changes:

        change_value = safe_float(
            change
        )

        if change_value is None:
            continue

        adjusted_price = (
            price
            * (
                1
                + change_value / 100
            )
        )

        if adjusted_price < 0:
            continue

        revenue = (
            area
            * adjusted_price
        )

        profit = (
            revenue
            - cost
        )

        margin = None

        if revenue > 0:
            margin = (
                profit
                / revenue
            ) * 100

        results.append(
            {
                "price_change": float(
                    change_value
                ),
                "price_per_m2": float(
                    adjusted_price
                ),
                "revenue": float(
                    revenue
                ),
                "profit": float(
                    profit
                ),
                "margin": float(
                    margin
                )
                if margin is not None
                else 0.0,
            }
        )

    return results


def analyze_project_financials(
    project: Dict[str, Any],
    sale_price_per_m2: Optional[
        float
    ] = None,
    investor_share: Optional[
        float
    ] = None,
) -> Dict[str, Any]:
    """
    Performs a complete financial analysis
    of a project record.
    """
    if not isinstance(
        project,
        dict,
    ):
        return {
            "success": False,
            "error": "Project not found.",
        }

    project_name = (
        project.get("name")
        or "Unnamed Project"
    )

    saleable_area = safe_float(
        project.get(
            "saleable_area"
        )
    )

    construction_area = safe_float(
        project.get(
            "construction_area"
        )
    )

    land_cost = (
        safe_float(
            project.get(
                "land_cost"
            )
        )
        or 0.0
    )

    construction_cost = (
        safe_float(
            project.get(
                "construction_cost"
            )
        )
        or 0.0
    )

    operating_cost = (
        safe_float(
            project.get(
                "operating_cost"
            )
        )
        or 0.0
    )

    financing_cost = (
        safe_float(
            project.get(
                "financing_cost"
            )
        )
        or 0.0
    )

    other_cost = (
        safe_float(
            project.get(
                "other_cost"
            )
        )
        or 0.0
    )

    stored_total_cost = safe_float(
        project.get(
            "total_cost"
        )
    )

    calculated_component_total = (
        calculate_total_cost_from_components(
            land_cost=land_cost,
            construction_cost=construction_cost,
            operating_cost=operating_cost,
            financing_cost=financing_cost,
            other_cost=other_cost,
        )
    )

    if stored_total_cost is not None:
        total_cost = max(
            0.0,
            stored_total_cost,
        )

    else:
        total_cost = (
            calculated_component_total
        )

    stored_revenue = safe_float(
        project.get(
            "revenue"
        )
    )

    stored_expected_revenue = safe_float(
        project.get(
            "expected_revenue"
        )
    )

    stored_profit = safe_float(
        project.get(
            "net_profit"
        )
    )

    stored_expected_profit = safe_float(
        project.get(
            "expected_profit"
        )
    )

    selected_price = safe_float(
        sale_price_per_m2
    )

    if (
        selected_price is not None
        and selected_price < 0
    ):
        selected_price = None

    if (
        selected_price is not None
        and saleable_area is not None
        and saleable_area > 0
    ):
        revenue = (
            saleable_area
            * selected_price
        )

    elif stored_revenue is not None:
        revenue = max(
            0.0,
            stored_revenue,
        )

    elif stored_expected_revenue is not None:
        revenue = max(
            0.0,
            stored_expected_revenue,
        )

    else:
        revenue = 0.0

    calculated_profit = (
        revenue
        - total_cost
    )

    if (
        selected_price is None
        and stored_profit is not None
    ):
        profit = stored_profit

    else:
        profit = calculated_profit

    margin = calculate_profit_margin(
        revenue,
        profit,
    )

    investor_share_value = None

    if investor_share is not None:
        investor_share_value = _safe_percentage(
            investor_share
        )

    if investor_share_value is None:
        investor_share_value = _safe_percentage(
            project.get(
                "investor_share"
            )
        )

    investor_profit = None
    operator_profit = None

    if investor_share_value is not None:

        investor_profit = (
            calculate_investor_profit(
                profit,
                investor_share_value,
            )
        )

        operator_profit = (
            calculate_operator_profit(
                profit,
                investor_share_value,
            )
        )

    investor_capital = safe_float(
        project.get(
            "investor_capital"
        )
    )

    if (
        investor_capital is not None
        and investor_capital <= 0
    ):
        investor_capital = None

    investor_roi = None

    if (
        investor_capital is not None
        and investor_profit is not None
    ):
        investor_roi = calculate_roi(
            investor_capital,
            investor_profit,
        )

    break_even_price = None

    if (
        saleable_area is not None
        and saleable_area > 0
    ):
        break_even_price = (
            calculate_break_even_price(
                total_cost,
                saleable_area,
            )
        )

    construction_cost_per_m2 = None

    if (
        construction_area is not None
        and construction_area > 0
        and construction_cost >= 0
    ):
        construction_cost_per_m2 = (
            construction_cost
            / construction_area
        )

    revenue_per_saleable_m2 = None

    if (
        saleable_area is not None
        and saleable_area > 0
        and revenue >= 0
    ):
        revenue_per_saleable_m2 = (
            revenue
            / saleable_area
        )

    sensitivity = []

    if (
        saleable_area is not None
        and saleable_area > 0
        and selected_price is not None
    ):
        sensitivity = (
            calculate_project_sensitivity(
                saleable_area=saleable_area,
                total_cost=total_cost,
                base_price=selected_price,
            )
        )

    return {
        "success": True,
        "project_name": str(
            project_name
        ),
        "saleable_area": saleable_area,
        "construction_area": construction_area,
        "land_cost": land_cost,
        "construction_cost": construction_cost,
        "operating_cost": operating_cost,
        "financing_cost": financing_cost,
        "other_cost": other_cost,
        "component_total_cost": (
            calculated_component_total
        ),
        "total_cost": total_cost,
        "revenue": revenue,
        "profit": profit,
        "margin": margin,
        "sale_price_per_m2": selected_price,
        "revenue_per_saleable_m2": (
            revenue_per_saleable_m2
        ),
        "construction_cost_per_m2": (
            construction_cost_per_m2
        ),
        "break_even_price": break_even_price,
        "investor_share": (
            investor_share_value
        ),
        "investor_capital": investor_capital,
        "investor_profit": investor_profit,
        "investor_roi": investor_roi,
        "operator_profit": operator_profit,
        "stored_revenue": stored_revenue,
        "stored_expected_revenue": (
            stored_expected_revenue
        ),
        "stored_profit": stored_profit,
        "stored_expected_profit": (
            stored_expected_profit
        ),
        "sensitivity": sensitivity,
    }


def financial_analysis_text(
    analysis: Dict[str, Any],
) -> str:
    """
    Converts financial analysis into
    a readable Telegram response.
    """
    if not isinstance(
        analysis,
        dict,
    ):
        return (
            "❌ ფინანსური ანალიზი "
            "ვერ შესრულდა."
        )

    if not analysis.get(
        "success"
    ):
        return (
            "❌ ფინანსური ანალიზი "
            "ვერ შესრულდა.\n"
            f"{analysis.get('error', '')}"
        )

    project_name = (
        analysis.get(
            "project_name"
        )
        or "პროექტი"
    )

    total_cost = analysis.get(
        "total_cost"
    )

    revenue = analysis.get(
        "revenue"
    )

    profit = analysis.get(
        "profit"
    )

    margin = analysis.get(
        "margin"
    )

    break_even_price = analysis.get(
        "break_even_price"
    )

    construction_cost_per_m2 = (
        analysis.get(
            "construction_cost_per_m2"
        )
    )

    sale_price_per_m2 = (
        analysis.get(
            "sale_price_per_m2"
        )
    )

    investor_share = analysis.get(
        "investor_share"
    )

    investor_capital = analysis.get(
        "investor_capital"
    )

    investor_profit = analysis.get(
        "investor_profit"
    )

    investor_roi = analysis.get(
        "investor_roi"
    )

    operator_profit = analysis.get(
        "operator_profit"
    )

    lines = [
        "📊 GENIOSA — ფინანსური ანალიზი",
        "",
        f"🏗️ პროექტი: {project_name}",
        "",
        "💰 ძირითადი მაჩვენებლები:",
        (
            "• ჯამური ღირებულება: "
            f"{format_money(total_cost)}"
        ),
        (
            "• შემოსავალი: "
            f"{format_money(revenue)}"
        ),
        (
            "• წმინდა მოგება: "
            f"{format_money(profit)}"
        ),
    ]

    if margin is not None:
        lines.append(
            "• მოგების მარჟა: "
            f"{format_number(margin)}%"
        )

    if sale_price_per_m2 is not None:
        lines.append(
            "• გაყიდვის ფასი: "
            f"{format_money(sale_price_per_m2)}/მ²"
        )

    if break_even_price is not None:
        lines.append(
            "• Break-even ფასი: "
            f"{format_money(break_even_price)}/მ²"
        )

    if (
        construction_cost_per_m2
        is not None
    ):
        lines.append(
            "• მშენებლობის ღირებულება: "
            f"{format_money(construction_cost_per_m2)}/მ²"
        )

    if investor_share is not None:
        lines.extend([
            "",
            "👤 ინვესტორი:",
            (
                "• წილი: "
                f"{format_number(investor_share)}%"
            ),
        ])

    if investor_capital is not None:
        lines.append(
            "• ინვესტირებული კაპიტალი: "
            f"{format_money(investor_capital)}"
        )

    if investor_profit is not None:
        lines.append(
            "• ინვესტორის მოგება: "
            f"{format_money(investor_profit)}"
        )

    if investor_roi is not None:
        lines.append(
            "• ინვესტორის ROI: "
            f"{format_number(investor_roi)}%"
        )

    if operator_profit is not None:
        lines.append(
            "• ოპერატორის/კომპანიის მოგება: "
            f"{format_money(operator_profit)}"
        )

    return "\n".join(
        lines
    )


def save_financial_analysis(
    chat_id: Any,
    analysis: Dict[str, Any],
    project_id: Optional[int] = None,
    analysis_type: str = "project",
) -> Optional[int]:
    """
    Saves financial analysis to database.
    """
    if not isinstance(
        analysis,
        dict,
    ):
        return None

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )

            if project_id_value <= 0:
                project_id_value = None

        except (
            TypeError,
            ValueError,
        ):
            project_id_value = None

    analysis_type_value = str(
        analysis_type
        or "project"
    ).strip()

    if not analysis_type_value:
        analysis_type_value = "project"

    input_data = {
        "project_name": analysis.get(
            "project_name"
        ),
        "saleable_area": analysis.get(
            "saleable_area"
        ),
        "construction_area": analysis.get(
            "construction_area"
        ),
        "sale_price_per_m2": analysis.get(
            "sale_price_per_m2"
        ),
        "investor_share": analysis.get(
            "investor_share"
        ),
        "investor_capital": analysis.get(
            "investor_capital"
        ),
        "total_cost": analysis.get(
            "total_cost"
        ),
    }

    result_data = analysis

    try:
        result = db_execute(
            """
            INSERT INTO financial_analyses (
                chat_id,
                project_id,
                analysis_type,
                input_data,
                result_data
            )
            VALUES (
                %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                str(chat_id),
                project_id_value,
                analysis_type_value,
                json.dumps(
                    input_data,
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(
                    result_data,
                    ensure_ascii=False,
                    default=str,
                ),
            ),
            fetchone=True,
            commit=True,
        )

        if result:
            try:
                return int(
                    result["id"]
                )
            except (
                TypeError,
                ValueError,
                KeyError,
            ):
                return None

    except Exception as exc:
        logger.error(
            "Failed to save financial analysis: %s",
            exc,
        )

    return None


def run_project_financial_analysis(
    chat_id: Any,
    project_id: int,
    sale_price_per_m2: Optional[
        float
    ] = None,
    investor_share: Optional[
        float
    ] = None,
) -> Dict[str, Any]:
    """
    Loads a project, performs financial analysis,
    and saves the result.
    """
    try:
        safe_project_id = int(
            project_id
        )

    except (
        TypeError,
        ValueError,
    ):
        return {
            "success": False,
            "error": "Invalid project ID.",
        }

    if safe_project_id <= 0:
        return {
            "success": False,
            "error": "Invalid project ID.",
        }

    project = get_project(
        chat_id,
        safe_project_id,
    )

    if not project:
        return {
            "success": False,
            "error": "Project not found.",
        }

    analysis = analyze_project_financials(
        project=project,
        sale_price_per_m2=sale_price_per_m2,
        investor_share=investor_share,
    )

    if analysis.get(
        "success"
    ):
        analysis_id = save_financial_analysis(
            chat_id=chat_id,
            project_id=safe_project_id,
            analysis=analysis,
            analysis_type="project",
        )

        analysis["analysis_id"] = (
            analysis_id
        )

    return analysis


print(
    "GENIOSA 4.0 — PART 9/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 10/12
# File generation: Excel, PowerPoint and asset management
# ============================================================


# ------------------------------------------------------------
# PART 10 — SAFE CONFIGURATION
# ------------------------------------------------------------

MAX_GENERATED_ASSETS = int(
    globals().get(
        "MAX_GENERATED_ASSETS",
        100,
    )
)

MAX_GENERATED_ASSETS = max(
    1,
    min(
        MAX_GENERATED_ASSETS,
        1000,
    ),
)


# PART 1 defines GENERATED_DIR.
# Keep GENERATION_DIR as a compatibility alias because
# older parts may still reference this name.
GENERATED_DIR = Path(
    globals().get(
        "GENERATED_DIR",
        Path(
            globals().get(
                "BASE_DIR",
                Path.cwd(),
            )
        ) / "generated",
    )
)

GENERATED_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

GENERATION_DIR = GENERATED_DIR


# ------------------------------------------------------------
# OPTIONAL LIBRARY CHECKS
# ------------------------------------------------------------

WORKBOOK_CLASS = globals().get(
    "Workbook"
)

PRESENTATION_CLASS = globals().get(
    "Presentation"
)


# ------------------------------------------------------------
# INTERNAL HELPERS
# ------------------------------------------------------------

def _safe_generated_project_name(
    project_name: Any,
) -> str:
    """
    Creates a filesystem-safe project name.
    """

    name = str(
        project_name
        or "project"
    ).strip()

    # Replace whitespace with underscores.
    name = re.sub(
        r"\s+",
        "_",
        name,
    )

    # Keep Unicode letters where possible,
    # but remove filesystem-dangerous characters.
    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = re.sub(
        r"[^0-9A-Za-z_\-\u0080-\uFFFF]+",
        "_",
        name,
    )

    name = re.sub(
        r"_+",
        "_",
        name,
    ).strip(
        "._-"
    )

    if not name:
        name = "project"

    return name[:120]


def _generated_timestamp() -> str:
    """
    Returns a high-resolution timestamp string
    suitable for generated filenames.
    """

    return str(
        int(
            time.time() * 1000
        )
    )


def _ensure_generated_directory() -> Path:
    """
    Ensures the generated assets directory exists.
    """

    directory = Path(
        globals().get(
            "GENERATED_DIR",
            Path.cwd() / "generated",
        )
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory


def _set_cell_bold(
    cell: Any,
    size: Optional[int] = None,
) -> None:
    """
    Safely applies bold formatting to an openpyxl cell.

    Avoids the deprecated Font.copy() method.
    """

    try:
        from copy import copy

        font = copy(
            cell.font
        )

        font.bold = True

        if size is not None:
            font.sz = size

        cell.font = font

    except Exception:
        # Formatting failure must never crash
        # file generation.
        pass


def _excel_number_format(
    cell: Any,
) -> None:
    """
    Applies a standard numeric format.
    """

    try:
        if isinstance(
            cell.value,
            (int, float),
        ) and not isinstance(
            cell.value,
            bool,
        ):
            cell.number_format = "#,##0.00"

    except Exception:
        pass


def _validate_generated_file(
    filepath: Optional[str],
) -> bool:
    """
    Confirms that a generated file exists
    and has non-zero size.
    """

    if not filepath:
        return False

    try:
        path = Path(
            filepath
        )

        return (
            path.exists()
            and path.is_file()
            and path.stat().st_size > 0
        )

    except Exception:
        return False


# ------------------------------------------------------------
# NUMBER / MONEY FORMATTING
# ------------------------------------------------------------

def format_number(
    value: Any,
    decimals: int = 2,
) -> str:
    """
    Formats a numeric value for human-readable output.
    """

    number = safe_float(
        value
    )

    if number is None:
        return "0"

    try:
        decimals = int(
            decimals
        )
    except Exception:
        decimals = 2

    decimals = max(
        0,
        min(
            decimals,
            8,
        ),
    )

    if decimals == 0:
        return f"{number:,.0f}"

    formatted = (
        f"{number:,.{decimals}f}"
    )

    formatted = (
        formatted
        .rstrip("0")
        .rstrip(".")
    )

    return formatted


def format_money(
    value: Any,
    currency: str = "$",
    decimals: int = 0,
) -> str:
    """
    Formats money values.
    """

    number = safe_float(
        value
    )

    if number is None:
        return (
            f"{currency}0"
        )

    return (
        f"{currency}"
        f"{format_number(number, decimals)}"
    )


# ------------------------------------------------------------
# GENERATED ASSET DATABASE MANAGEMENT
# ------------------------------------------------------------

def generated_asset_save(
    chat_id: Any,
    filename: str,
    asset_type: str,
    description: str = "",
    project_id: Optional[int] = None,
) -> Optional[int]:
    """
    Saves generated file metadata into database.
    """

    if chat_id is None:
        return None

    safe_filename = Path(
        str(
            filename
            or "generated_file"
        )
    ).name

    if not safe_filename:
        safe_filename = (
            "generated_file"
        )

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )
        except Exception:
            project_id_value = None

    try:
        result = db_execute(
            """
            INSERT INTO generated_assets (
                chat_id,
                project_id,
                asset_type,
                filename,
                description
            )
            VALUES (
                %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                str(chat_id),
                project_id_value,
                str(
                    asset_type
                    or "file"
                )[:50],
                safe_filename[:255],
                str(
                    description
                    or ""
                )[:2000],
            ),
            fetchone=True,
            commit=True,
        )

        if result:
            try:
                return int(
                    result["id"]
                )
            except Exception:
                try:
                    return int(
                        result[0]
                    )
                except Exception:
                    pass

    except Exception as exc:
        logger.error(
            "Failed to save generated asset: %s",
            exc,
        )

    return None


def get_generated_assets(
    chat_id: Any,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Returns generated files for a chat.
    """

    if chat_id is None:
        return []

    try:
        safe_limit = max(
            1,
            min(
                int(limit),
                MAX_GENERATED_ASSETS,
            ),
        )

    except Exception:
        safe_limit = (
            MAX_GENERATED_ASSETS
        )

    try:
        rows = db_execute(
            f"""
            SELECT
                g.*,
                p.name AS project_name
            FROM generated_assets g
            LEFT JOIN projects p
                ON p.id = g.project_id
               AND p.chat_id = g.chat_id
            WHERE g.chat_id = %s
            ORDER BY
                g.created_at DESC,
                g.id DESC
            LIMIT {safe_limit}
            """,
            (
                str(chat_id),
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:
        logger.error(
            "Failed to get generated assets: %s",
            exc,
        )

        return []


def generated_assets_summary(
    chat_id: Any,
) -> str:
    """
    Creates a readable list of generated files.
    """

    assets = get_generated_assets(
        chat_id,
        limit=MAX_GENERATED_ASSETS,
    )

    if not assets:
        return (
            "📦 GENIOSA — გენერირებული ფაილები\n\n"
            "გენერირებული ფაილები ჯერ არ არის."
        )

    lines = [
        "📦 GENIOSA — გენერირებული ფაილები",
        "",
    ]

    for asset in assets:
        asset_id = asset.get(
            "id"
        )

        filename = (
            asset.get("filename")
            or "უცნობი ფაილი"
        )

        asset_type = (
            asset.get("asset_type")
            or "file"
        )

        project_name = (
            asset.get("project_name")
            or "პროექტთან მიბმული არაა"
        )

        lines.append(
            f"#{asset_id} — {filename}"
        )

        lines.append(
            f"📁 ტიპი: {asset_type}"
        )

        lines.append(
            f"🏗️ პროექტი: {project_name}"
        )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


# ------------------------------------------------------------
# EXCEL GENERATION
# ------------------------------------------------------------

def generate_project_excel(
    project: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Generates a professional project financial Excel file.
    """

    if not project:
        return None

    if WORKBOOK_CLASS is None:
        logger.error(
            "openpyxl Workbook is not available."
        )
        return None

    project_id = project.get(
        "id"
    )

    project_name = (
        project.get("name")
        or "Geniosa Project"
    )

    safe_project_name = (
        _safe_generated_project_name(
            project_name
        )
    )

    filename = (
        "Geniosa_Project_"
        f"{safe_project_name}_"
        f"{_generated_timestamp()}.xlsx"
    )

    filepath = (
        _ensure_generated_directory()
        / filename
    )

    if analysis is None:
        analysis = (
            analyze_project_financials(
                project
            )
            or {}
        )

    try:
        workbook = (
            WORKBOOK_CLASS()
        )

        summary_sheet = (
            workbook.active
        )

        summary_sheet.title = (
            "Summary"
        )

        summary_rows = [
            [
                "GENIOSA 4.0 — PROJECT FINANCIAL MODEL"
            ],
            [],
            [
                "Project",
                project_name,
            ],
            [
                "Project ID",
                project_id,
            ],
            [
                "Industry",
                project.get(
                    "industry"
                ) or "",
            ],
            [
                "Location",
                project.get(
                    "location"
                ) or "",
            ],
            [],
            [
                "AREA & DEVELOPMENT"
            ],
            [
                "Land Area (m²)",
                project.get(
                    "land_area"
                ),
            ],
            [
                "Saleable Area (m²)",
                project.get(
                    "saleable_area"
                ),
            ],
            [
                "Construction Area (m²)",
                project.get(
                    "construction_area"
                ),
            ],
            [
                "Total Area (m²)",
                project.get(
                    "total_area"
                ),
            ],
            [],
            [
                "COST STRUCTURE"
            ],
            [
                "Land Cost",
                project.get(
                    "land_cost"
                ),
            ],
            [
                "Construction Cost",
                project.get(
                    "construction_cost"
                ),
            ],
            [
                "Operating Cost",
                project.get(
                    "operating_cost"
                ),
            ],
            [
                "Financing Cost",
                project.get(
                    "financing_cost"
                ),
            ],
            [
                "Other Cost",
                project.get(
                    "other_cost"
                ),
            ],
            [
                "Total Cost",
                analysis.get(
                    "total_cost"
                ),
            ],
            [],
            [
                "REVENUE & PROFIT"
            ],
            [
                "Revenue",
                analysis.get(
                    "revenue"
                ),
            ],
            [
                "Net Profit",
                analysis.get(
                    "profit"
                ),
            ],
            [
                "Profit Margin (%)",
                analysis.get(
                    "margin"
                ),
            ],
            [
                "Break-even Price / m²",
                analysis.get(
                    "break_even_price"
                ),
            ],
            [],
            [
                "INVESTOR"
            ],
            [
                "Investor Capital",
                analysis.get(
                    "investor_capital"
                ),
            ],
            [
                "Investor Share (%)",
                analysis.get(
                    "investor_share"
                ),
            ],
            [
                "Investor Profit",
                analysis.get(
                    "investor_profit"
                ),
            ],
            [
                "Investor ROI (%)",
                analysis.get(
                    "investor_roi"
                ),
            ],
        ]

        for row in summary_rows:
            summary_sheet.append(
                row
            )

        summary_sheet.column_dimensions[
            "A"
        ].width = 34

        summary_sheet.column_dimensions[
            "B"
        ].width = 30

        # Main title.
        if summary_sheet.max_row >= 1:
            for cell in (
                summary_sheet[1]
            ):
                _set_cell_bold(
                    cell,
                    size=14,
                )

        section_titles = {
            "AREA & DEVELOPMENT",
            "COST STRUCTURE",
            "REVENUE & PROFIT",
            "INVESTOR",
        }

        for row_number in range(
            1,
            summary_sheet.max_row + 1,
        ):
            first_cell = (
                summary_sheet.cell(
                    row=row_number,
                    column=1,
                )
            )

            if first_cell.value in (
                section_titles
            ):
                _set_cell_bold(
                    first_cell
                )

        for row_number in range(
            1,
            summary_sheet.max_row + 1,
        ):
            value_cell = (
                summary_sheet.cell(
                    row=row_number,
                    column=2,
                )
            )

            _excel_number_format(
                value_cell
            )

        # Freeze top row.
        summary_sheet.freeze_panes = (
            "A2"
        )

        # ----------------------------------------------------
        # SENSITIVITY SHEET
        # ----------------------------------------------------

        sensitivity_sheet = (
            workbook.create_sheet(
                "Sensitivity"
            )
        )

        sensitivity_sheet.append([
            "Price Change (%)",
            "Price / m²",
            "Revenue",
            "Profit",
            "Margin (%)",
        ])

        sensitivity = (
            analysis.get(
                "sensitivity"
            )
            or []
        )

        for item in sensitivity:
            if not isinstance(
                item,
                dict,
            ):
                continue

            sensitivity_sheet.append([
                item.get(
                    "price_change"
                ),
                item.get(
                    "price_per_m2"
                ),
                item.get(
                    "revenue"
                ),
                item.get(
                    "profit"
                ),
                item.get(
                    "margin"
                ),
            ])

        for column in (
            "A",
            "B",
            "C",
            "D",
            "E",
        ):
            sensitivity_sheet.column_dimensions[
                column
            ].width = 20

        for cell in (
            sensitivity_sheet[1]
        ):
            _set_cell_bold(
                cell
            )

        for row in sensitivity_sheet.iter_rows(
            min_row=2,
        ):
            for cell in row:
                _excel_number_format(
                    cell
                )

        sensitivity_sheet.freeze_panes = (
            "A2"
        )

        # ----------------------------------------------------
        # ASSUMPTIONS SHEET
        # ----------------------------------------------------

        assumptions_sheet = (
            workbook.create_sheet(
                "Assumptions"
            )
        )

        assumptions = [
            [
                "GENIOSA — ASSUMPTIONS"
            ],
            [],
            [
                "Field",
                "Value",
            ],
            [
                "Project Status",
                project.get(
                    "status"
                ) or "",
            ],
            [
                "Expected Revenue",
                project.get(
                    "expected_revenue"
                ),
            ],
            [
                "Expected Profit",
                project.get(
                    "expected_profit"
                ),
            ],
            [
                "Investor Capital",
                project.get(
                    "investor_capital"
                ),
            ],
            [
                "Investor Profit",
                project.get(
                    "investor_profit"
                ),
            ],
            [
                "Investor Share (%)",
                project.get(
                    "investor_share"
                ),
            ],
            [
                "Notes",
                project.get(
                    "notes"
                ) or "",
            ],
        ]

        for row in assumptions:
            assumptions_sheet.append(
                row
            )

        assumptions_sheet.column_dimensions[
            "A"
        ].width = 32

        assumptions_sheet.column_dimensions[
            "B"
        ].width = 70

        _set_cell_bold(
            assumptions_sheet["A1"],
            size=14,
        )

        _set_cell_bold(
            assumptions_sheet["A3"]
        )

        for row_number in range(
            4,
            assumptions_sheet.max_row + 1,
        ):
            _excel_number_format(
                assumptions_sheet.cell(
                    row=row_number,
                    column=2,
                )
            )

        assumptions_sheet.freeze_panes = (
            "A4"
        )

        # ----------------------------------------------------
        # SAVE
        # ----------------------------------------------------

        workbook.save(
            str(filepath)
        )

    except Exception as exc:
        logger.error(
            "Failed to generate Excel file: %s",
            exc,
            exc_info=True,
        )

        try:
            if filepath.exists():
                filepath.unlink()
        except Exception:
            pass

        return None

    if not _validate_generated_file(
        str(filepath)
    ):
        return None

    return str(
        filepath
    )


# ------------------------------------------------------------
# POWERPOINT GENERATION
# ------------------------------------------------------------

def generate_project_presentation(
    project: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Generates a PowerPoint investor presentation.
    """

    if not project:
        return None

    if PRESENTATION_CLASS is None:
        logger.error(
            "python-pptx Presentation is not available."
        )
        return None

    project_id = project.get(
        "id"
    )

    project_name = (
        project.get("name")
        or "Geniosa Project"
    )

    safe_project_name = (
        _safe_generated_project_name(
            project_name
        )
    )

    filename = (
        "Geniosa_Investor_"
        f"{safe_project_name}_"
        f"{_generated_timestamp()}.pptx"
    )

    filepath = (
        _ensure_generated_directory()
        / filename
    )

    if analysis is None:
        analysis = (
            analyze_project_financials(
                project
            )
            or {}
        )

    try:
        presentation = (
            PRESENTATION_CLASS()
        )

        # ----------------------------------------------------
        # TITLE SLIDE
        # ----------------------------------------------------

        title_slide = (
            presentation.slides.add_slide(
                presentation.slide_layouts[0]
            )
        )

        title_slide.shapes.title.text = (
            str(project_name)
        )

        if (
            len(
                title_slide.placeholders
            ) > 1
        ):
            subtitle = (
                title_slide.placeholders[1]
            )

            subtitle.text = (
                "GENIOSA 4.0\n"
                "Investor Presentation"
            )

        # ----------------------------------------------------
        # PROJECT OVERVIEW
        # ----------------------------------------------------

        slide = (
            presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
        )

        slide.shapes.title.text = (
            "Project Overview"
        )

        overview_text = (
            f"Industry: "
            f"{project.get('industry') or 'N/A'}\n"
            f"Location: "
            f"{project.get('location') or 'N/A'}\n"
            f"Land Area: "
            f"{format_number(project.get('land_area'))} m²\n"
            f"Saleable Area: "
            f"{format_number(project.get('saleable_area'))} m²\n"
            f"Construction Area: "
            f"{format_number(project.get('construction_area'))} m²\n"
            f"Total Area: "
            f"{format_number(project.get('total_area'))} m²"
        )

        if (
            len(
                slide.placeholders
            ) > 1
        ):
            slide.placeholders[1].text = (
                overview_text
            )

        # ----------------------------------------------------
        # FINANCIAL OVERVIEW
        # ----------------------------------------------------

        slide = (
            presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
        )

        slide.shapes.title.text = (
            "Financial Overview"
        )

        financial_text = (
            f"Total Cost: "
            f"{format_money(analysis.get('total_cost'))}\n"
            f"Revenue: "
            f"{format_money(analysis.get('revenue'))}\n"
            f"Net Profit: "
            f"{format_money(analysis.get('profit'))}\n"
            f"Profit Margin: "
            f"{format_number(analysis.get('margin'))}%\n"
            f"Break-even Price: "
            f"{format_money(analysis.get('break_even_price'))}/m²"
        )

        if (
            len(
                slide.placeholders
            ) > 1
        ):
            slide.placeholders[1].text = (
                financial_text
            )

        # ----------------------------------------------------
        # INVESTMENT STRUCTURE
        # ----------------------------------------------------

        slide = (
            presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
        )

        slide.shapes.title.text = (
            "Investment Structure"
        )

        investment_text = (
            f"Investor Capital: "
            f"{format_money(analysis.get('investor_capital'))}\n"
            f"Investor Share: "
            f"{format_number(analysis.get('investor_share'))}%\n"
            f"Investor Profit: "
            f"{format_money(analysis.get('investor_profit'))}\n"
            f"Investor ROI: "
            f"{format_number(analysis.get('investor_roi'))}%"
        )

        if (
            len(
                slide.placeholders
            ) > 1
        ):
            slide.placeholders[1].text = (
                investment_text
            )

        # ----------------------------------------------------
        # RISKS
        # ----------------------------------------------------

        slide = (
            presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
        )

        slide.shapes.title.text = (
            "Key Risks & Considerations"
        )

        risk_text = (
            "• Construction cost escalation\n"
            "• Sales price and absorption risk\n"
            "• Financing cost and interest-rate risk\n"
            "• Construction schedule risk\n"
            "• Market and demand risk\n"
            "• Regulatory and permitting risk\n"
            "• Liquidity and exit risk"
        )

        if (
            len(
                slide.placeholders
            ) > 1
        ):
            slide.placeholders[1].text = (
                risk_text
            )

        # ----------------------------------------------------
        # PROJECT NOTES
        # ----------------------------------------------------

        slide = (
            presentation.slides.add_slide(
                presentation.slide_layouts[1]
            )
        )

        slide.shapes.title.text = (
            "Project Notes"
        )

        notes = str(
            project.get("notes")
            or "No additional notes."
        )

        if (
            len(
                slide.placeholders
            ) > 1
        ):
            slide.placeholders[1].text = (
                notes
            )

        # ----------------------------------------------------
        # SAVE
        # ----------------------------------------------------

        presentation.save(
            str(filepath)
        )

    except Exception as exc:
        logger.error(
            "Failed to generate PowerPoint file: %s",
            exc,
            exc_info=True,
        )

        try:
            if filepath.exists():
                filepath.unlink()
        except Exception:
            pass

        return None

    if not _validate_generated_file(
        str(filepath)
    ):
        return None

    return str(
        filepath
    )


# ------------------------------------------------------------
# GENERATE ALL PROJECT FILES
# ------------------------------------------------------------

def generate_project_files(
    chat_id: Any,
    project_id: int,
) -> Dict[str, Any]:
    """
    Generates both Excel and PowerPoint
    files for a project.

    IMPORTANT:
    The return value always contains:
        success
        project_id
        files
        analysis

    PART 12 should iterate:
        result.get("files", [])
    """

    if chat_id is None:
        return {
            "success": False,
            "project_id": project_id,
            "files": [],
            "analysis": {},
            "error": "Chat ID is required.",
        }

    try:
        project_id_value = int(
            project_id
        )
    except Exception:
        return {
            "success": False,
            "project_id": project_id,
            "files": [],
            "analysis": {},
            "error": "Invalid project ID.",
        }

    # get_project() already scopes the lookup
    # to the current chat.
    project = get_project(
        chat_id,
        project_id_value,
    )

    if not project:
        return {
            "success": False,
            "project_id": project_id_value,
            "files": [],
            "analysis": {},
            "error": "Project not found.",
        }

    try:
        analysis = (
            analyze_project_financials(
                project
            )
            or {}
        )

    except Exception as exc:
        logger.error(
            "Financial analysis failed before file generation: %s",
            exc,
            exc_info=True,
        )

        return {
            "success": False,
            "project_id": project_id_value,
            "files": [],
            "analysis": {},
            "error": (
                "Financial analysis failed."
            ),
        }

    generated = []

    # --------------------------------------------------------
    # EXCEL
    # --------------------------------------------------------

    try:
        excel_path = (
            generate_project_excel(
                project,
                analysis,
            )
        )

        if excel_path:
            asset_id = (
                generated_asset_save(
                    chat_id=chat_id,
                    project_id=project_id_value,
                    filename=Path(
                        excel_path
                    ).name,
                    asset_type="xlsx",
                    description=(
                        "Project financial model"
                    ),
                )
            )

            generated.append({
                "type": "xlsx",
                "path": str(
                    excel_path
                ),
                "filename": Path(
                    excel_path
                ).name,
                "asset_id": asset_id,
            })

    except Exception as exc:
        logger.error(
            "Excel generation failed: %s",
            exc,
            exc_info=True,
        )

    # --------------------------------------------------------
    # POWERPOINT
    # --------------------------------------------------------

    try:
        presentation_path = (
            generate_project_presentation(
                project,
                analysis,
            )
        )

        if presentation_path:
            asset_id = (
                generated_asset_save(
                    chat_id=chat_id,
                    project_id=project_id_value,
                    filename=Path(
                        presentation_path
                    ).name,
                    asset_type="pptx",
                    description=(
                        "Investor presentation"
                    ),
                )
            )

            generated.append({
                "type": "pptx",
                "path": str(
                    presentation_path
                ),
                "filename": Path(
                    presentation_path
                ).name,
                "asset_id": asset_id,
            })

    except Exception as exc:
        logger.error(
            "PowerPoint generation failed: %s",
            exc,
            exc_info=True,
        )

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    result = {
        "success": bool(
            generated
        ),
        "project_id": project_id_value,
        "files": generated,
        "analysis": analysis,
    }

    if not generated:
        result["error"] = (
            "No project files could be generated."
        )

    elif len(generated) == 1:
        result["warning"] = (
            "Only one project file was generated successfully."
        )

    return result


print(
    "GENIOSA 4.0 — PART 10/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 11/12
# Commands, natural language processing and CRM actions
# ============================================================


# ------------------------------------------------------------
# PART 11 — SAFE LIMITS / CONSTANTS
# ------------------------------------------------------------

MAX_COMMAND_TEXT_LENGTH = int(
    globals().get(
        "MAX_COMMAND_TEXT_LENGTH",
        20000,
    )
)

MAX_COMMAND_TEXT_LENGTH = max(
    1000,
    min(
        MAX_COMMAND_TEXT_LENGTH,
        100000,
    ),
)


# ------------------------------------------------------------
# TEXT PARSING
# ------------------------------------------------------------

def parse_key_value_text(
    text: str,
) -> Dict[str, str]:
    """
    Parses key=value or key:value pairs.

    Supports English, Russian and Georgian.

    Examples:
        name: NIKKEA 12
        სახელი: NIKKEA 12
        location: Kutaisi
        ლოკაცია: Kutaisi
    """

    source = str(
        text or ""
    ).strip()

    if not source:
        return {}

    source = source[
        :MAX_COMMAND_TEXT_LENGTH
    ]

    result: Dict[str, str] = {}

    pattern = re.compile(
        r"""
        ([^\d=:\n,;][^=:\n,;]*?)
        \s*(?:=|:)
        \s*
        (?:"([^"]*)"|'([^']*)'|([^,\n;]+))
        """,
        re.VERBOSE,
    )

    for match in pattern.finditer(
        source
    ):
        key = (
            match.group(1)
            or ""
        ).strip().lower()

        value = (
            match.group(2)
            if match.group(2) is not None
            else (
                match.group(3)
                if match.group(3) is not None
                else (
                    match.group(4)
                    or ""
                )
            )
        )

        value = str(
            value or ""
        ).strip()

        if not key or not value:
            continue

        key = re.sub(
            r"\s+",
            " ",
            key,
        )

        result[key] = value

    return result


def first_number(
    text: str,
) -> Optional[float]:
    """
    Returns the first numeric value found in text.
    """

    source = str(
        text or ""
    )

    match = re.search(
        r"(?<!\d)"
        r"-?\d+(?:[.,]\d+)?"
        r"(?!\d)",
        source,
    )

    if not match:
        return None

    value = (
        match.group(0)
        .replace(",", ".")
    )

    try:
        result = float(
            value
        )

        if not (
            result == result
            and abs(result) != float("inf")
        ):
            return None

        return result

    except Exception:
        return None


def normalize_text_value(
    value: Any,
) -> Optional[str]:
    """
    Normalizes optional textual values.
    """

    if value is None:
        return None

    text_value = str(
        value
    ).strip()

    if not text_value:
        return None

    return text_value


def normalize_numeric_text(
    value: Any,
) -> Optional[float]:
    """
    Converts numeric text containing common
    currency and formatting symbols into float.

    Examples:
        1,500
        1.500
        1,500.50
        1.500,50
        $1,500
        1 500 ₾
    """

    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    text_value = str(
        value
    ).strip()

    if not text_value:
        return None

    for symbol in (
        "$",
        "€",
        "₾",
    ):
        text_value = text_value.replace(
            symbol,
            "",
        )

    text_value = re.sub(
        r"\b(?:USD|EUR|GEL|ლარი|lari)\b",
        "",
        text_value,
        flags=re.IGNORECASE,
    )

    text_value = text_value.strip()

    text_value = text_value.replace(
        " ",
        "",
    )

    if (
        "," in text_value
        and "." in text_value
    ):
        if (
            text_value.rfind(",")
            >
            text_value.rfind(".")
        ):
            text_value = (
                text_value
                .replace(".", "")
                .replace(",", ".")
            )
        else:
            text_value = (
                text_value
                .replace(",", "")
            )

    elif "," in text_value:
        comma_match = re.search(
            r",(\d{3})$",
            text_value,
        )

        if comma_match:
            text_value = (
                text_value
                .replace(",", "")
            )
        else:
            text_value = (
                text_value
                .replace(",", ".")
            )

    elif "." in text_value:
        dot_match = re.search(
            r"\.(\d{3})$",
            text_value,
        )

        if dot_match:
            text_value = (
                text_value
                .replace(".", "")
            )

    if not re.fullmatch(
        r"-?\d+(?:\.\d+)?",
        text_value,
    ):
        return None

    try:
        number = float(
            text_value
        )

        if not (
            number == number
            and abs(number) != float("inf")
        ):
            return None

        return number

    except Exception:
        return None


# ------------------------------------------------------------
# PROJECT CREATION FROM TEXT
# ------------------------------------------------------------

def create_project_from_text(
    chat_id: Any,
    text: str,
) -> Optional[int]:
    """
    Creates a project from explicit natural-language
    key/value data.
    """

    if chat_id is None:
        return None

    source = str(
        text or ""
    ).strip()

    if not source:
        return None

    source = source[
        :MAX_COMMAND_TEXT_LENGTH
    ]

    pairs = parse_key_value_text(
        source
    )

    name = (
        pairs.get("name")
        or pairs.get("project")
        or pairs.get("project name")
        or pairs.get("პროექტი")
        or pairs.get("სახელი")
        or pairs.get("პროექტის სახელი")
    )

    if not name:
        lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip()
        ]

        for first_line in lines[:3]:
            cleaned = re.sub(
                r"^(?:"
                r"პროექტი"
                r"|პროექტის\s+სახელი"
                r"|project"
                r"|project\s+name"
                r")"
                r"\s*[:\-]?\s*",
                "",
                first_line,
                flags=re.IGNORECASE,
            ).strip()

            if (
                cleaned
                and "=" not in cleaned
                and ":" not in cleaned
            ):
                name = cleaned
                break

    name = normalize_text_value(
        name
    )

    if not name:
        return None

    industry = (
        pairs.get("industry")
        or pairs.get("sector")
        or pairs.get("type")
        or pairs.get("ინდუსტრია")
        or pairs.get("სექტორი")
        or pairs.get("ტიპი")
    )

    location = (
        pairs.get("location")
        or pairs.get("address")
        or pairs.get("ლოკაცია")
        or pairs.get("ადგილი")
        or pairs.get("მისამართი")
    )

    description = (
        pairs.get("description")
        or pairs.get("აღწერა")
    )

    land_area = normalize_numeric_text(
        pairs.get("land_area")
        or pairs.get("land area")
        or pairs.get("land")
        or pairs.get("მიწის ფართობი")
        or pairs.get("მიწა")
    )

    saleable_area = normalize_numeric_text(
        pairs.get("saleable_area")
        or pairs.get("saleable area")
        or pairs.get("saleable")
        or pairs.get("გასაყიდი ფართობი")
        or pairs.get("გასაყიდი")
    )

    construction_area = normalize_numeric_text(
        pairs.get("construction_area")
        or pairs.get("construction area")
        or pairs.get("construction")
        or pairs.get("სამშენებლო ფართობი")
        or pairs.get("მშენებლობა")
    )

    total_area = normalize_numeric_text(
        pairs.get("total_area")
        or pairs.get("total area")
        or pairs.get("total")
        or pairs.get("საერთო ფართობი")
        or pairs.get("სრული ფართობი")
    )

    land_cost = normalize_numeric_text(
        pairs.get("land_cost")
        or pairs.get("land price")
        or pairs.get("land value")
        or pairs.get("მიწის ღირებულება")
        or pairs.get("მიწის ფასი")
    )

    construction_cost = normalize_numeric_text(
        pairs.get("construction_cost")
        or pairs.get("construction price")
        or pairs.get("construction value")
        or pairs.get("მშენებლობის ღირებულება")
        or pairs.get("მშენებლობის ფასი")
    )

    operating_cost = normalize_numeric_text(
        pairs.get("operating_cost")
        or pairs.get("operating cost")
        or pairs.get("ოპერაციული ხარჯი")
    )

    financing_cost = normalize_numeric_text(
        pairs.get("financing_cost")
        or pairs.get("financing cost")
        or pairs.get("ფინანსირების ხარჯი")
    )

    other_cost = normalize_numeric_text(
        pairs.get("other_cost")
        or pairs.get("other cost")
        or pairs.get("სხვა ხარჯი")
    )

    expected_revenue = normalize_numeric_text(
        pairs.get("expected_revenue")
        or pairs.get("expected revenue")
        or pairs.get("revenue")
        or pairs.get("შემოსავალი")
        or pairs.get("მოსალოდნელი შემოსავალი")
    )

    expected_profit = normalize_numeric_text(
        pairs.get("expected_profit")
        or pairs.get("expected profit")
        or pairs.get("profit")
        or pairs.get("მოგება")
        or pairs.get("მოსალოდნელი მოგება")
    )

    investor_capital = normalize_numeric_text(
        pairs.get("investor_capital")
        or pairs.get("investor capital")
        or pairs.get("investment")
        or pairs.get("ინვესტიცია")
        or pairs.get("ინვესტორის კაპიტალი")
    )

    investor_profit = normalize_numeric_text(
        pairs.get("investor_profit")
        or pairs.get("investor profit")
        or pairs.get("ინვესტორის მოგება")
    )

    investor_share = normalize_numeric_text(
        pairs.get("investor_share")
        or pairs.get("investor share")
        or pairs.get("share")
        or pairs.get("წილი")
        or pairs.get("ინვესტორის წილი")
    )

    notes = (
        pairs.get("notes")
        or pairs.get("note")
        or pairs.get("შენიშვნა")
        or pairs.get("შენიშვნები")
    )

    status = (
        pairs.get("status")
        or pairs.get("სტატუსი")
        or "active"
    )

    return create_project(
        chat_id=chat_id,
        name=name,
        industry=industry,
        location=location,
        description=description,
        land_area=land_area,
        saleable_area=saleable_area,
        construction_area=construction_area,
        total_area=total_area,
        land_cost=land_cost,
        construction_cost=construction_cost,
        operating_cost=operating_cost,
        financing_cost=financing_cost,
        other_cost=other_cost,
        expected_revenue=expected_revenue,
        expected_profit=expected_profit,
        investor_capital=investor_capital,
        investor_profit=investor_profit,
        investor_share=investor_share,
        notes=notes,
        status=status,
    )


# ------------------------------------------------------------
# INVESTOR CREATION FROM TEXT
# ------------------------------------------------------------

def create_investor_from_text(
    chat_id: Any,
    text: str,
) -> Optional[int]:
    """
    Creates an investor from explicit
    natural-language key/value data.
    """

    if chat_id is None:
        return None

    source = str(
        text or ""
    ).strip()

    if not source:
        return None

    source = source[
        :MAX_COMMAND_TEXT_LENGTH
    ]

    pairs = parse_key_value_text(
        source
    )

    name = (
        pairs.get("name")
        or pairs.get("investor")
        or pairs.get("investor name")
        or pairs.get("ინვესტორი")
        or pairs.get("სახელი")
        or pairs.get("ინვესტორის სახელი")
    )

    name = normalize_text_value(
        name
    )

    if not name:
        return None

    company = (
        pairs.get("company")
        or pairs.get("კომპანია")
    )

    country = (
        pairs.get("country")
        or pairs.get("ქვეყანა")
    )

    contact = (
        pairs.get("contact")
        or pairs.get("phone")
        or pairs.get("email")
        or pairs.get("კონტაქტი")
    )

    investment_capacity = normalize_numeric_text(
        pairs.get("investment_capacity")
        or pairs.get("investment capacity")
        or pairs.get("capacity")
        or pairs.get("საინვესტიციო შესაძლებლობა")
    )

    preferred_sector = (
        pairs.get("preferred_sector")
        or pairs.get("preferred sector")
        or pairs.get("sector")
        or pairs.get("სექტორი")
    )

    status = (
        pairs.get("status")
        or pairs.get("სტატუსი")
        or "new"
    )

    notes = (
        pairs.get("notes")
        or pairs.get("note")
        or pairs.get("შენიშვნა")
        or pairs.get("შენიშვნები")
    )

    return create_investor(
        chat_id=chat_id,
        name=name,
        company=company,
        country=country,
        contact=contact,
        investment_capacity=investment_capacity,
        preferred_sector=preferred_sector,
        status=status,
        notes=notes,
    )


# ------------------------------------------------------------
# NATURAL-LANGUAGE INTENT DETECTION
# ------------------------------------------------------------

def looks_like_project_request(
    text: str,
) -> bool:
    """
    Detects explicit project-creation requests.
    """

    source = str(
        text or ""
    ).strip().lower()

    if not source:
        return False

    keywords = [
        "პროექტის დამატება",
        "პროექტი დაამატე",
        "პროექტის შექმნა",
        "პროექტი შექმენი",
        "შექმენი პროექტი",
        "ახალი პროექტი",
        "დაამატე პროექტი",
        "create project",
        "new project",
        "add project",
        "project:",
        "project=",
    ]

    return any(
        keyword in source
        for keyword in keywords
    )


def looks_like_investor_request(
    text: str,
) -> bool:
    """
    Detects explicit investor-creation requests.
    """

    source = str(
        text or ""
    ).strip().lower()

    if not source:
        return False

    keywords = [
        "ინვესტორის დამატება",
        "ინვესტორი დაამატე",
        "ინვესტორის შექმნა",
        "ინვესტორი შექმენი",
        "შექმენი ინვესტორი",
        "ახალი ინვესტორი",
        "დაამატე ინვესტორი",
        "create investor",
        "new investor",
        "add investor",
        "investor:",
        "investor=",
    ]

    return any(
        keyword in source
        for keyword in keywords
    )


# ------------------------------------------------------------
# STATUS / CRM COMMANDS
# ------------------------------------------------------------

def status_text(
    chat_id: Any,
) -> str:
    """
    Returns a detailed Geniosa status.
    """

    try:
        env = validate_environment()
    except Exception:
        env = {
            "telegram": False,
            "gemini": False,
        }

    try:
        database_ok = database_is_available()
    except Exception:
        database_ok = False

    telegram_ok = bool(
        env.get(
            "telegram",
            False,
        )
    )

    gemini_ok = bool(
        env.get(
            "gemini",
            False,
        )
    )

    return (
        "🟢 GENIOSA 4.0 STATUS\n\n"
        f"Telegram: "
        f"{'OK' if telegram_ok else 'ERROR'}\n"
        f"Gemini: "
        f"{'OK' if gemini_ok else 'ERROR'}\n"
        f"Database: "
        f"{'OK' if database_ok else 'ERROR'}\n"
        f"Chat ID: {chat_id}"
    )


def memory_command(
    chat_id: Any,
) -> str:
    return memories_summary(
        chat_id
    )


def projects_command(
    chat_id: Any,
) -> str:
    return projects_summary(
        chat_id
    )


def investors_command(
    chat_id: Any,
) -> str:
    return investors_summary(
        chat_id
    )


def deals_command(
    chat_id: Any,
) -> str:
    return deals_summary(
        chat_id
    )


def documents_command(
    chat_id: Any,
) -> str:
    return documents_summary(
        chat_id
    )


def generated_files_command(
    chat_id: Any,
) -> str:
    return generated_assets_summary(
        chat_id
    )


# ------------------------------------------------------------
# PROJECT COMMAND
# ------------------------------------------------------------

def project_command(
    chat_id: Any,
    argument: str,
) -> str:
    """
    Shows a project by numeric ID or name.
    """

    value = str(
        argument or ""
    ).strip()

    if not value:
        return (
            "გამოიყენე:\n"
            "/project 1\n"
            "ან\n"
            "/project პროექტის სახელი"
        )

    project = None

    if value.isdigit():
        project = get_project(
            chat_id,
            int(value),
        )

    if project is None:
        project = find_project_by_name(
            chat_id,
            value,
        )

    if not project:
        return (
            "❌ პროექტი ვერ მოიძებნა."
        )

    return project_summary(
        project
    )


# ------------------------------------------------------------
# INVESTOR COMMAND
# ------------------------------------------------------------

def investor_command(
    chat_id: Any,
    argument: str,
) -> str:
    """
    Shows an investor by numeric ID or search text.
    """

    value = str(
        argument or ""
    ).strip()

    if not value:
        return (
            "გამოიყენე:\n"
            "/investor 1\n"
            "ან\n"
            "/investor კომპანიის სახელი"
        )

    investor = None

    if value.isdigit():
        investor = get_investor(
            chat_id,
            int(value),
        )

    if investor is None:
        investor = find_investor(
            chat_id,
            value,
        )

    if not investor:
        return (
            "❌ ინვესტორი ვერ მოიძებნა."
        )

    return format_investor(
        investor
    )


# ------------------------------------------------------------
# DEAL COMMAND
# ------------------------------------------------------------

def deal_command(
    chat_id: Any,
    argument: str,
) -> str:
    """
    Shows a deal by numeric ID.
    """

    value = str(
        argument or ""
    ).strip()

    if not value:
        return (
            "გამოიყენე:\n"
            "/deal 1"
        )

    if not value.isdigit():
        return (
            "❌ გარიგების ID უნდა იყოს რიცხვი."
        )

    deal = get_deal(
        chat_id,
        int(value),
    )

    if not deal:
        return (
            "❌ გარიგება ვერ მოიძებნა."
        )

    return format_deal(
        deal
    )


# ------------------------------------------------------------
# FINANCE COMMAND
# ------------------------------------------------------------

def finance_command(
    chat_id: Any,
    argument: str,
) -> str:
    """
    Runs financial analysis for a project.
    """

    value = str(
        argument or ""
    ).strip()

    if not value:
        return (
            "გამოიყენე:\n"
            "/finance 1"
        )

    project = None

    if value.isdigit():
        project_id = int(
            value
        )

        project = get_project(
            chat_id,
            project_id,
        )

    else:
        project = find_project_by_name(
            chat_id,
            value,
        )

        if not project:
            return (
                "❌ პროექტი ვერ მოიძებნა."
            )

        project_id = project.get(
            "id"
        )

    if not project:
        return (
            "❌ პროექტი ვერ მოიძებნა."
        )

    try:
        analysis = run_project_financial_analysis(
            chat_id=chat_id,
            project_id=project_id,
        )

    except Exception as exc:
        logger.error(
            "Finance command failed: %s",
            exc,
            exc_info=True,
        )

        return (
            "❌ ფინანსური ანალიზის შესრულება ვერ მოხერხდა."
        )

    if not analysis:
        return (
            "❌ ფინანსური ანალიზის მონაცემები ვერ მოიძებნა."
        )

    return financial_analysis_text(
        analysis
    )


# ------------------------------------------------------------
# GENERATED ASSET COMMAND
# ------------------------------------------------------------

def generated_asset_command(
    chat_id: Any,
    argument: str,
) -> str:
    """
    Shows metadata for one generated asset.
    """

    value = str(
        argument or ""
    ).strip()

    if not value or not value.isdigit():
        return (
            "გამოიყენე:\n"
            "/generated 1"
        )

    asset_id = int(
        value
    )

    assets = get_generated_assets(
        chat_id,
        limit=MAX_GENERATED_ASSETS,
    )

    for asset in assets:
        try:
            current_id = int(
                asset.get("id")
            )
        except Exception:
            continue

        if current_id != asset_id:
            continue

        filename = (
            asset.get("filename")
            or "unknown"
        )

        asset_type = (
            asset.get("asset_type")
            or "file"
        )

        description = (
            asset.get("description")
            or ""
        )

        response = (
            f"📦 ფაილი #{asset_id}\n"
            f"📎 {filename}\n"
            f"📁 ტიპი: {asset_type}"
        )

        if description:
            response += (
                f"\n📝 {description}"
            )

        return response

    return (
        "❌ გენერირებული ფაილი ვერ მოიძებნა."
    )


# ------------------------------------------------------------
# TELEGRAM COMMAND ROUTER
# ------------------------------------------------------------

def handle_command(
    chat_id: Any,
    command_text: str,
) -> str:
    """
    Handles Telegram slash commands.
    """

    source = str(
        command_text or ""
    ).strip()

    if not source:
        return ""

    if len(source) > MAX_COMMAND_TEXT_LENGTH:
        source = source[
            :MAX_COMMAND_TEXT_LENGTH
        ]

    parts = source.split(
        maxsplit=1
    )

    command = (
        parts[0]
        .strip()
        .lower()
        .split("@", 1)[0]
    )

    argument = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    if command in {
        "/start",
        "/help",
    }:
        return (
            "🤖 GENIOSA 4.0\n\n"
            "მე შემიძლია დაგეხმარო:\n"
            "• ბიზნესის ანალიზში\n"
            "• ინვესტიციებში\n"
            "• უძრავ ქონებაში\n"
            "• პროექტების მართვაში\n"
            "• ინვესტორების CRM-ში\n"
            "• ფინანსურ ანალიზში\n"
            "• დოკუმენტების ანალიზში\n"
            "• Excel/PPTX ფაილების შექმნაში\n\n"
            "ძირითადი ბრძანებები:\n"
            "/status\n"
            "/memory\n"
            "/projects\n"
            "/investors\n"
            "/deals\n"
            "/documents\n"
            "/finance ID\n"
            "/project ID\n"
            "/investor ID\n"
            "/deal ID\n"
            "/generated\n\n"
            "ან უბრალოდ მომწერე ბუნებრივ ენაზე."
        )

    if command == "/status":
        return status_text(
            chat_id
        )

    if command == "/memory":
        return memory_command(
            chat_id
        )

    if command == "/projects":
        return projects_command(
            chat_id
        )

    if command == "/investors":
        return investors_command(
            chat_id
        )

    if command == "/deals":
        return deals_command(
            chat_id
        )

    if command == "/documents":
        return documents_command(
            chat_id
        )

    if command in {
        "/generated",
        "/files",
    }:
        if argument:
            return generated_asset_command(
                chat_id,
                argument,
            )

        return generated_files_command(
            chat_id
        )

    if command == "/finance":
        return finance_command(
            chat_id,
            argument,
        )

    if command == "/project":
        return project_command(
            chat_id,
            argument,
        )

    if command == "/investor":
        return investor_command(
            chat_id,
            argument,
        )

    if command == "/deal":
        return deal_command(
            chat_id,
            argument,
        )

    return (
        "❌ უცნობი ბრძანებაა.\n\n"
        "გამოიყენე /help."
    )


# ------------------------------------------------------------
# MAIN TEXT MESSAGE HANDLER
# ------------------------------------------------------------

def handle_text_message(
    chat_id: Any,
    text: str,
) -> str:
    """
    Main natural-language message handler.

    Explicit CRM creation requests are handled first.
    All other messages go to Geniosa AI.
    """

    if chat_id is None:
        return (
            "❌ Chat ID ვერ განისაზღვრა."
        )

    message_text = str(
        text or ""
    ).strip()

    if not message_text:
        return (
            "❌ ცარიელი შეტყობინება."
        )

    message_text = message_text[
        :MAX_COMMAND_TEXT_LENGTH
    ]

    # --------------------------------------------------------
    # SLASH COMMANDS
    # --------------------------------------------------------

    if message_text.startswith("/"):
        response = handle_command(
            chat_id,
            message_text,
        )

        try:
            save_message(
                chat_id=chat_id,
                role="user",
                text=message_text,
            )

            save_message(
                chat_id=chat_id,
                role="assistant",
                text=response,
            )

        except Exception as exc:
            logger.warning(
                "Could not save command conversation: %s",
                exc,
            )

        return response

    # --------------------------------------------------------
    # SAVE USER MESSAGE
    # --------------------------------------------------------

    try:
        save_message(
            chat_id=chat_id,
            role="user",
            text=message_text,
        )

    except Exception as exc:
        logger.warning(
            "Could not save user message: %s",
            exc,
        )

    # --------------------------------------------------------
    # PROJECT CREATION
    # --------------------------------------------------------

    if looks_like_project_request(
        message_text
    ):
        project_id = (
            create_project_from_text(
                chat_id,
                message_text,
            )
        )

        if project_id:
            project = get_project(
                chat_id,
                project_id,
            )

            if project:
                response = (
                    "✅ პროექტი დაემატა "
                    "Geniosa-ს CRM-ში.\n\n"
                    + project_summary(
                        project
                    )
                )
            else:
                response = (
                    "✅ პროექტი შეიქმნა "
                    "Geniosa-ს CRM-ში."
                )

            try:
                save_message(
                    chat_id=chat_id,
                    role="assistant",
                    text=response,
                )
            except Exception as exc:
                logger.warning(
                    "Could not save project response: %s",
                    exc,
                )

            return response

        # Do not silently send a failed creation
        # request to AI as if nothing happened.
        response = (
            "⚠️ პროექტის შექმნა ვერ შესრულდა.\n\n"
            "მომწერე მაგალითად:\n"
            "ახალი პროექტი\n"
            "სახელი: NIKKEA 12\n"
            "ინდუსტრია: hotel\n"
            "ლოკაცია: Kutaisi\n"
            "გასაყიდი ფართობი: 10854"
        )

        try:
            save_message(
                chat_id=chat_id,
                role="assistant",
                text=response,
            )
        except Exception as exc:
            logger.warning(
                "Could not save project error response: %s",
                exc,
            )

        return response

    # --------------------------------------------------------
    # INVESTOR CREATION
    # --------------------------------------------------------

    if looks_like_investor_request(
        message_text
    ):
        investor_id = (
            create_investor_from_text(
                chat_id,
                message_text,
            )
        )

        if investor_id:
            investor = get_investor(
                chat_id,
                investor_id,
            )

            if investor:
                response = (
                    "✅ ინვესტორი დაემატა "
                    "Geniosa-ს CRM-ში.\n\n"
                    + format_investor(
                        investor
                    )
                )
            else:
                response = (
                    "✅ ინვესტორი შეიქმნა "
                    "Geniosa-ს CRM-ში."
                )

            try:
                save_message(
                    chat_id=chat_id,
                    role="assistant",
                    text=response,
                )
            except Exception as exc:
                logger.warning(
                    "Could not save investor response: %s",
                    exc,
                )

            return response

        response = (
            "⚠️ ინვესტორის შექმნა ვერ შესრულდა.\n\n"
            "მომწერე მაგალითად:\n"
            "ახალი ინვესტორი\n"
            "სახელი: John Smith\n"
            "კომპანია: ABC Capital\n"
            "ქვეყანა: UAE\n"
            "საინვესტიციო შესაძლებლობა: 5000000"
        )

        try:
            save_message(
                chat_id=chat_id,
                role="assistant",
                text=response,
            )
        except Exception as exc:
            logger.warning(
                "Could not save investor error response: %s",
                exc,
            )

        return response

    # --------------------------------------------------------
    # GENERAL AI REQUEST
    # --------------------------------------------------------

    try:
        response = ask_geniosa(
            chat_id=chat_id,
            user_message=message_text,
        )

    except Exception as exc:
        logger.error(
            "AI message handling failed: %s",
            exc,
            exc_info=True,
        )

        response = (
            "❌ Geniosa-ს AI მოდულმა დროებით ვერ შეძლო "
            "პასუხის დამუშავება."
        )

    if not response:
        response = (
            "❌ პასუხის გენერირება ვერ მოხერხდა."
        )

    try:
        save_message(
            chat_id=chat_id,
            role="assistant",
            text=response,
        )

    except Exception as exc:
        logger.warning(
            "Could not save assistant message: %s",
            exc,
        )

    return response


print(
    "GENIOSA 4.0 — PART 11/12 LOADED"
)
# ============================================================
# GENIOSA 4.0 — PART 12/12
# Telegram polling, file sending, startup and shutdown
# ============================================================

from pathlib import Path


# ------------------------------------------------------------
# Telegram compatibility helpers
# ------------------------------------------------------------

def telegram_url(
    method: str,
) -> str:
    """
    Compatibility wrapper for Telegram API URL generation.
    """

    return telegram_api_url(
        method
    )


def send_message(
    chat_id: Any,
    text: str,
    **kwargs: Any,
) -> bool:
    """
    Compatibility wrapper around send_telegram_message().
    """

    try:
        return bool(
            send_telegram_message(
                chat_id=chat_id,
                text=text,
                **kwargs,
            )
        )
    except TypeError:
        try:
            return bool(
                send_telegram_message(
                    chat_id,
                    text,
                )
            )
        except Exception as exc:
            logger.warning(
                "Could not send Telegram message: %s",
                exc,
            )
            return False
    except Exception as exc:
        logger.warning(
            "Could not send Telegram message: %s",
            exc,
        )
        return False


def send_long_message(
    chat_id: Any,
    text: str,
    chunk_size: int = 4000,
) -> bool:
    """
    Sends long Telegram messages in safe chunks.
    """

    if text is None:
        return False

    message = str(
        text
    ).strip()

    if not message:
        return False

    chunk_size = max(
        500,
        min(
            int(chunk_size),
            4000,
        ),
    )

    chunks: List[str] = []

    while message:
        if len(message) <= chunk_size:
            chunks.append(message)
            break

        split_at = message.rfind(
            "\n",
            0,
            chunk_size,
        )

        if split_at < 200:
            split_at = message.rfind(
                " ",
                0,
                chunk_size,
            )

        if split_at < 200:
            split_at = chunk_size

        chunk = message[
            :split_at
        ].strip()

        if chunk:
            chunks.append(
                chunk
            )

        message = message[
            split_at:
        ].strip()

    success = True

    for chunk in chunks:
        if not send_message(
            chat_id,
            chunk,
        ):
            success = False
            break

    return success


# ------------------------------------------------------------
# Telegram timeout compatibility
# ------------------------------------------------------------

TELEGRAM_FILE_TIMEOUT = int(
    globals().get(
        "TELEGRAM_FILE_TIMEOUT",
        globals().get(
            "TELEGRAM_REQUEST_TIMEOUT",
            60,
        ),
    )
)

TELEGRAM_FILE_TIMEOUT = max(
    10,
    min(
        TELEGRAM_FILE_TIMEOUT,
        600,
    ),
)


# ------------------------------------------------------------
# Telegram polling offset
# ------------------------------------------------------------

TELEGRAM_OFFSET = int(
    globals().get(
        "TELEGRAM_OFFSET",
        globals().get(
            "LAST_UPDATE_ID",
            0,
        ),
    )
)


# ------------------------------------------------------------
# Telegram document sending
# ------------------------------------------------------------

def send_document_to_chat(
    chat_id: Any,
    filepath: str,
    caption: str = "",
) -> bool:
    """
    Sends a local file to a Telegram chat.
    """

    if chat_id is None:
        return False

    if not filepath:
        return False

    path = Path(
        filepath
    )

    try:
        path = path.resolve()
    except Exception:
        pass

    if not path.exists():
        logger.error(
            "File does not exist: %s",
            filepath,
        )
        return False

    if not path.is_file():
        logger.error(
            "Path is not a file: %s",
            filepath,
        )
        return False

    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not configured."
        )
        return False

    try:
        file_size = path.stat().st_size

        max_size = int(
            globals().get(
                "MAX_DOCUMENT_FILE_SIZE_BYTES",
                globals().get(
                    "MAX_DOCUMENT_SIZE_BYTES",
                    25 * 1024 * 1024,
                ),
            )
        )

        if file_size > max_size:
            logger.error(
                "File is too large for Telegram: %s",
                path,
            )
            return False

    except Exception as exc:
        logger.warning(
            "Could not validate file size: %s",
            exc,
        )

    try:

        with path.open(
            "rb"
        ) as file_handle:

            response = requests.post(
                telegram_url(
                    "sendDocument"
                ),
                data={
                    "chat_id": str(
                        chat_id
                    ),
                    "caption": str(
                        caption or ""
                    )[:1000],
                },
                files={
                    "document": (
                        path.name,
                        file_handle,
                    )
                },
                timeout=TELEGRAM_FILE_TIMEOUT,
            )

        if not response.ok:
            logger.error(
                "Telegram sendDocument failed: %s",
                response.text[:1000],
            )
            return False

        try:
            data = response.json()
        except ValueError:
            logger.error(
                "Telegram sendDocument returned invalid JSON."
            )
            return False

        if not isinstance(
            data,
            dict,
        ):
            return False

        if not data.get(
            "ok"
        ):
            logger.error(
                "Telegram sendDocument returned error: %s",
                data,
            )
            return False

        return True

    except Exception as exc:
        logger.exception(
            "Could not send document to Telegram: %s",
            exc,
        )
        return False


# ------------------------------------------------------------
# Generated files
# ------------------------------------------------------------

def send_generated_files_to_chat(
    chat_id: Any,
    project_id: int,
) -> List[str]:
    """
    Generates and sends the project's Excel and PPTX files.

    generate_project_files() in PART 10 returns a dictionary
    containing a 'files' list.
    """

    sent_files: List[str] = []

    try:

        result = generate_project_files(
            chat_id=chat_id,
            project_id=project_id,
        )

    except Exception as exc:

        logger.exception(
            "Could not generate project files: %s",
            exc,
        )

        return sent_files

    if not result:
        logger.warning(
            "No project files were generated for project %s.",
            project_id,
        )
        return sent_files

    if not isinstance(
        result,
        dict,
    ):
        logger.error(
            "Unexpected generate_project_files() result type: %s",
            type(result).__name__,
        )
        return sent_files

    if not result.get(
        "success",
        False,
    ):
        logger.warning(
            "Project file generation was not successful: %s",
            result,
        )

    files = result.get(
        "files",
        [],
    )

    if not isinstance(
        files,
        list,
    ):
        logger.warning(
            "Generated files field is not a list."
        )
        return sent_files

    for file_info in files:

        filepath = None

        if isinstance(
            file_info,
            str,
        ):
            filepath = file_info

        elif isinstance(
            file_info,
            dict,
        ):
            filepath = (
                file_info.get("path")
                or file_info.get("filepath")
                or file_info.get("file")
            )

        if not filepath:
            continue

        path = Path(
            str(filepath)
        )

        if not path.exists():
            logger.warning(
                "Generated file does not exist: %s",
                filepath,
            )
            continue

        if not path.is_file():
            continue

        suffix = path.suffix.lower()

        if suffix == ".xlsx":

            caption = (
                "📊 Geniosa — Financial Model / Excel"
            )

        elif suffix == ".pptx":

            caption = (
                "📑 Geniosa — Investor Presentation / PPTX"
            )

        else:

            caption = (
                "📎 Geniosa — Generated File"
            )

        if send_document_to_chat(
            chat_id,
            str(path),
            caption,
        ):
            sent_files.append(
                str(path)
            )

    return sent_files


# ------------------------------------------------------------
# Help message
# ------------------------------------------------------------

def send_help_message(
    chat_id: Any,
) -> None:

    try:

        response = handle_command(
            chat_id,
            "/help",
        )

        if response:
            send_long_message(
                chat_id,
                response,
            )

    except Exception as exc:

        logger.exception(
            "Could not send help message: %s",
            exc,
        )


# ------------------------------------------------------------
# Telegram getUpdates
# ------------------------------------------------------------

def telegram_get_updates(
    offset: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Gets Telegram updates using long polling.
    """

    params: Dict[str, Any] = {
        "timeout": int(
            TELEGRAM_POLL_TIMEOUT
        ),
        "allowed_updates": json.dumps(
            [
                "message",
            ]
        ),
    }

    if offset is not None:
        params["offset"] = int(
            offset
        )

    try:

        data = telegram_api_request(
            "getUpdates",
            params,
        )

    except Exception as exc:

        logger.warning(
            "Telegram getUpdates failed: %s",
            exc,
        )

        return []

    if not data:
        return []

    if not isinstance(
        data,
        dict,
    ):
        logger.warning(
            "Unexpected Telegram getUpdates response type."
        )
        return []

    result = data.get(
        "result",
        [],
    )

    if not isinstance(
        result,
        list,
    ):
        logger.warning(
            "Unexpected Telegram getUpdates result type."
        )
        return []

    return result


# ------------------------------------------------------------
# Telegram webhook
# ------------------------------------------------------------

def telegram_delete_webhook() -> bool:
    """
    Removes an existing Telegram webhook so polling can work.
    """

    try:

        data = telegram_api_request(
            "deleteWebhook",
            {
                "drop_pending_updates": False,
            },
        )

    except Exception as exc:

        logger.warning(
            "Telegram webhook removal failed: %s",
            exc,
        )

        return False

    success = bool(
        data
        and isinstance(data, dict)
        and data.get("ok")
    )

    if success:

        logger.info(
            "Telegram webhook removed successfully."
        )

    else:

        logger.warning(
            "Telegram webhook removal was not confirmed."
        )

    return success


# ------------------------------------------------------------
# PostgreSQL polling advisory lock
# ------------------------------------------------------------

def acquire_polling_lock() -> bool:
    """
    Acquires the PostgreSQL advisory lock.

    The function retries while another Geniosa
    process owns the lock.
    """

    global POLLING_LOCK_CONNECTION
    global POLLING_LOCK_ACQUIRED

    if not DATABASE_URL:

        logger.warning(
            "DATABASE_URL is missing. "
            "Telegram polling lock cannot be created."
        )

        return False

    if POLLING_LOCK_ACQUIRED:

        return True

    while not POLLING_STOP.is_set():

        connection = None
        cursor = None

        try:

            connection = psycopg2.connect(
                DATABASE_URL,
                connect_timeout=DATABASE_CONNECT_TIMEOUT,
            )

            connection.autocommit = True

            cursor = connection.cursor()

            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (
                    POLLING_LOCK_ID,
                ),
            )

            result = cursor.fetchone()

            acquired = bool(
                result
                and result[0]
            )

            if acquired:

                POLLING_LOCK_CONNECTION = (
                    connection
                )

                POLLING_LOCK_ACQUIRED = True

                logger.info(
                    "Telegram polling advisory lock acquired."
                )

                return True

            logger.warning(
                "Another Geniosa instance already owns "
                "the Telegram polling lock. Retrying..."
            )

            try:
                connection.close()
            except Exception:
                pass

            connection = None

            POLLING_STOP.wait(
                5
            )

        except Exception as exc:

            logger.warning(
                "Could not acquire polling lock: %s. "
                "Retrying...",
                exc,
            )

            if connection:

                try:
                    connection.close()
                except Exception:
                    pass

            POLLING_STOP.wait(
                5
            )

        finally:

            if cursor:

                try:
                    cursor.close()
                except Exception:
                    pass

    return False


# ------------------------------------------------------------
# Release PostgreSQL polling lock
# ------------------------------------------------------------

def release_polling_lock() -> None:
    """
    Releases PostgreSQL advisory lock.
    """

    global POLLING_LOCK_CONNECTION
    global POLLING_LOCK_ACQUIRED

    connection = POLLING_LOCK_CONNECTION

    POLLING_LOCK_CONNECTION = None
    POLLING_LOCK_ACQUIRED = False

    if not connection:
        return

    cursor = None

    try:

        cursor = connection.cursor()

        cursor.execute(
            "SELECT pg_advisory_unlock(%s)",
            (
                POLLING_LOCK_ID,
            ),
        )

        logger.info(
            "Telegram polling advisory lock released."
        )

    except Exception as exc:

        logger.warning(
            "Could not release polling lock cleanly: %s",
            exc,
        )

    finally:

        if cursor:

            try:
                cursor.close()
            except Exception:
                pass

        try:
            connection.close()
        except Exception:
            pass


# ------------------------------------------------------------
# Telegram document processing
# ------------------------------------------------------------

def process_telegram_document(
    chat_id: Any,
    document: Dict[str, Any],
) -> str:
    """
    Downloads and analyzes a Telegram document.
    """

    if not isinstance(
        document,
        dict,
    ):
        return (
            "❌ დოკუმენტის მონაცემები არასწორია."
        )

    file_id = document.get(
        "file_id"
    )

    filename = (
        document.get("file_name")
        or f"telegram_file_{file_id}"
    )

    if not file_id:

        return (
            "❌ დოკუმენტის ID ვერ მივიღე."
        )

    filepath = None

    try:

        filepath = telegram_download_file(
            file_id
        )

    except Exception as exc:

        logger.exception(
            "Telegram document download failed: %s",
            exc,
        )

        return (
            "❌ ფაილის ჩამოტვირთვა ვერ მოხერხდა."
        )

    if not filepath:

        return (
            "❌ ფაილის ჩამოტვირთვა ვერ მოხერხდა."
        )

    try:

        path = Path(
            filepath
        )

        if not path.exists():
            return (
                "❌ ჩამოტვირთული ფაილი ვერ მოიძებნა."
            )

        file_type = detect_file_type(
            filename
        )

        if not file_type:
            file_type = detect_file_type(
                str(path)
            )

        extracted_text = extract_file_text(
            str(path)
        )

        if not extracted_text:

            return (
                "⚠️ ფაილიდან ტექსტის ამოღება ვერ მოხერხდა."
            )

        extracted_text = str(
            extracted_text
        ).strip()

        if not extracted_text:

            return (
                "⚠️ ფაილიდან ტექსტის ამოღება ვერ მოხერხდა."
            )

        record_id = save_document_record(
            chat_id=chat_id,
            filename=filename,
            file_type=file_type,
            extracted_text=extracted_text,
        )

        if not record_id:

            logger.warning(
                "Document record was not created."
            )

        # PART 7 canonical signature:
        # analyze_document_with_ai(chat_id, document_id)
        if record_id:

            analysis = analyze_document_with_ai(
                chat_id=chat_id,
                document_id=record_id,
            )

        else:

            analysis = (
                "⚠️ დოკუმენტის ჩანაწერის შექმნა ვერ მოხერხდა."
            )

        if not analysis:

            analysis = (
                "⚠️ დოკუმენტის ანალიზის შედეგი ვერ მივიღე."
            )

        if record_id:

            try:

                update_document_analysis(
                    chat_id=chat_id,
                    document_id=record_id,
                    analysis=analysis,
                )

            except Exception as exc:

                logger.warning(
                    "Could not save document analysis: %s",
                    exc,
                )

        return (
            f"📄 ფაილი მიღებულია: {filename}\n\n"
            f"{analysis}"
        )

    except Exception as exc:

        logger.exception(
            "Document processing failed: %s",
            exc,
        )

        return (
            "❌ დოკუმენტის დამუშავებისას "
            "შეცდომა მოხდა."
        )

    finally:

        if filepath:

            try:

                cleanup_temp_file(
                    filepath
                )

            except Exception as exc:

                logger.warning(
                    "Could not clean temporary document: %s",
                    exc,
                )


# ------------------------------------------------------------
# Telegram photo processing
# ------------------------------------------------------------

def process_telegram_photo(
    chat_id: Any,
    photos: List[Dict[str, Any]],
    caption: str = "",
) -> str:
    """
    Downloads the largest Telegram photo and analyzes it.
    """

    if not photos:

        return (
            "❌ ფოტო ვერ მივიღე."
        )

    photo = photos[-1]

    if not isinstance(
        photo,
        dict,
    ):
        return (
            "❌ ფოტოს მონაცემები არასწორია."
        )

    file_id = photo.get(
        "file_id"
    )

    if not file_id:

        return (
            "❌ ფოტოს ID ვერ მივიღე."
        )

    filepath = None

    try:

        filepath = telegram_download_file(
            file_id
        )

    except Exception as exc:

        logger.exception(
            "Telegram photo download failed: %s",
            exc,
        )

        return (
            "❌ ფოტოს ჩამოტვირთვა ვერ მოხერხდა."
        )

    if not filepath:

        return (
            "❌ ფოტოს ჩამოტვირთვა ვერ მოხერხდა."
        )

    try:

        path = Path(
            filepath
        )

        if not path.exists():

            return (
                "❌ ჩამოტვირთული ფოტო ვერ მოიძებნა."
            )

        result = analyze_image_with_ai(
            chat_id=chat_id,
            filepath=str(path),
            user_caption=caption,
        )

        if not result:

            return (
                "⚠️ ფოტოს ანალიზის შედეგი ვერ მივიღე."
            )

        return str(
            result
        )

    except Exception as exc:

        logger.exception(
            "Photo processing failed: %s",
            exc,
        )

        return (
            "❌ ფოტოს ანალიზისას "
            "შეცდომა მოხდა."
        )

    finally:

        if filepath:

            try:

                cleanup_temp_file(
                    filepath
                )

            except Exception as exc:

                logger.warning(
                    "Could not clean temporary photo: %s",
                    exc,
                )


# ------------------------------------------------------------
# Telegram message processing
# ------------------------------------------------------------

def process_telegram_message(
    message: Dict[str, Any],
) -> None:
    """
    Processes one Telegram message.
    """

    if not isinstance(
        message,
        dict,
    ):
        return

    chat = message.get(
        "chat"
    ) or {}

    if not isinstance(
        chat,
        dict,
    ):
        return

    chat_id = chat.get(
        "id"
    )

    if chat_id is None:
        return

    user = message.get(
        "from"
    ) or {}

    if not isinstance(
        user,
        dict,
    ):
        user = {}

    username = (
        user.get("username")
        or user.get("first_name")
        or "user"
    )

    logger.info(
        "Telegram message from %s in chat %s",
        username,
        chat_id,
    )

    # --------------------------------------------------------
    # ACCESS CONTROL
    # --------------------------------------------------------

    try:

        if not is_chat_allowed(
            chat_id
        ):

            send_message(
                chat_id,
                "⛔ წვდომა შეზღუდულია.",
            )

            return

    except Exception as exc:

        logger.exception(
            "Chat authorization check failed: %s",
            exc,
        )

        send_message(
            chat_id,
            "❌ მომხმარებლის ავტორიზაციის შემოწმება ვერ მოხერხდა.",
        )

        return

    text = (
        message.get("text")
        or ""
    ).strip()

    caption = (
        message.get("caption")
        or ""
    ).strip()

    document = message.get(
        "document"
    )

    photos = message.get(
        "photo"
    )

    try:

        # ----------------------------------------------------
        # DOCUMENT
        # ----------------------------------------------------

        if document:

            send_chat_action(
                chat_id,
                "typing",
            )

            response = process_telegram_document(
                chat_id,
                document,
            )

            send_long_message(
                chat_id,
                response,
            )

            return

        # ----------------------------------------------------
        # PHOTO
        # ----------------------------------------------------

        if photos:

            send_chat_action(
                chat_id,
                "typing",
            )

            response = process_telegram_photo(
                chat_id,
                photos,
                caption,
            )

            send_long_message(
                chat_id,
                response,
            )

            return

        # ----------------------------------------------------
        # TEXT
        # ----------------------------------------------------

        if text:

            send_chat_action(
                chat_id,
                "typing",
            )

            response = handle_text_message(
                chat_id,
                text,
            )

            if response:

                send_long_message(
                    chat_id,
                    response,
                )

            return

        # ----------------------------------------------------
        # UNSUPPORTED MESSAGE TYPE
        # ----------------------------------------------------

        send_message(
            chat_id,
            (
                "📎 ფაილი ან შეტყობინება მივიღე, "
                "მაგრამ ამ ტიპის მონაცემის დამუშავება "
                "ჯერ არ არის მხარდაჭერილი."
            ),
        )

    except Exception as exc:

        logger.exception(
            "Telegram message processing failed: %s",
            exc,
        )

        try:

            send_message(
                chat_id,
                (
                    "❌ დამუშავებისას მოხდა ტექნიკური "
                    "შეცდომა. სცადე ხელახლა."
                ),
            )

        except Exception:

            logger.exception(
                "Could not send Telegram error message."
            )


# ------------------------------------------------------------
# Telegram update processing
# ------------------------------------------------------------

def process_telegram_update(
    update: Dict[str, Any],
) -> None:
    """
    Processes a single Telegram update.
    """

    if not isinstance(
        update,
        dict,
    ):
        return

    message = update.get(
        "message"
    )

    if not isinstance(
        message,
        dict,
    ):
        return

    process_telegram_message(
        message
    )


# ------------------------------------------------------------
# Main Telegram polling loop
# ------------------------------------------------------------

def telegram_polling_loop() -> None:
    """
    Main Telegram long-polling loop.

    PostgreSQL advisory lock prevents multiple
    Geniosa instances from polling Telegram simultaneously.
    """

    global TELEGRAM_OFFSET
    global LAST_UPDATE_ID

    logger.info(
        "Geniosa Telegram polling loop started."
    )

    if not acquire_polling_lock():

        logger.warning(
            "Telegram polling stopped before lock acquisition."
        )

        return

    try:

        telegram_delete_webhook()

        while not POLLING_STOP.is_set():

            try:

                updates = telegram_get_updates(
                    TELEGRAM_OFFSET
                )

                if not updates:
                    continue

                for update in updates:

                    if POLLING_STOP.is_set():
                        break

                    if not isinstance(
                        update,
                        dict,
                    ):
                        continue

                    update_id = update.get(
                        "update_id"
                    )

                    # Process the update first.
                    # Advance the offset only after the
                    # update has been handed to the processor.
                    try:

                        process_telegram_update(
                            update
                        )

                    except Exception as exc:

                        logger.exception(
                            "Telegram update processing failed: %s",
                            exc,
                        )

                    if update_id is not None:

                        try:

                            next_offset = (
                                int(update_id) + 1
                            )

                            TELEGRAM_OFFSET = (
                                next_offset
                            )

                            LAST_UPDATE_ID = (
                                next_offset
                            )

                        except (
                            TypeError,
                            ValueError,
                        ):

                            logger.warning(
                                "Invalid Telegram update_id: %s",
                                update_id,
                            )

            except requests.RequestException as exc:

                logger.warning(
                    "Telegram network error: %s",
                    exc,
                )

                POLLING_STOP.wait(
                    5
                )

            except Exception as exc:

                logger.exception(
                    "Telegram polling error: %s",
                    exc,
                )

                POLLING_STOP.wait(
                    5
                )

    finally:

        release_polling_lock()

        logger.info(
            "Geniosa Telegram polling loop stopped."
        )


# ------------------------------------------------------------
# Start Telegram polling
# ------------------------------------------------------------

def start_telegram_polling() -> bool:
    """
    Starts Telegram polling in a background thread.
    """

    global POLLING_THREAD

    if not TELEGRAM_BOT_TOKEN:

        logger.error(
            "Cannot start Telegram polling: "
            "TELEGRAM_BOT_TOKEN is missing."
        )

        return False

    with POLLING_THREAD_LOCK:

        if POLLING_THREAD is not None:

            if POLLING_THREAD.is_alive():

                logger.info(
                    "Telegram polling is already running."
                )

                return True

        POLLING_STOP.clear()

        POLLING_THREAD = threading.Thread(
            target=telegram_polling_loop,
            name="geniosa-telegram-polling",
            daemon=True,
        )

        POLLING_THREAD.start()

    logger.info(
        "Telegram polling thread started."
    )

    return True


# ------------------------------------------------------------
# Stop Telegram polling
# ------------------------------------------------------------

def stop_telegram_polling() -> None:
    """
    Stops Telegram polling gracefully.
    """

    global POLLING_THREAD

    POLLING_STOP.set()

    thread = POLLING_THREAD

    if thread is not None:

        if (
            thread.is_alive()
            and thread is not threading.current_thread()
        ):

            thread.join(
                timeout=10
            )

    POLLING_THREAD = None

    release_polling_lock()

    logger.info(
        "Telegram polling shutdown completed."
    )


# ------------------------------------------------------------
# Geniosa initialization
# ------------------------------------------------------------

def initialize_geniosa() -> None:
    """
    Initializes database and application dependencies.
    """

    logger.info(
        "Initializing %s %s...",
        APP_NAME,
        APP_VERSION,
    )

    try:

        environment = validate_environment()

    except Exception as exc:

        logger.exception(
            "Environment validation failed: %s",
            exc,
        )

        environment = {
            "database": bool(
                DATABASE_URL
            ),
            "telegram": bool(
                TELEGRAM_BOT_TOKEN
            ),
            "gemini": bool(
                GEMINI_API_KEY
            ),
        }

    database_ready = False

    if not environment.get(
        "database",
        False,
    ):

        logger.warning(
            "DATABASE_URL is not configured."
        )

    else:

        try:

            ensure_database_ready()

            database_ready = True

            logger.info(
                "PostgreSQL database initialized."
            )

        except Exception as exc:

            logger.exception(
                "Database initialization failed: %s",
                exc,
            )

    if not environment.get(
        "telegram",
        False,
    ):

        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    if not environment.get(
        "gemini",
        False,
    ):

        logger.warning(
            "GEMINI_API_KEY is not configured."
        )

    logger.info(
        "Geniosa initialization completed."
    )

    if (
        TELEGRAM_BOT_TOKEN
        and not database_ready
    ):

        logger.warning(
            "Telegram polling will not start because "
            "the database is not ready."
        )


# ------------------------------------------------------------
# Geniosa shutdown
# ------------------------------------------------------------

def shutdown_geniosa() -> None:
    """
    Gracefully shuts down Geniosa.
    """

    logger.info(
        "Shutting down Geniosa..."
    )

    try:

        stop_telegram_polling()

    except Exception as exc:

        logger.exception(
            "Telegram polling shutdown failed: %s",
            exc,
        )

    logger.info(
        "Geniosa shutdown completed."
    )


# ------------------------------------------------------------
# FastAPI lifespan
# ------------------------------------------------------------

@asynccontextmanager
async def geniosa_lifespan(
    application: FastAPI,
):
    """
    FastAPI lifespan handler.
    """

    initialize_geniosa()

    should_start_polling = False

    if TELEGRAM_BOT_TOKEN and DATABASE_URL:

        try:

            should_start_polling = database_is_available()

        except Exception as exc:

            logger.warning(
                "Could not verify database before polling: %s",
                exc,
            )

            should_start_polling = False

    if should_start_polling:

        started = start_telegram_polling()

        if not started:

            logger.error(
                "Telegram polling could not be started."
            )

    elif TELEGRAM_BOT_TOKEN:

        logger.warning(
            "Telegram polling was not started because "
            "the database is unavailable."
        )

    try:

        yield

    finally:

        shutdown_geniosa()


# ------------------------------------------------------------
# Register FastAPI lifespan
# ------------------------------------------------------------

app.router.lifespan_context = geniosa_lifespan


# ------------------------------------------------------------
# Geniosa information endpoint
# ------------------------------------------------------------

@app.get("/geniosa")
def geniosa_info():

    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "status": "running",
    }


# ============================================================
# GENIOSA 4.0 — PART 12/12 LOADED
# ============================================================

print(
    "GENIOSA 4.0 — PART 12/12 LOADED"
)

print(
    "GENIOSA 4.0 — FULL APP LOADED"
)
