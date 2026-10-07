# ============================================================
# GENIOSA 4.0 — PART 1/12
# Core configuration, environment, FastAPI,
# Telegram, Gemini, PostgreSQL and health checks
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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager

import requests
import psycopg2
import psycopg2.extras

from fastapi import FastAPI
from fastapi.responses import JSONResponse


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("geniosa")


# ============================================================
# APPLICATION SETTINGS
# ============================================================

APP_NAME = "Geniosa"

APP_VERSION = "4.0"

APP_ENV = os.getenv(
    "APP_ENV",
    "production",
).strip()

TIMEZONE = os.getenv(
    "TIMEZONE",
    "Asia/Tbilisi",
).strip()


# ============================================================
# TELEGRAM SETTINGS
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_REQUEST_TIMEOUT = int(
    os.getenv(
        "TELEGRAM_REQUEST_TIMEOUT",
        "30",
    )
)

TELEGRAM_API_BASE = (
    "https://api.telegram.org/bot"
    + TELEGRAM_BOT_TOKEN
)

TELEGRAM_FILE_BASE = (
    "https://api.telegram.org/file/bot"
    + TELEGRAM_BOT_TOKEN
)

TELEGRAM_POLL_INTERVAL = float(
    os.getenv(
        "TELEGRAM_POLL_INTERVAL",
        "2",
    )
)

TELEGRAM_POLL_TIMEOUT = int(
    os.getenv(
        "TELEGRAM_POLL_TIMEOUT",
        "25",
    )
)


# ============================================================
# GEMINI SETTINGS
# ============================================================

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    "",
).strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite",
).strip()

GEMINI_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/models"
)

GEMINI_TIMEOUT = int(
    os.getenv(
        "GEMINI_TIMEOUT",
        "120",
    )
)


# ============================================================
# DATABASE SETTINGS
# ============================================================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "",
).strip()

DATABASE_CONNECT_TIMEOUT = int(
    os.getenv(
        "DATABASE_CONNECT_TIMEOUT",
        "10",
    )
)


# ============================================================
# OWNER SETTINGS
# ============================================================

OWNER_ID_RAW = os.getenv(
    "OWNER_ID",
    "",
).strip()

try:
    OWNER_ID = int(
        OWNER_ID_RAW
    ) if OWNER_ID_RAW else None

except ValueError:
    OWNER_ID = None


# ============================================================
# STORAGE
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

STORAGE_DIR = os.path.join(
    BASE_DIR,
    "storage",
)

UPLOADS_DIR = os.path.join(
    STORAGE_DIR,
    "uploads",
)

GENERATED_DIR = os.path.join(
    STORAGE_DIR,
    "generated",
)

TEMP_DIR = os.path.join(
    STORAGE_DIR,
    "temp",
)


os.makedirs(
    STORAGE_DIR,
    exist_ok=True,
)

os.makedirs(
    UPLOADS_DIR,
    exist_ok=True,
)

os.makedirs(
    GENERATED_DIR,
    exist_ok=True,
)

os.makedirs(
    TEMP_DIR,
    exist_ok=True,
)


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
)


# ============================================================

# GLOBAL POLLING STATE

# ============================================================

POLLING_STOP = threading.Event()

POLLING_THREAD = None

POLLING_THREAD_LOCK = threading.Lock()

POLLING_LOCK_CONNECTION = None

POLLING_LOCK_ACQUIRED = False

LAST_UPDATE_ID = 0


# ============================================================
# DATABASE LOCK ID
# ============================================================
POLLING_LOCK_ID = int(
    os.getenv(
        "POLLING_LOCK_ID",
        "406391202",
    )
)


# ============================================================
# DOCUMENT SETTINGS
# ============================================================

MAX_DOCUMENT_SIZE_MB = int(
    os.getenv(
        "MAX_DOCUMENT_SIZE_MB",
        "20",
    )
)

MAX_DOCUMENT_SIZE_BYTES = (
    MAX_DOCUMENT_SIZE_MB
    * 1024
    * 1024
)


# ============================================================
# SECURITY / ACCESS
# ============================================================

ALLOWED_CHAT_IDS_RAW = os.getenv(
    "ALLOWED_CHAT_IDS",
    "",
).strip()

ALLOWED_CHAT_IDS = set()

if ALLOWED_CHAT_IDS_RAW:

    for value in ALLOWED_CHAT_IDS_RAW.split(","):

        value = value.strip()

        if not value:
            continue

        try:
            ALLOWED_CHAT_IDS.add(
                int(value)
            )

        except ValueError:

            logger.warning(
                "Invalid ALLOWED_CHAT_IDS value: %s",
                value,
            )


def is_chat_allowed(
    chat_id: Optional[int],
) -> bool:
    """
    Проверяет, разрешён ли Telegram chat_id.

    Если список ALLOWED_CHAT_IDS пустой,
    доступ разрешён.
    """

    if not ALLOWED_CHAT_IDS:
        return True

    if chat_id is None:
        return False

    try:
        return int(chat_id) in ALLOWED_CHAT_IDS

    except (TypeError, ValueError):
        return False


# ============================================================
# ENVIRONMENT VALIDATION
# ============================================================

def get_environment_status() -> Dict[str, bool]:

    return {
        "telegram": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini": bool(
            GEMINI_API_KEY
        ),
        "database": bool(
            DATABASE_URL
        ),
    }


def validate_environment() -> Dict[str, bool]:
    """
    Проверяет основные переменные окружения.
    """

    status = get_environment_status()

    if not status["telegram"]:

        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    if not status["gemini"]:

        logger.warning(
            "GEMINI_API_KEY is not configured"
        )

    if not status["database"]:

        logger.warning(
            "DATABASE_URL is not configured"
        )

    return status


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db_connection():

    if not DATABASE_URL:

        raise RuntimeError(
            "DATABASE_URL is not configured"
        )

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=DATABASE_CONNECT_TIMEOUT,
    )


# ============================================================
# DATABASE AVAILABILITY
# ============================================================

def database_available() -> bool:

    if not DATABASE_URL:
        return False

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

        logger.error(
            "Database availability check failed: %s",
            exc,
        )

        return False

    finally:

        if connection is not None:

            try:
                connection.close()

            except Exception:
                pass


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api_url(
    method: str,
) -> str:

    return (
        TELEGRAM_API_BASE
        + "/"
        + method
    )


def telegram_request(
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
) -> Optional[Dict[str, Any]]:

    if not TELEGRAM_BOT_TOKEN:

        logger.error(
            "Telegram bot token is not configured"
        )

        return None

    if payload is None:
        payload = {}

    request_timeout = (
        timeout
        if timeout is not None
        else TELEGRAM_REQUEST_TIMEOUT
    )

    try:

        response = requests.post(
            telegram_api_url(method),
            json=payload,
            timeout=request_timeout,
        )

        if response.status_code != 200:

            logger.error(
                "Telegram API HTTP %s: %s",
                response.status_code,
                response.text[:1000],
            )

            return None

        data = response.json()

        if not data.get("ok"):

            logger.error(
                "Telegram API error: %s",
                data,
            )

            return None

        return data

    except requests.RequestException as exc:

        logger.error(
            "Telegram request failed: %s",
            exc,
        )

        return None

    except Exception as exc:

        logger.error(
            "Telegram request unexpected error: %s",
            exc,
        )

        return None


# ============================================================
# TELEGRAM SEND MESSAGE
# ============================================================

def send_telegram_message(
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
) -> bool:

    if not text:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": (
            disable_web_page_preview
        ),
    }

    if parse_mode:
        payload["parse_mode"] = parse_mode

    result = telegram_request(
        "sendMessage",
        payload,
    )

    return bool(
        result
        and result.get("ok")
    )


# ============================================================
# TELEGRAM CHAT ACTION
# ============================================================

def send_chat_action(
    chat_id: int,
    action: str = "typing",
) -> bool:

    result = telegram_request(
        "sendChatAction",
        {
            "chat_id": chat_id,
            "action": action,
        },
    )

    return bool(
        result
        and result.get("ok")
    )


# ============================================================
# TELEGRAM BOT INFORMATION
# ============================================================

def telegram_get_me() -> Optional[Dict[str, Any]]:

    result = telegram_request(
        "getMe",
        {},
    )

    if not result:
        return None

    return result.get(
        "result"
    )


# ============================================================
# GEMINI API
# ============================================================

def gemini_api_url(
    model: Optional[str] = None,
) -> str:

    selected_model = (
        model
        or GEMINI_MODEL
    ).strip()

    return (
        GEMINI_API_BASE
        + "/"
        + selected_model
        + ":generateContent"
    )


# ============================================================
# BASIC TEXT UTILITIES
# ============================================================

def safe_text(
    value: Any,
) -> str:

    if value is None:
        return ""

    return str(value).strip()


def normalize_text(
    value: Any,
) -> str:

    text = safe_text(
        value
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def utc_now() -> datetime:

    return datetime.now(
        timezone.utc
    )


def generate_uuid() -> str:

    return str(
        uuid.uuid4()
    )


# ============================================================
# FASTAPI ROOT
# ============================================================

@app.get("/")
def root():

    env = get_environment_status()

    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "status": "online",
        "telegram_configured": env["telegram"],
        "gemini_configured": env["gemini"],
        "database_configured": env["database"],
    }


# ============================================================
# HEAD HEALTH CHECK
# ============================================================

@app.head("/")
def root_head():

    return None


# ============================================================
# STATUS ENDPOINT
# ============================================================

@app.get("/status")
def status():

    env = get_environment_status()

    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "status": "online",
        "environment": APP_ENV,
        "telegram_configured": env["telegram"],
        "gemini_configured": env["gemini"],
        "database_configured": env["database"],
        "gemini_model": GEMINI_MODEL,
        "polling_thread_alive": (
            POLLING_THREAD.is_alive()
            if POLLING_THREAD
            else False
        ),
    }


# ============================================================
# DATABASE HEALTH CHECK
# ============================================================

@app.get("/health/database")
def database_health():

    available = database_available()

    return JSONResponse(
        status_code=(
            200
            if available
            else 503
        ),
        content={
            "database": (
                "ok"
                if available
                else "unavailable"
            )
        },
    )


# ============================================================
# GENERAL HEALTH CHECK
# ============================================================

@app.get("/health")
def health():

    env = get_environment_status()

    db_ok = database_available()

    overall_ok = (
        env["telegram"]
        and env["gemini"]
        and env["database"]
        and db_ok
    )

    return JSONResponse(
        status_code=(
            200
            if overall_ok
            else 503
        ),
        content={
            "service": APP_NAME,
            "version": APP_VERSION,
            "status": (
                "healthy"
                if overall_ok
                else "degraded"
            ),
            "telegram": env["telegram"],
            "gemini": env["gemini"],
            "database": env["database"],
            "database_connection": db_ok,
            "gemini_model": GEMINI_MODEL,
        },
    )


# ============================================================
# STARTUP CONFIGURATION LOG
# ============================================================

def log_startup_configuration():

    logger.info(
        "============================================================"
    )

    logger.info(
        "GENIOSA 4.0 configuration"
    )

    logger.info(
        "APP_ENV=%s",
        APP_ENV,
    )

    logger.info(
        "Telegram configured=%s",
        bool(
            TELEGRAM_BOT_TOKEN
        ),
    )

    logger.info(
        "Gemini configured=%s",
        bool(
            GEMINI_API_KEY
        ),
    )

    logger.info(
        "Gemini model=%s",
        GEMINI_MODEL,
    )

    logger.info(
        "Database configured=%s",
        bool(
            DATABASE_URL
        ),
    )

    logger.info(
        "Telegram request timeout=%s",
        TELEGRAM_REQUEST_TIMEOUT,
    )

    logger.info(
        "============================================================"
    )


# ============================================================
# INITIAL ENVIRONMENT CHECK
# ============================================================

validate_environment()

log_startup_configuration()


# ============================================================
# PART 1 COMPLETE
# ============================================================

print(
    "GENIOSA 4.0 — PART 1/12 LOADED"
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
            query[:500],
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
# 2.2 — DATABASE INITIALIZATION
# ============================================================

def init_db() -> bool:
    """
    Creates all Geniosa database tables and applies
    safe, non-destructive schema migrations.

    Existing database data is preserved.
    This function can be executed repeatedly.
    """

    connection = None
    cursor = None

    try:

        connection = db()

        cursor = connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        )

        # ====================================================
        # MESSAGES
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in message_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

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

        legacy_message_columns = [
            "content",
            "message_text",
            "message",
            "body",
        ]

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
            """
        )

        cursor.execute(
            """
            UPDATE messages
            SET chat_id = '0'
            WHERE chat_id IS NULL
            """
        )

        # ====================================================
        # BUSINESS MEMORY
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS business_memory (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                memory TEXT NOT NULL,

                category TEXT DEFAULT 'general',

                importance INTEGER DEFAULT 5,

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in business_memory_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE business_memory
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        cursor.execute(
            """
            UPDATE business_memory
            SET category = 'general'
            WHERE category IS NULL
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
        # PROJECTS
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

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in project_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE projects
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        cursor.execute(
            """
            UPDATE projects
            SET status = 'active'
            WHERE status IS NULL
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
        # DOCUMENTS
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

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in document_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE documents
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
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
        # INVESTORS
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

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in investor_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE investors
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        cursor.execute(
            """
            UPDATE investors
            SET status = 'new'
            WHERE status IS NULL
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
        # DEALS
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

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in deal_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE deals
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        cursor.execute(
            """
            UPDATE deals
            SET stage = 'new'
            WHERE stage IS NULL
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
        # RESEARCH
        # ====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS research (

                id BIGSERIAL PRIMARY KEY,

                chat_id TEXT NOT NULL,

                project_id BIGINT,

                query TEXT NOT NULL,

                result TEXT,

                created_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in research_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE research
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        # ====================================================
        # FINANCIAL ANALYSES
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

                created_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in financial_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE financial_analyses
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        # ====================================================
        # GENERATED ASSETS
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

                created_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in generated_asset_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE generated_assets
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
                """
            )

        # ====================================================
        # SECURITIES
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

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in securities_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE securities
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
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
        # CRYPTO ASSETS
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

                created_at TIMESTAMP NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP NOT NULL
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
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

            "updated_at": (
                "TIMESTAMP NOT NULL "
                "DEFAULT CURRENT_TIMESTAMP"
            ),

        }

        for column_name, column_type in crypto_columns.items():

            cursor.execute(
                f"""
                ALTER TABLE crypto_assets
                ADD COLUMN IF NOT EXISTS
                {column_name} {column_type}
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
        # 2.3 — INDEXES
        # ====================================================

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_messages_chat_id
            ON messages(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_messages_created_at
            ON messages(created_at)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_business_memory_chat_id
            ON business_memory(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_business_memory_category
            ON business_memory(category)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_projects_chat_id
            ON projects(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_projects_status
            ON projects(status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_documents_chat_id
            ON documents(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_documents_project_id
            ON documents(project_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_investors_chat_id
            ON investors(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_investors_status
            ON investors(status)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_deals_chat_id
            ON deals(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_deals_project_id
            ON deals(project_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_deals_investor_id
            ON deals(investor_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_research_chat_id
            ON research(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_research_project_id
            ON research(project_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_financial_analyses_chat_id
            ON financial_analyses(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_financial_analyses_project_id
            ON financial_analyses(project_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_generated_assets_chat_id
            ON generated_assets(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_generated_assets_project_id
            ON generated_assets(project_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_securities_chat_id
            ON securities(chat_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_crypto_assets_chat_id
            ON crypto_assets(chat_id)
            """
        )

        # ====================================================
        # 2.4 — COMMIT
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
# 2.5 — DATABASE STARTUP TEST
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
# 2.6 — PART 2 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 2/12 LOADED"
) 


# ============================================================
# GENIOSA 4.0 — PART 3/12
# Messages, persistent business memory and AI context
# ============================================================


# ============================================================
# 3.1 — SAVE MESSAGE
# ============================================================

def save_message(
    chat_id: Any,
    role: str,
    text: Optional[str] = None,
    content: Optional[str] = None,
) -> Optional[int]:

    message_text = text

    if message_text is None:
        message_text = content

    if message_text is None:
        message_text = ""

    message_text = str(
        message_text
    ).strip()

    role = str(
        role
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
                str(chat_id),
                role,
                message_text,
            ),
            fetchone=True,
            commit=True,
        )

        if result:

            return int(
                result["id"]
            )

        return None

    except Exception as exc:

        logger.error(
            "Failed to save message: %s",
            exc
        )

        return None


# ============================================================
# 3.2 — GET RECENT MESSAGES
# ============================================================

def get_recent_messages(
    chat_id: Any,
    limit: int = 20,
) -> List[Dict[str, Any]]:

    try:

        safe_limit = max(
            1,
            min(
                int(limit),
                100,
            )
        )

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
                str(chat_id),
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
            exc
        )

        return []


# ============================================================
# 3.3 — FORMAT CONVERSATION HISTORY
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

        role = str(
            message.get(
                "role",
                "user"
            )
        ).upper()

        text_value = str(
            message.get(
                "text",
                ""
            )
        ).strip()

        if not text_value:
            continue

        lines.append(
            f"{role}: {text_value}"
        )

    return "\n".join(
        lines
    )


# ============================================================
# 3.4 — SAVE BUSINESS MEMORY
# ============================================================

def save_memory(
    chat_id: Any,
    memory: str,
    category: str = "general",
    importance: int = 5,
) -> Optional[int]:

    memory = str(
        memory or ""
    ).strip()

    category = str(
        category or "general"
    ).strip()

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
                str(chat_id),
                memory,
                category,
                importance,
            ),
            fetchone=True,
            commit=True,
        )

        if result:

            return int(
                result["id"]
            )

        return None

    except Exception as exc:

        logger.error(
            "Failed to save business memory: %s",
            exc
        )

        return None


# ============================================================
# 3.5 — GET BUSINESS MEMORIES
# ============================================================

def get_memories(
    chat_id: Any,
    limit: int = 30,
) -> List[Dict[str, Any]]:

    try:

        safe_limit = max(
            1,
            min(
                int(limit),
                100,
            )
        )

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
                str(chat_id),
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get business memories: %s",
            exc
        )

        return []


# ============================================================
# 3.6 — BUILD MEMORY CONTEXT
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

        memory = str(
            item.get(
                "memory",
                ""
            )
        ).strip()

        category = str(
            item.get(
                "category",
                "general"
            )
        ).strip()

        importance = item.get(
            "importance",
            5
        )

        if not memory:
            continue

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
# 3.7 — DELETE BUSINESS MEMORY
# ============================================================

def delete_memory(
    chat_id: Any,
    memory_id: int,
) -> bool:

    try:

        result = db_execute(
            """
            DELETE FROM business_memory
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                int(memory_id),
                str(chat_id),
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
            memory_id,
            exc
        )

        return False


# ============================================================
# 3.8 — MEMORY SUMMARY
# ============================================================

def memories_summary(
    chat_id: Any,
    limit: int = 50,
) -> str:

    memories = get_memories(
        chat_id,
        limit=limit,
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

        memory_id = item.get(
            "id"
        )

        category = item.get(
            "category",
            "general"
        )

        importance = item.get(
            "importance",
            5
        )

        memory = str(
            item.get(
                "memory",
                ""
            )
        ).strip()

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

    return "\n".join(
        lines
    ).strip()


# ============================================================
# 3.9 — INDUSTRY NAMES
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

    if not industry:

        return (
            "არ არის მითითებული"
        )

    value = str(
        industry
    ).strip()

    return INDUSTRY_NAMES.get(
        value.lower(),
        value
    )


# ============================================================
# 3.10 — PROJECT TO AI CONTEXT
# ============================================================

def project_to_ai_context(
    project: Optional[Dict[str, Any]]
) -> str:

    if not project:

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
            str
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
# 3.11 — BUILD PROJECTS CONTEXT
# ============================================================

def build_projects_context(
    chat_id: Any,
    limit: int = 20,
) -> str:

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
                updated_at DESC,
                id DESC
            LIMIT {max(1, min(int(limit), 50))}
            """,
            (
                str(chat_id),
            ),
            fetchall=True,
        )

        rows = rows or []

    except Exception as exc:

        logger.error(
            "Failed to build projects context: %s",
            exc
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

        project_id = project.get(
            "id"
        )

        name = project.get(
            "name",
            "Unnamed"
        )

        industry = industry_name(
            project.get(
                "industry"
            )
        )

        location = project.get(
            "location"
        ) or "N/A"

        expected_profit = project.get(
            "expected_profit"
        )

        total_cost = project.get(
            "total_cost"
        )

        expected_revenue = project.get(
            "expected_revenue"
        )

        status = project.get(
            "status"
        ) or "active"

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

    return "\n".join(
        lines
    )


# ============================================================
# 3.12 — BUILD BUSINESS CONTEXT
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
# 3.13 — SAFE TEXT NORMALIZATION
# ============================================================

def clean_context_text(
    value: Any,
    max_length: int = 12000,
) -> str:

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
# 3.14 — FULL AI CONTEXT
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
# 3.15 — PART 3 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 3/12 LOADED"
)# ============================================================

# GENIOSA 4.0 — PART 4/12

# Projects CRM — create, read, update, delete and context

# ============================================================

# ============================================================

# 4.1 — CREATE PROJECT

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

    project_name = str(

        name or ""

    ).strip()

    if not project_name:

        return None

    industry = (

        str(industry).strip()

        if industry is not None

        else None

    )

    location = (

        str(location).strip()

        if location is not None

        else None

    )

    description = (

        str(description).strip()

        if description is not None

        else None

    )

    notes = (

        str(notes).strip()

        if notes is not None

        else None

    )

    status = (

        str(status or "active")

        .strip()

        .lower()

    )

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

                str(chat_id),

                project_name,

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

                status,

            ),

            fetchone=True,

            commit=True,

        )

        if result:

            return int(

                result["id"]

            )

        return None

    except Exception as exc:

        logger.error(

            "Failed to create project: %s",

            exc

        )

        return None

# ============================================================

# 4.2 — GET PROJECTS

# ============================================================

def get_projects(

    chat_id: Any,

    limit: int = 50,

) -> List[Dict[str, Any]]:

    """

    Return projects belonging to the current chat.

    """

    try:

        safe_limit = max(

            1,

            min(

                int(limit),

                MAX_PROJECT_RECORDS,

            )

        )

        rows = db_execute(

            f"""

            SELECT *

            FROM projects

            WHERE chat_id = %s

            ORDER BY

                updated_at DESC,

                id DESC

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

            "Failed to get projects: %s",

            exc

        )

        return []

# ============================================================

# 4.3 — GET SINGLE PROJECT

# ============================================================

def get_project(

    chat_id: Any,

    project_id: int,

) -> Optional[Dict[str, Any]]:

    """

    Return one project belonging to the current chat.

    """

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

                int(project_id),

                str(chat_id),

            ),

            fetchone=True,

        )

        return result

    except Exception as exc:

        logger.error(

            "Failed to get project %s: %s",

            project_id,

            exc

        )

        return None

# ============================================================

# 4.4 — FIND PROJECT BY NAME

# ============================================================

def find_project_by_name(

    chat_id: Any,

    name: str,

) -> Optional[Dict[str, Any]]:

    """

    Find a project using a partial name match.

    """

    search_name = str(

        name or ""

    ).strip()

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

                updated_at DESC,

                id DESC

            LIMIT 1

            """,

            (

                str(chat_id),

                f"%{search_name}%",

            ),

            fetchone=True,

        )

        return result

    except Exception as exc:

        logger.error(

            "Failed to find project '%s': %s",

            search_name,

            exc

        )

        return None

# ============================================================

# 4.5 — UPDATE PROJECT

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

        if field_name == "name":

            field_value = str(

                field_value or ""

            ).strip()

            if not field_value:

                continue

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

        int(project_id),

        str(chat_id),

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

        return bool(result)

    except Exception as exc:

        logger.error(

            "Failed to update project %s: %s",

            project_id,

            exc

        )

        return False

# ============================================================

# 4.6 — DELETE PROJECT

# ============================================================

def delete_project(

    chat_id: Any,

    project_id: int,

) -> bool:

    """

    Delete one project belonging to the current chat.

    """

    try:

        result = db_execute(

            """

            DELETE FROM projects

            WHERE id = %s

              AND chat_id = %s

            RETURNING id

            """,

            (

                int(project_id),

                str(chat_id),

            ),

            fetchone=True,

            commit=True,

        )

        return bool(result)

    except Exception as exc:

        logger.error(

            "Failed to delete project %s: %s",

            project_id,

            exc

        )

        return False

# ============================================================

# 4.7 — PROJECT SUMMARY

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

    name = project.get(

        "name"

    ) or "უსახელო პროექტი"

    industry = industry_name(

        project.get(

            "industry"

        )

    )

    location = project.get(

        "location"

    ) or "არ არის მითითებული"

    description = project.get(

        "description"

    ) or ""

    status = project.get(

        "status"

    ) or "active"

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

            str(description),

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

        if value is not None:

            area_lines.append(

                f"{label}: "

                f"{format_number(value)} {unit}"

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

        if value is not None:

            financial_lines.append(

                f"{label}: "

                f"{format_money(value)}"

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

        if value_type == "money":

            display_value = format_money(

                value

            )

        else:

            display_value = (

                f"{format_number(value)}%"

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

    notes = project.get(

        "notes"

    )

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

# 4.8 — PROJECTS SUMMARY

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

        project_id = project.get(

            "id"

        )

        name = project.get(

            "name"

        ) or "უსახელო"

        location = project.get(

            "location"

        ) or "—"

        status = project.get(

            "status"

        ) or "active"

        expected_profit = project.get(

            "expected_profit"

        )

        profit_text = (

            format_money(

                expected_profit

            )

            if expected_profit is not None

            else "—"

        )

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

    return "\n".join(

        lines

    ).strip()

# ============================================================

# 4.9 — PROJECT DEAL IDS

# ============================================================

def get_project_deal_count(

    chat_id: Any,

    project_id: int,

) -> int:

    """

    Count deals connected to a project.

    Full deal operations are implemented in PART 5.

    """

    try:

        result = db_execute(

            """

            SELECT COUNT(*) AS total

            FROM deals

            WHERE chat_id = %s

              AND project_id = %s

            """,

            (

                str(chat_id),

                int(project_id),

            ),

            fetchone=True,

        )

        if not result:

            return 0

        return int(

            result.get(

                "total",

                0

            )

        )

    except Exception as exc:

        logger.error(

            "Failed to count project deals: %s",

            exc

        )

        return 0

# ============================================================

# 4.10 — PROJECT CONTEXT

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

# 4.11 — PROJECT STATUS NORMALIZATION

# ============================================================

def normalize_project_status(

    value: Optional[str]

) -> str:

    """

    Normalize common project status values.

    """

    if not value:

        return "active"

    text_value = str(

        value

    ).strip().lower()

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

# 4.12 — PROJECT FIELD CALCULATIONS

# ============================================================

def calculate_project_total_cost(

    project: Optional[Dict[str, Any]]

) -> float:

    """

    Calculate total project cost from available cost components.

    """

    if not project:

        return 0.0

    existing_total = project.get(

        "total_cost"

    )

    if existing_total is not None:

        try:

            return float(

                existing_total

            )

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

            total += float(

                value

            )

        except Exception:

            continue

    return total

# ============================================================

# 4.13 — CALCULATE PROJECT PROFIT

# ============================================================

def calculate_project_profit(

    project: Optional[Dict[str, Any]]

) -> float:

    """

    Calculate project profit from expected revenue minus total cost.

    """

    if not project:

        return 0.0

    existing_profit = project.get(

        "expected_profit"

    )

    if existing_profit is not None:

        try:

            return float(

                existing_profit

            )

        except Exception:

            pass

    revenue = (

        project.get(

            "expected_revenue"

        )

        if project.get(

            "expected_revenue"

        ) is not None

        else project.get(

            "revenue"

        )

    )

    if revenue is None:

        return 0.0

    try:

        revenue = float(

            revenue

        )

    except Exception:

        return 0.0

    total_cost = calculate_project_total_cost(

        project

    )

    return revenue - total_cost

# ============================================================

# 4.14 — PROJECT PROFIT MARGIN

# ============================================================

def calculate_project_margin(

    project: Optional[Dict[str, Any]]

) -> float:

    """

    Calculate expected profit margin as a percentage.

    """

    if not project:

        return 0.0

    revenue = (

        project.get(

            "expected_revenue"

        )

        if project.get(

            "expected_revenue"

        ) is not None

        else project.get(

            "revenue"

        )

    )

    if revenue is None:

        return 0.0

    try:

        revenue = float(

            revenue

        )

    except Exception:

        return 0.0

    if revenue == 0:

        return 0.0

    profit = calculate_project_profit(

        project

    )

    return (

        profit / revenue

    ) * 100.0

# ============================================================

# 4.15 — PART 4 COMPLETION MARKER

# ============================================================

print(

    "GENIOSA 4.0 — PART 4/12 LOADED"

)# ============================================================
# GENIOSA 4.0 — PART 5/12
# Investors & Deals CRM
# ============================================================


# ============================================================
# 5.1 — CREATE INVESTOR
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

    investor_name = str(
        name or ""
    ).strip()

    if not investor_name:
        return None

    company = (
        str(company).strip()
        if company is not None
        else None
    )

    country = (
        str(country).strip()
        if country is not None
        else None
    )

    contact = (
        str(contact).strip()
        if contact is not None
        else None
    )

    preferred_sector = (
        str(preferred_sector).strip()
        if preferred_sector is not None
        else None
    )

    notes = (
        str(notes).strip()
        if notes is not None
        else None
    )

    status = str(
        status or "new"
    ).strip().lower()

    try:

        if investment_capacity is not None:
            investment_capacity = float(
                investment_capacity
            )

    except Exception:

        investment_capacity = None

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
                str(chat_id),
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

        if result:
            return int(
                result["id"]
            )

        return None

    except Exception as exc:

        logger.error(
            "Failed to create investor: %s",
            exc
        )

        return None


# ============================================================
# 5.2 — GET INVESTORS
# ============================================================

def get_investors(
    chat_id: Any,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Return investors belonging to the current chat.
    """

    try:

        safe_limit = max(
            1,
            min(
                int(limit),
                MAX_INVESTOR_RECORDS,
            )
        )

        rows = db_execute(
            f"""
            SELECT *
            FROM investors
            WHERE chat_id = %s
            ORDER BY
                updated_at DESC,
                id DESC
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
            "Failed to get investors: %s",
            exc
        )

        return []


# ============================================================
# 5.3 — GET SINGLE INVESTOR
# ============================================================

def get_investor(
    chat_id: Any,
    investor_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Return one investor belonging to the current chat.
    """

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
                int(investor_id),
                str(chat_id),
            ),
            fetchone=True,
        )

    except Exception as exc:

        logger.error(
            "Failed to get investor %s: %s",
            investor_id,
            exc
        )

        return None


# ============================================================
# 5.4 — FIND INVESTOR
# ============================================================

def find_investor(
    chat_id: Any,
    search_text: str,
) -> Optional[Dict[str, Any]]:
    """
    Search investor by name, company, country or contact.
    """

    value = str(
        search_text or ""
    ).strip()

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
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            (
                str(chat_id),
                pattern,
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
            exc
        )

        return None


# ============================================================
# 5.5 — UPDATE INVESTOR
# ============================================================

def update_investor(
    chat_id: Any,
    investor_id: int,
    **fields,
) -> bool:
    """
    Update allowed investor fields.
    """

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

        if field_name == "investment_capacity":

            try:
                field_value = float(
                    field_value
                )
            except Exception:
                continue

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
        int(investor_id),
        str(chat_id),
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

        return bool(result)

    except Exception as exc:

        logger.error(
            "Failed to update investor %s: %s",
            investor_id,
            exc
        )

        return False


# ============================================================
# 5.6 — DELETE INVESTOR
# ============================================================

def delete_investor(
    chat_id: Any,
    investor_id: int,
) -> bool:
    """
    Delete one investor belonging to the current chat.
    """

    try:

        result = db_execute(
            """
            DELETE FROM investors
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                int(investor_id),
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:

        logger.error(
            "Failed to delete investor %s: %s",
            investor_id,
            exc
        )

        return False


# ============================================================
# 5.7 — FORMAT INVESTOR
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

    name = investor.get(
        "name"
    ) or "უცნობი"

    company = investor.get(
        "company"
    )

    country = investor.get(
        "country"
    )

    contact = investor.get(
        "contact"
    )

    capacity = investor.get(
        "investment_capacity"
    )

    sector = investor.get(
        "preferred_sector"
    )

    status = investor.get(
        "status"
    ) or "new"

    notes = investor.get(
        "notes"
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
        lines.append(
            f"💰 საინვესტიციო შესაძლებლობა: "
            f"{format_money(capacity)}"
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
            str(notes),
        ])

    return "\n".join(
        lines
    )


# ============================================================
# 5.8 — INVESTORS SUMMARY
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

        capacity_text = (
            format_money(capacity)
            if capacity is not None
            else "—"
        )

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
# 5.9 — CREATE DEAL
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

    project_id_value = None
    investor_id_value = None

    if project_id is not None:

        try:
            project_id_value = int(
                project_id
            )
        except Exception:
            return None

        project = get_project(
            chat_id,
            project_id_value,
        )

        if not project:
            return None

    if investor_id is not None:

        try:
            investor_id_value = int(
                investor_id
            )
        except Exception:
            return None

        investor = get_investor(
            chat_id,
            investor_id_value,
        )

        if not investor:
            return None

    try:

        proposed_amount = (
            float(proposed_amount)
            if proposed_amount is not None
            else None
        )

        proposed_share = (
            float(proposed_share)
            if proposed_share is not None
            else None
        )

        valuation = (
            float(valuation)
            if valuation is not None
            else None
        )

    except Exception:

        return None

    stage = str(
        stage or "new"
    ).strip().lower()

    notes = (
        str(notes).strip()
        if notes is not None
        else None
    )

    next_step = (
        str(next_step).strip()
        if next_step is not None
        else None
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
                str(chat_id),
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

        if result:
            return int(
                result["id"]
            )

        return None

    except Exception as exc:

        logger.error(
            "Failed to create deal: %s",
            exc
        )

        return None


# ============================================================
# 5.10 — GET DEALS
# ============================================================

def get_deals(
    chat_id: Any,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Return deals with related project and investor names.
    """

    try:

        safe_limit = max(
            1,
            min(
                int(limit),
                MAX_DEAL_RECORDS,
            )
        )

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
            "Failed to get deals: %s",
            exc
        )

        return []


# ============================================================
# 5.11 — GET SINGLE DEAL
# ============================================================

def get_deal(
    chat_id: Any,
    deal_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Return one deal with related CRM data.
    """

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
                int(deal_id),
                str(chat_id),
            ),
            fetchone=True,
        )

    except Exception as exc:

        logger.error(
            "Failed to get deal %s: %s",
            deal_id,
            exc
        )

        return None


# ============================================================
# 5.12 — UPDATE DEAL
# ============================================================

def update_deal(
    chat_id: Any,
    deal_id: int,
    **fields,
) -> bool:
    """
    Update allowed deal fields.
    """

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

                try:
                    field_value = int(
                        field_value
                    )
                except Exception:
                    continue

                if not get_project(
                    chat_id,
                    field_value,
                ):
                    continue

        elif field_name == "investor_id":

            if field_value is not None:

                try:
                    field_value = int(
                        field_value
                    )
                except Exception:
                    continue

                if not get_investor(
                    chat_id,
                    field_value,
                ):
                    continue

        elif field_name in {
            "proposed_amount",
            "proposed_share",
            "valuation",
        }:

            if field_value is not None:

                try:
                    field_value = float(
                        field_value
                    )
                except Exception:
                    continue

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
        int(deal_id),
        str(chat_id),
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

        return bool(result)

    except Exception as exc:

        logger.error(
            "Failed to update deal %s: %s",
            deal_id,
            exc
        )

        return False


# ============================================================
# 5.13 — DELETE DEAL
# ============================================================

def delete_deal(
    chat_id: Any,
    deal_id: int,
) -> bool:
    """
    Delete one deal belonging to the current chat.
    """

    try:

        result = db_execute(
            """
            DELETE FROM deals
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                int(deal_id),
                str(chat_id),
            ),
            fetchone=True,
            commit=True,
        )

        return bool(result)

    except Exception as exc:

        logger.error(
            "Failed to delete deal %s: %s",
            deal_id,
            exc
        )

        return False


# ============================================================
# 5.14 — FORMAT DEAL
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
        lines.append(
            f"💰 შეთავაზებული თანხა: "
            f"{format_money(proposed_amount)}"
        )

    if proposed_share is not None:
        lines.append(
            f"📈 შეთავაზებული წილი: "
            f"{format_number(proposed_share)}%"
        )

    if valuation is not None:
        lines.append(
            f"🏷️ შეფასება: "
            f"{format_money(valuation)}"
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
# 5.15 — DEALS SUMMARY
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

        amount_text = (
            format_money(amount)
            if amount is not None
            else "—"
        )

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
# 5.16 — GET PROJECT DEALS
# ============================================================

def get_project_deals(
    chat_id: Any,
    project_id: int,
) -> List[Dict[str, Any]]:
    """
    Return all deals connected to one project.
    """

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
                d.updated_at DESC,
                d.id DESC
            """,
            (
                str(chat_id),
                int(project_id),
            ),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:

        logger.error(
            "Failed to get project deals: %s",
            exc
        )

        return []


# ============================================================
# 5.17 — BUILD CRM CONTEXT
# ============================================================

def build_crm_context(
    chat_id: Any
) -> str:
    """
    Build compact investor and deal context for Gemini.
    """

    investors = get_investors(
        chat_id,
        limit=50,
    )

    deals = get_deals(
        chat_id,
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
# 5.18 — ENHANCED FULL AI CONTEXT
# ============================================================

def build_full_ai_context(
    chat_id: Any
) -> str:
    """
    Combine memory, conversation, projects, investors and deals.

    This replaces the lighter context builder from PART 3.
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

    return clean_context_text(
        "\n".join(
            sections
        ).strip(),
        max_length=40000,
    )


# ============================================================
# 5.19 — PART 5 COMPLETION MARKER
# ============================================================

print(
    "GENIOSA 4.0 — PART 5/12 LOADED"
)# ============================================================
# GENIOSA 4.0 — PART 6/12
# Document processing, extraction and document CRM
# ============================================================


def detect_file_type(filename: str) -> str:
    """
    Determines supported document type from file extension.
    """
    name = str(filename or "").strip().lower()

    if "." not in name:
        return "unknown"

    extension = name.rsplit(".", 1)[-1].strip()

    if extension in SUPPORTED_DOCUMENT_TYPES:
        return extension

    return "unknown"


def extract_pdf_text(filepath: str) -> str:
    """
    Extracts text from a PDF file.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {filepath}")

    reader = PdfReader(str(path))
    pages = []

    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            logger.warning(
                "Failed to extract PDF page %s: %s",
                index,
                exc,
            )
            text = ""

        text = str(text).strip()

        if text:
            pages.append(
                f"--- PAGE {index} ---\n{text}"
            )

    return "\n\n".join(pages).strip()


def extract_docx_text(filepath: str) -> str:
    """
    Extracts paragraphs and tables from DOCX.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(f"DOCX file not found: {filepath}")

    document = Document(str(path))
    sections = []

    for paragraph in document.paragraphs:
        text = str(paragraph.text or "").strip()
        if text:
            sections.append(text)

    for table_index, table in enumerate(
        document.tables,
        start=1,
    ):
        rows = []

        for row in table.rows:
            cells = []

            for cell in row.cells:
                value = str(cell.text or "").strip()
                cells.append(value)

            rows.append(" | ".join(cells))

        if rows:
            sections.append(
                f"--- TABLE {table_index} ---\n"
                + "\n".join(rows)
            )

    return "\n\n".join(sections).strip()


def extract_excel_text(filepath: str) -> str:
    """
    Extracts readable worksheet data from XLSX/XLSM.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"Excel file not found: {filepath}"
        )

    workbook = load_workbook(
        filename=str(path),
        read_only=True,
        data_only=False,
    )

    sections = []

    try:
        for worksheet in workbook.worksheets:
            rows = []
            row_count = 0

            for row in worksheet.iter_rows(
                values_only=True
            ):
                row_count += 1

                if row_count > MAX_EXCEL_ROWS_PER_SHEET:
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

            if rows:
                sections.append(
                    f"--- SHEET: {worksheet.title} ---\n"
                    + "\n".join(rows)
                )

    finally:
        workbook.close()

    return "\n\n".join(sections).strip()


def extract_pptx_text(filepath: str) -> str:
    """
    Extracts text from PowerPoint slides.
    """
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(
            f"PPTX file not found: {filepath}"
        )

    presentation = Presentation(str(path))
    sections = []

    for slide_number, slide in enumerate(
        presentation.slides,
        start=1,
    ):
        texts = []

        for shape in slide.shapes:
            try:
                if not hasattr(shape, "text"):
                    continue

                value = str(
                    shape.text or ""
                ).strip()

                if value:
                    texts.append(value)

            except Exception as exc:
                logger.warning(
                    "Failed to read PPTX shape on slide %s: %s",
                    slide_number,
                    exc,
                )

        if texts:
            sections.append(
                f"--- SLIDE {slide_number} ---\n"
                + "\n".join(texts)
            )

    return "\n\n".join(sections).strip()


def extract_txt_text(filepath: str) -> str:
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
            return path.read_text(
                encoding=encoding
            ).strip()
        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error

    return ""


def extract_csv_text(filepath: str) -> str:
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
            return path.read_text(
                encoding=encoding
            ).strip()
        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error

    return ""


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

    detected_type = (
        file_type
        or detect_file_type(path.name)
    )

    detected_type = str(
        detected_type or ""
    ).lower().strip()

    if detected_type == "pdf":
        return extract_pdf_text(str(path))

    if detected_type == "docx":
        return extract_docx_text(str(path))

    if detected_type in {"xlsx", "xlsm"}:
        return extract_excel_text(str(path))

    if detected_type == "pptx":
        return extract_pptx_text(str(path))

    if detected_type == "txt":
        return extract_txt_text(str(path))

    if detected_type == "csv":
        return extract_csv_text(str(path))

    raise ValueError(
        f"Unsupported document type: {detected_type}"
    )


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
    safe_filename = Path(
        str(filename or "document")
    ).name

    detected_type = (
        file_type
        or detect_file_type(safe_filename)
    )

    text_value = (
        str(extracted_text)
        if extracted_text is not None
        else ""
    )

    if len(text_value) > MAX_DOCUMENT_AI_TEXT:
        text_value = text_value[
            :MAX_DOCUMENT_AI_TEXT
        ]

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(project_id)
        except Exception:
            project_id_value = None

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
            return int(result["id"])

    except Exception as exc:
        logger.error(
            "Failed to save document record: %s",
            exc,
        )

    return None


def update_document_analysis(
    chat_id: Any,
    document_id: int,
    analysis: str,
) -> bool:
    """
    Saves AI analysis for a document.
    """
    try:
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
                str(analysis or ""),
                int(document_id),
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


def update_document_project(
    chat_id: Any,
    document_id: int,
    project_id: Optional[int],
) -> bool:
    """
    Links a document to a project.
    """
    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(project_id)
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
                int(document_id),
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


def get_documents(
    chat_id: Any,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Returns documents belonging to a chat.
    """
    try:
        safe_limit = max(
            1,
            min(
                int(limit),
                MAX_DOCUMENT_RECORDS,
            ),
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
            (str(chat_id),),
            fetchall=True,
        )

        return rows or []

    except Exception as exc:
        logger.error(
            "Failed to get documents: %s",
            exc,
        )
        return []


def get_document(
    chat_id: Any,
    document_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Returns one document owned by the chat.
    """
    try:
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
                int(document_id),
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


def delete_document(
    chat_id: Any,
    document_id: int,
) -> bool:
    """
    Deletes one document record.
    """
    try:
        result = db_execute(
            """
            DELETE FROM documents
            WHERE id = %s
              AND chat_id = %s
            RETURNING id
            """,
            (
                int(document_id),
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
        document_id = document.get("id")
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
            if extracted_text.strip()
            else "ტექსტი არ არის"
        )

        analysis_status = (
            "გაანალიზებულია"
            if analysis.strip()
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

    return "\n".join(lines).strip()


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

    extracted_text = (
        document.get("extracted_text")
        or ""
    )

    extracted_text = str(
        extracted_text
    ).strip()

    if len(extracted_text) > MAX_DOCUMENT_AI_TEXT:
        extracted_text = extracted_text[
            :MAX_DOCUMENT_AI_TEXT
        ]

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


def build_document_analysis_prompt(
    document: Dict[str, Any],
    chat_id: Any,
) -> str:
    """
    Builds a structured AI prompt for document analysis.
    """
    document_context = prepare_document_for_ai(
        document
    )

    business_context = build_full_ai_context(
        chat_id
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


def document_text_preview(
    document: Optional[Dict[str, Any]],
    max_length: int = 1500,
) -> str:
    """
    Creates a short preview of extracted document text.
    """
    if not document:
        return "❌ დოკუმენტი ვერ მოიძებნა."

    text_value = str(
        document.get("extracted_text")
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

    safe_length = max(
        100,
        min(int(max_length), 10000),
    )

    if len(clean_value) > safe_length:
        return (
            clean_value[:safe_length].rstrip()
            + "..."
        )

    return clean_value


def document_analysis_summary(
    document: Optional[Dict[str, Any]],
) -> str:
    """
    Returns a concise document analysis result.
    """
    if not document:
        return "❌ დოკუმენტი ვერ მოიძებნა."

    document_id = document.get("id")
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


print("GENIOSA 4.0 — PART 6/12 LOADED")# ============================================================
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

    if not candidates:
        return ""

    parts = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        content = candidate.get("content") or {}
        content_parts = content.get("parts") or []

        for part in content_parts:
            if not isinstance(part, dict):
                continue

            text_value = part.get("text")

            if text_value:
                parts.append(
                    str(text_value).strip()
                )

    return "\n".join(
        part for part in parts if part
    ).strip()


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
                return str(message).strip()

            status = error_data.get("status")

            if status:
                return str(status).strip()

    if response_data:
        return str(response_data)[:2000]

    return "Unknown Gemini API error."


def gemini_generate(
    prompt: str,
    model: Optional[str] = None,
    temperature: float = 0.35,
    max_output_tokens: int = 8192,
) -> str:
    """
    Sends a text prompt to Gemini and returns generated text.
    """
    if not GEMINI_API_KEY:
        return (
            "❌ GEMINI_API_KEY არ არის "
            "კონფიგურირებული."
        )

    prompt_text = str(prompt or "").strip()

    if not prompt_text:
        return (
            "❌ Gemini-სთვის გასაგზავნი "
            "ტექსტი ცარიელია."
        )

    selected_model = (
        model
        or GEMINI_MODEL
        or "gemini-2.5-flash-lite"
    ).strip()

    url = gemini_url(selected_model)

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
            "temperature": float(
                max(0.0, min(1.0, temperature))
            ),
            "maxOutputTokens": int(
                max(
                    256,
                    min(
                        max_output_tokens,
                        32768,
                    ),
                )
            ),
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
            timeout=120,
        )

        try:
            response_data = response.json()
        except Exception:
            response_data = {
                "error": response.text[:4000]
            }

        if response.status_code != 200:
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
            finish_reason = str(
                response_data["candidates"][0]
                .get("finishReason", "")
            )
        except Exception:
            pass

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

    context = build_full_ai_context(
        chat_id
    )

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

    return "\n".join(prompt_parts).strip()


def process_ai_text(
    text: str,
) -> str:
    """
    Cleans AI output before sending it to Telegram.
    """
    value = str(text or "").strip()

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
    Detects simple long-term business facts
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

    if explicit_memory:
        memory_text = user_text

        if len(memory_text) > 2000:
            memory_text = memory_text[:2000].strip()

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

    for memory_text, category, importance in candidates:
        if not memory_text:
            continue

        try:
            save_memory(
                chat_id=chat_id,
                memory=memory_text,
                category=category,
                importance=importance,
            )
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

        if response_text.startswith(
            "❌ Gemini"
        ) or response_text.startswith(
            "❌ AI"
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
    Loads a document, sends it to Gemini,
    and stores the analysis.
    """
    document = get_document(
        chat_id,
        document_id,
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

    prompt = build_document_analysis_prompt(
        document=document,
        chat_id=chat_id,
    )

    analysis = gemini_generate(
        prompt=prompt,
        temperature=0.2,
        max_output_tokens=12000,
    )

    analysis = process_ai_text(
        analysis
    )

    if analysis.startswith("❌"):
        return analysis

    saved = update_document_analysis(
        chat_id=chat_id,
        document_id=document_id,
        analysis=analysis,
    )

    if not saved:
        logger.warning(
            "Document analysis generated but "
            "could not be saved. document_id=%s",
            document_id,
        )

    return analysis


print("GENIOSA 4.0 — PART 7/12 LOADED")# ============================================================
# GENIOSA 4.0 — PART 8/12
# Telegram files, photos and multimodal AI analysis
# ============================================================


def telegram_api_request(
    method: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = TELEGRAM_REQUEST_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    """
    Generic Telegram Bot API request.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "Telegram token is not configured."
        )
        return None

    request_payload = payload or {}

    try:
        response = requests.post(
            telegram_url(method),
            json=request_payload,
            timeout=timeout,
        )

        if response.status_code != 200:
            logger.error(
                "Telegram API HTTP %s for %s: %s",
                response.status_code,
                method,
                response.text[:2000],
            )
            return None

        data = response.json()

        if not data.get("ok"):
            logger.error(
                "Telegram API error for %s: %s",
                method,
                data,
            )
            return None

        return data

    except requests.Timeout:
        logger.error(
            "Telegram API timeout for %s",
            method,
        )
        return None

    except requests.RequestException as exc:
        logger.error(
            "Telegram API request failed for %s: %s",
            method,
            exc,
        )
        return None

    except Exception as exc:
        logger.exception(
            "Unexpected Telegram API error for %s: %s",
            method,
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

    result = data.get("result")

    if not isinstance(result, dict):
        return None

    return result


def telegram_download_file(
    file_id: str,
    destination_dir: Optional[Path] = None,
    filename: Optional[str] = None,
) -> Optional[str]:
    """
    Downloads a Telegram file into local storage.
    """
    file_info = telegram_get_file(
        file_id
    )

    if not file_info:
        return None

    file_path = str(
        file_info.get("file_path") or ""
    ).strip()

    if not file_path:
        logger.error(
            "Telegram returned no file_path."
        )
        return None

    target_dir = (
        destination_dir
        if destination_dir is not None
        else DOWNLOAD_DIR
    )

    target_dir = Path(target_dir)
    target_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    original_name = (
        filename
        or Path(file_path).name
        or f"telegram_{file_id}"
    )

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        str(original_name),
    ).strip("._")

    if not safe_name:
        safe_name = (
            f"telegram_{file_id}"
        )

    timestamp = int(time.time() * 1000)

    destination = (
        target_dir
        / f"{timestamp}_{safe_name}"
    )

    download_url = (
        f"https://api.telegram.org/file/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{file_path}"
    )

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

        with destination.open(
            "wb"
        ) as output_file:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    output_file.write(chunk)

        if not destination.exists():
            return None

        if destination.stat().st_size <= 0:
            try:
                destination.unlink()
            except Exception:
                pass
            return None

        return str(destination)

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
    path = Path(filepath)

    if not path.exists():
        return None

    try:
        data = path.read_bytes()

        if not data:
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
    Sends text + image/file bytes to Gemini.
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

    path = Path(filepath)

    if not path.exists():
        return (
            "❌ ფაილი ვერ მოიძებნა."
        )

    encoded_data = encode_file_base64(
        str(path)
    )

    if not encoded_data:
        return (
            "❌ ფაილის წაკითხვა "
            "ვერ მოხერხდა."
        )

    detected_mime = (
        mime_type
        or guess_mime_type(str(path))
    )

    selected_model = (
        model
        or GEMINI_MODEL
        or "gemini-2.5-flash-lite"
    ).strip()

    url = gemini_url(
        selected_model
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
            "temperature": float(
                max(
                    0.0,
                    min(
                        1.0,
                        temperature,
                    ),
                )
            ),
            "maxOutputTokens": int(
                max(
                    256,
                    min(
                        max_output_tokens,
                        32768,
                    ),
                )
            ),
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
            timeout=120,
        )

        try:
            response_data = response.json()
        except Exception:
            response_data = {
                "error": response.text[:4000]
            }

        if response.status_code != 200:
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

        return (
            "❌ Gemini-მ მულტიმედიურ "
            "ფაილზე პასუხი ვერ დააბრუნა."
        )

    except requests.Timeout:
        logger.error(
            "Gemini multimodal request timed out."
        )

        return (
            "❌ სურათის ანალიზისას "
            "დრო ამოიწურა."
        )

    except requests.RequestException as exc:
        logger.error(
            "Gemini multimodal request failed: %s",
            exc,
        )

        return (
            "❌ სურათის ანალიზისას "
            "კავშირის შეცდომა მოხდა."
        )

    except Exception as exc:
        logger.exception(
            "Unexpected multimodal Gemini error: %s",
            exc,
        )

        return (
            "❌ სურათის ანალიზისას "
            "მოულოდნელი შეცდომა მოხდა."
        )


def analyze_image_with_ai(
    chat_id: Any,
    filepath: str,
    user_caption: str = "",
) -> str:
    """
    Analyzes a Telegram image using Gemini.
    """
    path = Path(filepath)

    if not path.exists():
        return (
            "❌ სურათი ვერ მოიძებნა."
        )

    caption = str(
        user_caption or ""
    ).strip()

    context = build_full_ai_context(
        chat_id
    )

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
{context}
""".strip()

    result = gemini_generate_multimodal(
        prompt=prompt,
        filepath=str(path),
        mime_type=guess_mime_type(
            str(path)
        ),
        temperature=0.2,
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
    path = Path(filepath)

    if not path.exists():
        return (
            None,
            "❌ ფაილი ვერ მოიძებნა.",
        )

    safe_filename = (
        filename
        or path.name
    )

    file_type = detect_file_type(
        safe_filename
    )

    if file_type == "unknown":
        return (
            None,
            "❌ ამ ტიპის დოკუმენტი "
            "ჯერ არ არის მხარდაჭერილი.",
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
        path = Path(filepath)

        if path.exists():
            path.unlink()

    except Exception as exc:
        logger.warning(
            "Could not remove temporary file %s: %s",
            filepath,
            exc,
        )


print("GENIOSA 4.0 — PART 8/12 LOADED")# ============================================================
# GENIOSA 4.0 — PART 9/12
# Financial analysis and investment calculations
# ============================================================


def safe_float(value: Any) -> Optional[float]:
    """
    Safely converts a value to float.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    try:
        if isinstance(value, str):
            cleaned = value.strip()
            cleaned = cleaned.replace(",", "")
            cleaned = cleaned.replace("$", "")
            cleaned = cleaned.replace("€", "")
            cleaned = cleaned.replace("₾", "")

            if not cleaned:
                return None

            return float(cleaned)

        return float(value)

    except (TypeError, ValueError):
        return None


def calculate_revenue_from_area(
    saleable_area: Any,
    sale_price_per_m2: Any,
) -> Optional[float]:
    """
    Calculates revenue from saleable area
    and average selling price per m².
    """
    area = safe_float(saleable_area)
    price = safe_float(sale_price_per_m2)

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
    area = safe_float(construction_area)
    price = safe_float(construction_price_per_m2)

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
    Calculates total project cost from cost components.
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
        value = safe_float(component)

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
    revenue_value = safe_float(revenue)
    cost_value = safe_float(total_cost)

    if revenue_value is None or cost_value is None:
        return None

    return revenue_value - cost_value


def calculate_profit_margin(
    revenue: Any,
    profit: Any,
) -> Optional[float]:
    """
    Calculates profit margin as percentage of revenue.
    """
    revenue_value = safe_float(revenue)
    profit_value = safe_float(profit)

    if revenue_value is None:
        return None

    if revenue_value == 0:
        return None

    return (
        profit_value / revenue_value
    ) * 100


def calculate_roi(
    investment: Any,
    profit: Any,
) -> Optional[float]:
    """
    Calculates ROI based on invested capital.
    """
    investment_value = safe_float(investment)
    profit_value = safe_float(profit)

    if investment_value is None:
        return None

    if investment_value == 0:
        return None

    return (
        profit_value / investment_value
    ) * 100


def calculate_investor_profit(
    net_profit: Any,
    investor_share: Any,
) -> Optional[float]:
    """
    Calculates investor profit from net profit
    and ownership/profit share percentage.
    """
    profit_value = safe_float(net_profit)
    share_value = safe_float(investor_share)

    if profit_value is None or share_value is None:
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
    profit_value = safe_float(net_profit)
    share_value = safe_float(investor_share)

    if profit_value is None or share_value is None:
        return None

    operator_share = 100 - share_value

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
    cost_value = safe_float(total_cost)
    area_value = safe_float(saleable_area)

    if cost_value is None or area_value is None:
        return None

    if area_value <= 0:
        return None

    return cost_value / area_value


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
    cost_value = safe_float(total_cost)
    margin_value = safe_float(
        target_profit_margin
    )

    if cost_value is None or margin_value is None:
        return None

    if margin_value < 0 or margin_value >= 100:
        return None

    return cost_value / (
        1 - margin_value / 100
    )


def calculate_project_sensitivity(
    saleable_area: Any,
    total_cost: Any,
    base_price: Any,
    price_changes: Optional[List[float]] = None,
) -> List[Dict[str, float]]:
    """
    Creates a simple sales-price sensitivity table.
    """
    area = safe_float(saleable_area)
    cost = safe_float(total_cost)
    price = safe_float(base_price)

    if area is None or cost is None or price is None:
        return []

    if area <= 0 or price < 0:
        return []

    changes = (
        price_changes
        if price_changes is not None
        else [-20, -10, 0, 10, 20]
    )

    results = []

    for change in changes:
        try:
            change_value = float(change)
        except (TypeError, ValueError):
            continue

        adjusted_price = (
            price
            * (1 + change_value / 100)
        )

        revenue = area * adjusted_price
        profit = revenue - cost
        margin = (
            (profit / revenue) * 100
            if revenue != 0
            else None
        )

        results.append(
            {
                "price_change": change_value,
                "price_per_m2": adjusted_price,
                "revenue": revenue,
                "profit": profit,
                "margin": (
                    float(margin)
                    if margin is not None
                    else 0.0
                ),
            }
        )

    return results


def analyze_project_financials(
    project: Dict[str, Any],
    sale_price_per_m2: Optional[float] = None,
    investor_share: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Performs a complete financial analysis
    of a project record.
    """
    if not project:
        return {
            "success": False,
            "error": "Project not found.",
        }

    project_name = (
        project.get("name")
        or "Unnamed Project"
    )

    saleable_area = safe_float(
        project.get("saleable_area")
    )

    construction_area = safe_float(
        project.get("construction_area")
    )

    land_cost = safe_float(
        project.get("land_cost")
    ) or 0.0

    construction_cost = safe_float(
        project.get("construction_cost")
    ) or 0.0

    operating_cost = safe_float(
        project.get("operating_cost")
    ) or 0.0

    financing_cost = safe_float(
        project.get("financing_cost")
    ) or 0.0

    other_cost = safe_float(
        project.get("other_cost")
    ) or 0.0

    stored_total_cost = safe_float(
        project.get("total_cost")
    )

    stored_revenue = safe_float(
        project.get("revenue")
    )

    stored_expected_revenue = safe_float(
        project.get("expected_revenue")
    )

    stored_profit = safe_float(
        project.get("net_profit")
    )

    stored_expected_profit = safe_float(
        project.get("expected_profit")
    )

    total_cost = (
        stored_total_cost
        if stored_total_cost is not None
        else calculate_total_cost_from_components(
            land_cost=land_cost,
            construction_cost=construction_cost,
            operating_cost=operating_cost,
            financing_cost=financing_cost,
            other_cost=other_cost,
        )
    )

    revenue = (
        stored_revenue
        if stored_revenue is not None
        else stored_expected_revenue
    )

    selected_price = safe_float(
        sale_price_per_m2
    )

    if (
        selected_price is not None
        and saleable_area is not None
    ):
        revenue = (
            saleable_area
            * selected_price
        )

    if revenue is None:
        revenue = 0.0

    calculated_profit = (
        revenue - total_cost
    )

    profit = (
        stored_profit
        if (
            stored_profit is not None
            and sale_price_per_m2 is None
        )
        else calculated_profit
    )

    margin = calculate_profit_margin(
        revenue,
        profit,
    )

    investor_share_value = safe_float(
        investor_share
    )

    if investor_share_value is None:
        investor_share_value = safe_float(
            project.get("investor_share")
        )

    investor_profit = None
    operator_profit = None

    if investor_share_value is not None:
        investor_profit = calculate_investor_profit(
            profit,
            investor_share_value,
        )

        operator_profit = calculate_operator_profit(
            profit,
            investor_share_value,
        )

    investor_capital = safe_float(
        project.get("investor_capital")
    )

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

    if saleable_area is not None:
        break_even_price = calculate_break_even_price(
            total_cost,
            saleable_area,
        )

    construction_cost_per_m2 = None

    if (
        construction_area is not None
        and construction_area > 0
    ):
        construction_cost_per_m2 = (
            construction_cost
            / construction_area
        )

    revenue_per_saleable_m2 = None

    if (
        saleable_area is not None
        and saleable_area > 0
    ):
        revenue_per_saleable_m2 = (
            revenue
            / saleable_area
        )

    sensitivity = []

    if (
        saleable_area is not None
        and selected_price is not None
    ):
        sensitivity = calculate_project_sensitivity(
            saleable_area=saleable_area,
            total_cost=total_cost,
            base_price=selected_price,
        )

    return {
        "success": True,
        "project_name": project_name,
        "saleable_area": saleable_area,
        "construction_area": construction_area,
        "land_cost": land_cost,
        "construction_cost": construction_cost,
        "operating_cost": operating_cost,
        "financing_cost": financing_cost,
        "other_cost": other_cost,
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
        "investor_share": investor_share_value,
        "investor_capital": investor_capital,
        "investor_profit": investor_profit,
        "investor_roi": investor_roi,
        "operator_profit": operator_profit,
        "stored_expected_revenue": (
            stored_expected_revenue
        ),
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
    if not analysis:
        return (
            "❌ ფინანსური ანალიზი ვერ შესრულდა."
        )

    if not analysis.get("success"):
        return (
            "❌ ფინანსური ანალიზი ვერ შესრულდა.\n"
            f"{analysis.get('error', '')}"
        )

    project_name = (
        analysis.get("project_name")
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
    construction_cost_per_m2 = analysis.get(
        "construction_cost_per_m2"
    )
    sale_price_per_m2 = analysis.get(
        "sale_price_per_m2"
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

    lines = [
        "📊 GENIOSA — ფინანსური ანალიზი",
        "",
        f"🏗️ პროექტი: {project_name}",
        "",
        "💰 ძირითადი მაჩვენებლები:",
        f"• ჯამური ღირებულება: "
        f"{format_money(total_cost)}",
        f"• შემოსავალი: "
        f"{format_money(revenue)}",
        f"• წმინდა მოგება: "
        f"{format_money(profit)}",
    ]

    if margin is not None:
        lines.append(
            f"• მოგების მარჟა: "
            f"{format_number(margin)}%"
        )

    if sale_price_per_m2 is not None:
        lines.append(
            f"• გაყიდვის ფასი: "
            f"{format_money(sale_price_per_m2)}/მ²"
        )

    if break_even_price is not None:
        lines.append(
            f"• Break-even ფასი: "
            f"{format_money(break_even_price)}/მ²"
        )

    if construction_cost_per_m2 is not None:
        lines.append(
            f"• მშენებლობის ღირებულება: "
            f"{format_money(construction_cost_per_m2)}/მ²"
        )

    if investor_share is not None:
        lines.extend([
            "",
            "👤 ინვესტორი:",
            f"• წილი: "
            f"{format_number(investor_share)}%",
        ])

    if investor_capital is not None:
        lines.append(
            f"• ინვესტირებული კაპიტალი: "
            f"{format_money(investor_capital)}"
        )

    if investor_profit is not None:
        lines.append(
            f"• ინვესტორის მოგება: "
            f"{format_money(investor_profit)}"
        )

    if investor_roi is not None:
        lines.append(
            f"• ინვესტორის ROI: "
            f"{format_number(investor_roi)}%"
        )

    return "\n".join(lines)


def save_financial_analysis(
    chat_id: Any,
    analysis: Dict[str, Any],
    project_id: Optional[int] = None,
    analysis_type: str = "project",
) -> Optional[int]:
    """
    Saves financial analysis to database.
    """
    if not analysis:
        return None

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(
                project_id
            )
        except Exception:
            project_id_value = None

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
                str(analysis_type),
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
            return int(result["id"])

    except Exception as exc:
        logger.error(
            "Failed to save financial analysis: %s",
            exc,
        )

    return None


def run_project_financial_analysis(
    chat_id: Any,
    project_id: int,
    sale_price_per_m2: Optional[float] = None,
    investor_share: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Loads a project, performs financial analysis,
    and saves the result.
    """
    project = get_project(
        chat_id,
        project_id,
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

    if analysis.get("success"):
        save_financial_analysis(
            chat_id=chat_id,
            project_id=project_id,
            analysis=analysis,
            analysis_type="project",
        )

    return analysis


print("GENIOSA 4.0 — PART 9/12 LOADED")# ============================================================
# GENIOSA 4.0 — PART 10/12
# File generation: Excel, PowerPoint and asset management
# ============================================================


def format_number(
    value: Any,
    decimals: int = 2,
) -> str:
    """
    Formats a numeric value for human-readable output.
    """
    number = safe_float(value)

    if number is None:
        return "0"

    if decimals < 0:
        decimals = 0

    if decimals == 0:
        return f"{number:,.0f}"

    formatted = f"{number:,.{decimals}f}"

    formatted = formatted.rstrip("0").rstrip(".")

    return formatted


def format_money(
    value: Any,
    currency: str = "$",
    decimals: int = 0,
) -> str:
    """
    Formats money values.
    """
    number = safe_float(value)

    if number is None:
        return f"{currency}0"

    return (
        f"{currency}"
        f"{format_number(number, decimals)}"
    )


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
    safe_filename = Path(
        str(filename or "generated_file")
    ).name

    project_id_value = None

    if project_id is not None:
        try:
            project_id_value = int(project_id)
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
                str(asset_type or "file"),
                safe_filename,
                str(description or ""),
            ),
            fetchone=True,
            commit=True,
        )

        if result:
            return int(result["id"])

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
    try:
        safe_limit = max(
            1,
            min(
                int(limit),
                MAX_GENERATED_ASSETS,
            ),
        )

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
            (str(chat_id),),
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
        asset_id = asset.get("id")
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

    return "\n".join(lines).strip()


def generate_project_excel(
    project: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Generates a professional project financial Excel file.
    """
    if not project:
        return None

    project_id = project.get("id")

    project_name = (
        project.get("name")
        or "Geniosa Project"
    )

    safe_project_name = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(project_name),
    ).strip("_")

    if not safe_project_name:
        safe_project_name = "project"

    timestamp = int(
        time.time()
    )

    filename = (
        f"Geniosa_Project_"
        f"{safe_project_name}_"
        f"{timestamp}.xlsx"
    )

    filepath = (
        GENERATION_DIR
        / filename
    )

    if analysis is None:
        analysis = analyze_project_financials(
            project
        )

    workbook = Workbook()

    summary_sheet = workbook.active
    summary_sheet.title = "Summary"

    summary_rows = [
        ["GENIOSA 4.0 — PROJECT FINANCIAL MODEL"],
        [],
        ["Project", project_name],
        ["Project ID", project_id],
        ["Industry", project.get("industry") or ""],
        ["Location", project.get("location") or ""],
        [],
        ["AREA & DEVELOPMENT"],
        [
            "Land Area (m²)",
            project.get("land_area"),
        ],
        [
            "Saleable Area (m²)",
            project.get("saleable_area"),
        ],
        [
            "Construction Area (m²)",
            project.get("construction_area"),
        ],
        [
            "Total Area (m²)",
            project.get("total_area"),
        ],
        [],
        ["COST STRUCTURE"],
        [
            "Land Cost",
            project.get("land_cost"),
        ],
        [
            "Construction Cost",
            project.get("construction_cost"),
        ],
        [
            "Operating Cost",
            project.get("operating_cost"),
        ],
        [
            "Financing Cost",
            project.get("financing_cost"),
        ],
        [
            "Other Cost",
            project.get("other_cost"),
        ],
        [
            "Total Cost",
            analysis.get("total_cost"),
        ],
        [],
        ["REVENUE & PROFIT"],
        [
            "Revenue",
            analysis.get("revenue"),
        ],
        [
            "Net Profit",
            analysis.get("profit"),
        ],
        [
            "Profit Margin (%)",
            analysis.get("margin"),
        ],
        [
            "Break-even Price / m²",
            analysis.get("break_even_price"),
        ],
        [],
        ["INVESTOR"],
        [
            "Investor Capital",
            analysis.get("investor_capital"),
        ],
        [
            "Investor Share (%)",
            analysis.get("investor_share"),
        ],
        [
            "Investor Profit",
            analysis.get("investor_profit"),
        ],
        [
            "Investor ROI (%)",
            analysis.get("investor_roi"),
        ],
    ]

    for row in summary_rows:
        summary_sheet.append(row)

    summary_sheet.column_dimensions[
        "A"
    ].width = 32

    summary_sheet.column_dimensions[
        "B"
    ].width = 28

    for cell in summary_sheet[1]:
        cell.font = cell.font.copy(
            bold=True,
            size=14,
        )

    for row_number in range(
        1,
        summary_sheet.max_row + 1,
    ):
        first_cell = summary_sheet.cell(
            row=row_number,
            column=1,
        )

        if first_cell.value in {
            "AREA & DEVELOPMENT",
            "COST STRUCTURE",
            "REVENUE & PROFIT",
            "INVESTOR",
        }:
            first_cell.font = first_cell.font.copy(
                bold=True
            )

    for row_number in range(
        1,
        summary_sheet.max_row + 1,
    ):
        value_cell = summary_sheet.cell(
            row=row_number,
            column=2,
        )

        if isinstance(
            value_cell.value,
            (int, float),
        ):
            value_cell.number_format = (
                '#,##0.00'
            )

    sensitivity_sheet = workbook.create_sheet(
        "Sensitivity"
    )

    sensitivity_sheet.append([
        "Price Change (%)",
        "Price / m²",
        "Revenue",
        "Profit",
        "Margin (%)",
    ])

    sensitivity = analysis.get(
        "sensitivity"
    ) or []

    for item in sensitivity:
        sensitivity_sheet.append([
            item.get("price_change"),
            item.get("price_per_m2"),
            item.get("revenue"),
            item.get("profit"),
            item.get("margin"),
        ])

    sensitivity_sheet.column_dimensions[
        "A"
    ].width = 20

    sensitivity_sheet.column_dimensions[
        "B"
    ].width = 20

    sensitivity_sheet.column_dimensions[
        "C"
    ].width = 20

    sensitivity_sheet.column_dimensions[
        "D"
    ].width = 20

    sensitivity_sheet.column_dimensions[
        "E"
    ].width = 18

    assumptions_sheet = workbook.create_sheet(
        "Assumptions"
    )

    assumptions = [
        ["GENIOSA — ASSUMPTIONS"],
        [],
        [
            "Field",
            "Value",
        ],
        [
            "Project Status",
            project.get("status") or "",
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
            project.get("notes") or "",
        ],
    ]

    for row in assumptions:
        assumptions_sheet.append(row)

    assumptions_sheet.column_dimensions[
        "A"
    ].width = 30

    assumptions_sheet.column_dimensions[
        "B"
    ].width = 70

    try:
        workbook.save(
            str(filepath)
        )
    except Exception as exc:
        logger.error(
            "Failed to save Excel file: %s",
            exc,
        )
        return None

    if not filepath.exists():
        return None

    return str(filepath)


def generate_project_presentation(
    project: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Generates a PowerPoint investor presentation.
    """
    if not project:
        return None

    project_id = project.get("id")

    project_name = (
        project.get("name")
        or "Geniosa Project"
    )

    safe_project_name = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(project_name),
    ).strip("_")

    if not safe_project_name:
        safe_project_name = "project"

    timestamp = int(
        time.time()
    )

    filename = (
        f"Geniosa_Investor_"
        f"{safe_project_name}_"
        f"{timestamp}.pptx"
    )

    filepath = (
        GENERATION_DIR
        / filename
    )

    if analysis is None:
        analysis = analyze_project_financials(
            project
        )

    presentation = Presentation()

    title_slide = presentation.slides.add_slide(
        presentation.slide_layouts[0]
    )

    title_slide.shapes.title.text = (
        f"{project_name}"
    )

    subtitle = title_slide.placeholders[1]

    subtitle.text = (
        "GENIOSA 4.0\n"
        "Investor Presentation"
    )

    slide = presentation.slides.add_slide(
        presentation.slide_layouts[1]
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

    slide.placeholders[1].text = overview_text

    slide = presentation.slides.add_slide(
        presentation.slide_layouts[1]
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

    slide.placeholders[1].text = financial_text

    slide = presentation.slides.add_slide(
        presentation.slide_layouts[1]
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

    slide.placeholders[1].text = (
        investment_text
    )

    slide = presentation.slides.add_slide(
        presentation.slide_layouts[1]
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

    slide.placeholders[1].text = (
        risk_text
    )

    slide = presentation.slides.add_slide(
        presentation.slide_layouts[1]
    )

    slide.shapes.title.text = (
        "Project Notes"
    )

    notes = str(
        project.get("notes")
        or "No additional notes."
    )

    slide.placeholders[1].text = notes

    try:
        presentation.save(
            str(filepath)
        )
    except Exception as exc:
        logger.error(
            "Failed to save PowerPoint file: %s",
            exc,
        )
        return None

    if not filepath.exists():
        return None

    return str(filepath)


def generate_project_files(
    chat_id: Any,
    project_id: int,
) -> Dict[str, Any]:
    """
    Generates both Excel and PowerPoint
    files for a project.
    """
    project = get_project(
        chat_id,
        project_id,
    )

    if not project:
        return {
            "success": False,
            "error": "Project not found.",
        }

    analysis = analyze_project_financials(
        project
    )

    excel_path = generate_project_excel(
        project,
        analysis,
    )

    presentation_path = (
        generate_project_presentation(
            project,
            analysis,
        )
    )

    generated = []

    if excel_path:
        asset_id = generated_asset_save(
            chat_id=chat_id,
            project_id=project_id,
            filename=Path(
                excel_path
            ).name,
            asset_type="xlsx",
            description=(
                "Project financial model"
            ),
        )

        generated.append({
            "type": "xlsx",
            "path": excel_path,
            "asset_id": asset_id,
        })

    if presentation_path:
        asset_id = generated_asset_save(
            chat_id=chat_id,
            project_id=project_id,
            filename=Path(
                presentation_path
            ).name,
            asset_type="pptx",
            description=(
                "Investor presentation"
            ),
        )

        generated.append({
            "type": "pptx",
            "path": presentation_path,
            "asset_id": asset_id,
        })

    return {
        "success": bool(generated),
        "project_id": project_id,
        "files": generated,
        "analysis": analysis,
    }


print("GENIOSA 4.0 — PART 10/12 LOADED")# ============================================================
# GENIOSA 4.0 — PART 11/12
# Commands, natural language processing and CRM actions
# ============================================================


def parse_key_value_text(
    text: str,
) -> Dict[str, str]:
    """
    Parses simple key=value or key:value pairs.
    """
    source = str(text or "").strip()

    if not source:
        return {}

    result = {}

    pattern = re.compile(
        r"""
        ([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_\- ]*)
        \s*(?:=|:)\s*
        (?:"([^"]+)"|'([^']+)'|([^,\n;]+))
        """,
        re.VERBOSE,
    )

    for match in pattern.finditer(source):
        key = (
            match.group(1)
            or ""
        ).strip().lower()

        value = (
            match.group(2)
            or match.group(3)
            or match.group(4)
            or ""
        ).strip()

        if key and value:
            result[key] = value

    return result


def first_number(
    text: str,
) -> Optional[float]:
    """
    Returns the first numeric value found in text.
    """
    source = str(text or "")

    match = re.search(
        r"(?<!\d)"
        r"-?\d+(?:[.,]\d+)?"
        r"(?!\d)",
        source,
    )

    if not match:
        return None

    value = match.group(0).replace(
        ",",
        ".",
    )

    try:
        return float(value)
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
    currency/formatting symbols into float.
    """
    if value is None:
        return None

    text_value = str(
        value
    ).strip()

    if not text_value:
        return None

    text_value = text_value.replace(
        "$",
        "",
    )
    text_value = text_value.replace(
        "€",
        "",
    )
    text_value = text_value.replace(
        "₾",
        "",
    )
    text_value = text_value.replace(
        " ",
        "",
    )

    if (
        "," in text_value
        and "." in text_value
    ):
        if text_value.rfind(",") > text_value.rfind("."):
            text_value = text_value.replace(
                ".",
                "",
            )
            text_value = text_value.replace(
                ",",
                ".",
            )
        else:
            text_value = text_value.replace(
                ",",
                "",
            )
    elif "," in text_value:
        text_value = text_value.replace(
            ",",
            ".",
        )

    try:
        return float(text_value)

    except Exception:
        return None


def create_project_from_text(
    chat_id: Any,
    text: str,
) -> Optional[int]:
    """
    Creates a project from a natural-language message.

    Supported examples:
        პროექტი: NIKKEA 12
        სახელი: NIKKEA 12
        ინდუსტრია: hotel
        ლოკაცია: Kutaisi
        მიწა: 3070
        გასაყიდი ფართი: 10854
        მშენებლობა: 2150000
    """
    source = str(
        text or ""
    ).strip()

    if not source:
        return None

    pairs = parse_key_value_text(
        source
    )

    name = (
        pairs.get("name")
        or pairs.get("project")
        or pairs.get("პროექტი")
        or pairs.get("სახელი")
    )

    if not name:
        lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip()
        ]

        if lines:
            first_line = lines[0]

            cleaned = re.sub(
                r"^(?:პროექტი|project)\s*[:\-]?\s*",
                "",
                first_line,
                flags=re.IGNORECASE,
            ).strip()

            if cleaned:
                name = cleaned

    if not name:
        return None

    industry = (
        pairs.get("industry")
        or pairs.get("sector")
        or pairs.get("ინდუსტრია")
        or pairs.get("სექტორი")
    )

    location = (
        pairs.get("location")
        or pairs.get("ლოკაცია")
        or pairs.get("ადგილი")
    )

    description = (
        pairs.get("description")
        or pairs.get("აღწერა")
    )

    land_area = normalize_numeric_text(
        pairs.get("land_area")
        or pairs.get("land")
        or pairs.get("მიწის ფართობი")
        or pairs.get("მიწა")
    )

    saleable_area = normalize_numeric_text(
        pairs.get("saleable_area")
        or pairs.get("saleable")
        or pairs.get("გასაყიდი ფართობი")
        or pairs.get("გასაყიდი")
    )

    construction_area = normalize_numeric_text(
        pairs.get("construction_area")
        or pairs.get("construction")
        or pairs.get("სამშენებლო ფართობი")
        or pairs.get("მშენებლობა")
    )

    total_area = normalize_numeric_text(
        pairs.get("total_area")
        or pairs.get("total")
        or pairs.get("საერთო ფართობი")
        or pairs.get("სრული ფართობი")
    )

    land_cost = normalize_numeric_text(
        pairs.get("land_cost")
        or pairs.get("land price")
        or pairs.get("მიწის ღირებულება")
        or pairs.get("მიწის ფასი")
    )

    construction_cost = normalize_numeric_text(
        pairs.get("construction_cost")
        or pairs.get("construction price")
        or pairs.get("მშენებლობის ღირებულება")
        or pairs.get("მშენებლობის ფასი")
    )

    operating_cost = normalize_numeric_text(
        pairs.get("operating_cost")
        or pairs.get("ოპერაციული ხარჯი")
    )

    financing_cost = normalize_numeric_text(
        pairs.get("financing_cost")
        or pairs.get("ფინანსირების ხარჯი")
    )

    other_cost = normalize_numeric_text(
        pairs.get("other_cost")
        or pairs.get("სხვა ხარჯი")
    )

    expected_revenue = normalize_numeric_text(
        pairs.get("expected_revenue")
        or pairs.get("revenue")
        or pairs.get("შემოსავალი")
        or pairs.get("მოსალოდნელი შემოსავალი")
    )

    expected_profit = normalize_numeric_text(
        pairs.get("expected_profit")
        or pairs.get("profit")
        or pairs.get("მოგება")
        or pairs.get("მოსალოდნელი მოგება")
    )

    investor_capital = normalize_numeric_text(
        pairs.get("investor_capital")
        or pairs.get("investment")
        or pairs.get("ინვესტიცია")
        or pairs.get("ინვესტორის კაპიტალი")
    )

    investor_profit = normalize_numeric_text(
        pairs.get("investor_profit")
        or pairs.get("ინვესტორის მოგება")
    )

    investor_share = normalize_numeric_text(
        pairs.get("investor_share")
        or pairs.get("share")
        or pairs.get("წილი")
        or pairs.get("ინვესტორის წილი")
    )

    notes = (
        pairs.get("notes")
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


def create_investor_from_text(
    chat_id: Any,
    text: str,
) -> Optional[int]:
    """
    Creates an investor from natural-language key/value text.
    """
    source = str(
        text or ""
    ).strip()

    if not source:
        return None

    pairs = parse_key_value_text(
        source
    )

    name = (
        pairs.get("name")
        or pairs.get("investor")
        or pairs.get("ინვესტორი")
        or pairs.get("სახელი")
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
        or pairs.get("capacity")
        or pairs.get("საინვესტიციო შესაძლებლობა")
    )

    preferred_sector = (
        pairs.get("preferred_sector")
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


def looks_like_project_request(
    text: str,
) -> bool:
    """
    Detects whether a message appears to describe
    creation of a project.
    """
    source = str(
        text or ""
    ).strip().lower()

    if not source:
        return False

    keywords = [
        "პროექტის დამატება",
        "პროექტი შექმენი",
        "შექმენი პროექტი",
        "ახალი პროექტი",
        "create project",
        "new project",
        "add project",
        "project:",
    ]

    return any(
        keyword in source
        for keyword in keywords
    )


def looks_like_investor_request(
    text: str,
) -> bool:
    """
    Detects whether a message appears to describe
    creation of an investor.
    """
    source = str(
        text or ""
    ).strip().lower()

    if not source:
        return False

    keywords = [
        "ინვესტორის დამატება",
        "ინვესტორი დაამატე",
        "შექმენი ინვესტორი",
        "ახალი ინვესტორი",
        "create investor",
        "new investor",
        "add investor",
        "investor:",
    ]

    return any(
        keyword in source
        for keyword in keywords
    )


def status_text(
    chat_id: Any,
) -> str:
    """
    Returns a detailed Geniosa status.
    """
    env = validate_environment()
    database_ok = database_is_available()

    return (
        "🟢 GENIOSA 4.0 STATUS\n\n"
        f"Telegram: "
        f"{'OK' if env['telegram'] else 'ERROR'}\n"
        f"Gemini: "
        f"{'OK' if env['gemini'] else 'ERROR'}\n"
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

    if not value.isdigit():
        project = find_project_by_name(
            chat_id,
            value,
        )

        if not project:
            return (
                "❌ პროექტი ვერ მოიძებნა."
            )

        project_id = project.get("id")

    else:
        project_id = int(value)

    analysis = run_project_financial_analysis(
        chat_id=chat_id,
        project_id=project_id,
    )

    return financial_analysis_text(
        analysis
    )


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

    asset_id = int(value)

    assets = get_generated_assets(
        chat_id,
        limit=MAX_GENERATED_ASSETS,
    )

    for asset in assets:
        if int(asset.get("id")) == asset_id:
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

            return (
                f"📦 ფაილი #{asset_id}\n"
                f"📎 {filename}\n"
                f"📁 ტიპი: {asset_type}\n"
                f"📝 {description}"
            )

    return (
        "❌ გენერირებული ფაილი ვერ მოიძებნა."
    )


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

    parts = source.split(
        maxsplit=1
    )

    command = parts[0].lower()
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


def handle_text_message(
    chat_id: Any,
    text: str,
) -> str:
    """
    Main natural-language message handler.
    """
    message_text = str(
        text or ""
    ).strip()

    if not message_text:
        return (
            "❌ ცარიელი შეტყობინება."
        )

    if message_text.startswith("/"):
        return handle_command(
            chat_id,
            message_text,
        )

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

    if looks_like_project_request(
        message_text
    ):
        project_id = create_project_from_text(
            chat_id,
            message_text,
        )

        if project_id:
            project = get_project(
                chat_id,
                project_id,
            )

            response = (
                "✅ პროექტი დაემატა Geniosa-ს CRM-ში.\n\n"
                + project_summary(
                    project
                )
            )

            save_message(
                chat_id=chat_id,
                role="assistant",
                text=response,
            )

            return response

    if looks_like_investor_request(
        message_text
    ):
        investor_id = create_investor_from_text(
            chat_id,
            message_text,
        )

        if investor_id:
            investor = get_investor(
                chat_id,
                investor_id,
            )

            response = (
                "✅ ინვესტორი დაემატა Geniosa-ს CRM-ში.\n\n"
                + format_investor(
                    investor
                )
            )

            save_message(
                chat_id=chat_id,
                role="assistant",
                text=response,
            )

            return response

    response = ask_geniosa(
        chat_id=chat_id,
        user_message=message_text,
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


print("GENIOSA 4.0 — PART 11/12 LOADED")

# ============================================================
# GENIOSA 4.0 — PART 12/12
# Telegram polling, file sending, startup and shutdown
# ============================================================

from pathlib import Path


# ------------------------------------------------------------
# Telegram URL compatibility helper
# ------------------------------------------------------------

def telegram_url(
    method: str,
) -> str:
    """
    Compatibility wrapper for Telegram API URL generation.

    PART 1 defines telegram_api_url().
    Older functions may still call telegram_url().
    """

    return telegram_api_url(
        method
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

    path = Path(
        filepath
    )

    if not path.exists():
        logger.error(
            "File does not exist: %s",
            filepath,
        )
        return False

    if not TELEGRAM_BOT_TOKEN:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not configured."
        )
        return False

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

        data = response.json()

        if not data.get("ok"):
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
    """

    result: List[str] = []

    files = generate_project_files(
        chat_id=chat_id,
        project_id=project_id,
    )

    if not files:
        return result

    for filepath in files:

        path = Path(
            filepath
        )

        if not path.exists():
            continue

        if path.suffix.lower() == ".xlsx":

            caption = (
                "📊 Geniosa — Financial Model / Excel"
            )

        elif path.suffix.lower() == ".pptx":

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
            result.append(
                str(path)
            )

    return result


# ------------------------------------------------------------
# Help message
# ------------------------------------------------------------

def send_help_message(
    chat_id: Any,
) -> None:

    send_long_message(
        chat_id,
        handle_command(
            chat_id,
            "/help",
        ),
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

    params = {
        "timeout": TELEGRAM_POLL_TIMEOUT,
        "allowed_updates": json.dumps(
            [
                "message",
            ]
        ),
    }

    if offset is not None:
        params["offset"] = offset

    data = telegram_api_request(
        "getUpdates",
        params,
    )

    if not data:
        return []

    return data.get(
        "result",
        [],
    )


# ------------------------------------------------------------
# Telegram webhook
# ------------------------------------------------------------

def telegram_delete_webhook() -> bool:
    """
    Removes an existing Telegram webhook so polling can work.
    """

    data = telegram_api_request(
        "deleteWebhook",
        {
            "drop_pending_updates": False,
        },
    )

    success = bool(
        data
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

    The function keeps retrying while another Geniosa
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

            cursor.close()

            acquired = bool(
                result
                and result[0]
            )

            if acquired:

                POLLING_LOCK_CONNECTION = connection
                POLLING_LOCK_ACQUIRED = True

                logger.info(
                    "Telegram polling advisory lock acquired."
                )

                return True

            connection.close()
            connection = None

            logger.warning(
                "Another Geniosa instance already owns "
                "the Telegram polling lock. Retrying..."
            )

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

    try:

        cursor = connection.cursor()

        cursor.execute(
            "SELECT pg_advisory_unlock(%s)",
            (
                POLLING_LOCK_ID,
            ),
        )

        cursor.close()

    except Exception as exc:

        logger.warning(
            "Could not release polling lock cleanly: %s",
            exc,
        )

    finally:

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

    filepath = telegram_download_file(
        file_id
    )

    if not filepath:
        return (
            "❌ ფაილის ჩამოტვირთვა ვერ მოხერხდა."
        )

    try:

        extracted_text = extract_file_text(
            filepath
        )

        if not extracted_text.strip():
            return (
                "⚠️ ფაილიდან ტექსტის ამოღება ვერ მოხერხდა."
            )

        record_id = save_document_record(
            chat_id=chat_id,
            filename=filename,
            file_type=detect_file_type(
                filename
            ),
            extracted_text=extracted_text,
        )

        if not record_id:
            logger.warning(
                "Document record was not created."
            )

        analysis = analyze_document_with_ai(
            chat_id=chat_id,
            filename=filename,
            extracted_text=extracted_text,
        )

        if record_id:

            update_document_analysis(
                chat_id=chat_id,
                document_id=record_id,
                analysis=analysis,
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

        cleanup_temp_file(
            filepath
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

    file_id = photo.get(
        "file_id"
    )

    if not file_id:
        return (
            "❌ ფოტოს ID ვერ მივიღე."
        )

    filepath = telegram_download_file(
        file_id
    )

    if not filepath:
        return (
            "❌ ფოტოს ჩამოტვირთვა ვერ მოხერხდა."
        )

    try:

        result = analyze_image_with_ai(
            chat_id=chat_id,
            filepath=filepath,
            user_caption=caption,
        )

        return result

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

        cleanup_temp_file(
            filepath
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

    chat = message.get(
        "chat"
    ) or {}

    chat_id = chat.get(
        "id"
    )

    if chat_id is None:
        return

    user = message.get(
        "from"
    ) or {}

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

    if not user_allowed(
        chat_id
    ):

        send_message(
            chat_id,
            "⛔ წვდომა შეზღუდულია.",
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

        if document:

            response = process_telegram_document(
                chat_id,
                document,
            )

            send_long_message(
                chat_id,
                response,
            )

            return

        if photos:

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

        if text:

            response = handle_text_message(
                chat_id,
                text,
            )

            send_long_message(
                chat_id,
                response,
            )

            return

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

        send_message(
            chat_id,
            (
                "❌ დამუშავებისას მოხდა ტექნიკური "
                "შეცდომა. სცადე ხელახლა."
            ),
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

    message = update.get(
        "message"
    )

    if not message:
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

    The process waits for the PostgreSQL advisory lock
    instead of exiting when another deployment temporarily
    owns it.
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

                for update in updates:

                    if POLLING_STOP.is_set():
                        break

                    update_id = update.get(
                        "update_id"
                    )

                    if update_id is not None:

                        TELEGRAM_OFFSET = (
                            int(update_id) + 1
                        )

                        LAST_UPDATE_ID = (
                            TELEGRAM_OFFSET
                        )

                    process_telegram_update(
                        update
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

    if POLLING_THREAD is not None:

        if POLLING_THREAD.is_alive():

            logger.info(
                "Telegram polling is already running."
            )

            return True

    with POLLING_THREAD_LOCK:

        if POLLING_THREAD is not None:

            if POLLING_THREAD.is_alive():
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
    Stops Telegram polling.
    """

    global POLLING_THREAD

    POLLING_STOP.set()

    thread = POLLING_THREAD

    if thread is not None:

        if thread.is_alive():

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

    environment = validate_environment()

    if not environment["database"]:

        logger.warning(
            "DATABASE_URL is not configured."
        )

    else:

        try:

            ensure_database_ready()

            logger.info(
                "PostgreSQL database initialized."
            )

        except Exception as exc:

            logger.exception(
                "Database initialization failed: %s",
                exc,
            )

    if not environment["telegram"]:

        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    if not environment["gemini"]:

        logger.warning(
            "GEMINI_API_KEY is not configured."
        )

    logger.info(
        "Geniosa initialization completed."
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

    stop_telegram_polling()

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

    if TELEGRAM_BOT_TOKEN:

        if DATABASE_URL:

            start_telegram_polling()

        else:

            logger.warning(
                "Telegram polling was not started because "
                "DATABASE_URL is missing."
            )

    else:

        logger.warning(
            "Telegram polling was not started because "
            "TELEGRAM_BOT_TOKEN is missing."
        )

    yield

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
