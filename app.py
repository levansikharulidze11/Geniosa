import os

import re

import json

import time

import base64

import logging

import threading

from pathlib import Path

from contextlib import asynccontextmanager

from datetime import datetime

import requests

import psycopg2

from psycopg2.extras import RealDictCursor

from fastapi import FastAPI

from openpyxl import load_workbook, Workbook

from pptx import Presentation

from pypdf import PdfReader

from docx import Document

# ============================================================

# GENIOSA 4.0

# UNIVERSAL BUSINESS & INVESTMENT INTELLIGENCE PLATFORM

# ============================================================

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s %(levelname)s %(message)s"

)

logger = logging.getLogger("geniosa")

# ============================================================

# ENVIRONMENT VARIABLES

# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(

    "TELEGRAM_BOT_TOKEN",

    ""

).strip()

GEMINI_API_KEY = os.getenv(

    "GEMINI_API_KEY",

    ""

).strip()

DATABASE_URL = os.getenv(

    "DATABASE_URL",

    ""

).strip()

OWNER_ID = os.getenv(

    "GENIOSA_OWNER_ID",

    ""

).strip()

GEMINI_MODEL = os.getenv(

    "GEMINI_MODEL",

    "gemini-2.5-flash-lite"

).strip()

# ============================================================

# REQUIRED ENVIRONMENT CHECK

# ============================================================

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError(

        "TELEGRAM_BOT_TOKEN is missing"

    )

if not GEMINI_API_KEY:

    raise RuntimeError(

        "GEMINI_API_KEY is missing"

    )

if not DATABASE_URL:

    raise RuntimeError(

        "DATABASE_URL is missing"

    )

# ============================================================

# TELEGRAM API

# ============================================================

TELEGRAM_API = (

    f"https://api.telegram.org/bot"

    f"{TELEGRAM_BOT_TOKEN}"

)

def telegram_url(method):

    return (

        f"{TELEGRAM_API}/{method}"

    )

# ============================================================

# GEMINI API

# ============================================================

def gemini_url(model=None):

    selected_model = (

        model or GEMINI_MODEL

    ).strip()

    return (

        "https://generativelanguage.googleapis.com/"

        f"v1beta/models/{selected_model}:generateContent"

    )

# ============================================================

# FASTAPI APPLICATION

# ============================================================

app = FastAPI(

    title="Geniosa",

    version="4.0"

)

# ============================================================

# FILE STORAGE

# ============================================================

DOWNLOAD_DIR = Path(

    "/tmp/geniosa"

)

DOWNLOAD_DIR.mkdir(

    parents=True,

    exist_ok=True

)

GENERATION_DIR = (

    DOWNLOAD_DIR / "generated"

)

GENERATION_DIR.mkdir(

    parents=True,

    exist_ok=True

)

# ============================================================

# TELEGRAM MESSAGE LIMIT

# ============================================================

MAX_TELEGRAM_MESSAGE = 3900

# ============================================================

# GLOBAL TELEGRAM POLLING STATE

# ============================================================

POLLING_THREAD = None

POLLING_STOP = threading.Event()

POLLING_THREAD_LOCK = threading.Lock()

TELEGRAM_OFFSET = None

# PostgreSQL advisory-lock connection.

#

# This is important on Render:

# if two application processes/instances accidentally

# start, only one of them will be allowed to poll Telegram.

POLLING_LOCK_CONN = None

POLLING_LOCK_ACQUIRED = False

# ============================================================

# SECURITY

# ============================================================

def user_allowed(chat_id):

    """

    If GENIOSA_OWNER_ID is configured,

    only that Telegram user can use Geniosa.

    If GENIOSA_OWNER_ID is empty,

    the bot remains open.

    """

    if not OWNER_ID:

        return True

    return str(chat_id) == str(OWNER_ID)

# ============================================================

# DATABASE CONNECTION

# ============================================================

def db():

    """

    Creates a PostgreSQL connection.

    DATABASE_URL is supplied by Render PostgreSQL.

    """

    return psycopg2.connect(

        DATABASE_URL,

        cursor_factory=RealDictCursor,

        connect_timeout=15

    )

# ============================================================

# DATABASE INITIALIZATION

# ============================================================

def init_db():

    """

    Creates Geniosa tables.

    Also performs lightweight schema migration for

    older Geniosa database versions.

    """

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        # ----------------------------------------------------

        # MESSAGES

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS messages (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                role TEXT NOT NULL,

                text TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # BUSINESS MEMORY

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS business_memory (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                memory TEXT NOT NULL,

                category TEXT DEFAULT 'general',

                importance INTEGER DEFAULT 5,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # PROJECTS

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS projects (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                name TEXT NOT NULL,

                industry TEXT DEFAULT 'other',

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

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # DOCUMENTS

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS documents (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                project_id INTEGER,

                filename TEXT,

                file_type TEXT,

                extracted_text TEXT,

                analysis TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # INVESTORS

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS investors (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                name TEXT NOT NULL,

                company TEXT,

                country TEXT,

                contact TEXT,

                investment_capacity DOUBLE PRECISION,

                preferred_sector TEXT,

                status TEXT DEFAULT 'new',

                notes TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # DEALS / CRM

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS deals (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                project_id INTEGER,

                investor_id INTEGER,

                stage TEXT DEFAULT 'new',

                proposed_amount DOUBLE PRECISION,

                proposed_share DOUBLE PRECISION,

                valuation DOUBLE PRECISION,

                notes TEXT,

                next_step TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # RESEARCH

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS research (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                project_id INTEGER,

                query TEXT,

                result TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # FINANCIAL ANALYSES

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS financial_analyses (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                project_id INTEGER,

                analysis_type TEXT,

                input_data TEXT,

                result_data TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # GENERATED ASSETS

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS generated_assets (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                project_id INTEGER,

                asset_type TEXT,

                filename TEXT,

                description TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # SECURITIES

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS securities (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                symbol TEXT,

                name TEXT,

                asset_type TEXT,

                exchange TEXT,

                currency TEXT,

                quantity DOUBLE PRECISION,

                average_price DOUBLE PRECISION,

                notes TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # CRYPTO ASSETS

        # ----------------------------------------------------

        cur.execute(

            """

            CREATE TABLE IF NOT EXISTS crypto_assets (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                symbol TEXT,

                name TEXT,

                quantity DOUBLE PRECISION,

                average_price DOUBLE PRECISION,

                wallet TEXT,

                notes TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ====================================================

        # LIGHTWEIGHT MIGRATION

        # ====================================================

        #

        # Older versions of Geniosa used different project

        # field names. We add the newer fields if they do not

        # already exist.

        #

        # This prevents an existing Render database from

        # breaking when the new app.py is deployed.

        # ====================================================

        project_columns = {

            "land_cost": "DOUBLE PRECISION",

            "construction_cost": "DOUBLE PRECISION",

            "operating_cost": "DOUBLE PRECISION",

            "financing_cost": "DOUBLE PRECISION",

            "other_cost": "DOUBLE PRECISION",

            "expected_revenue": "DOUBLE PRECISION",

            "expected_profit": "DOUBLE PRECISION",

            "notes": "TEXT"

        }

        for column, data_type in project_columns.items():

            cur.execute(

                f"""

                ALTER TABLE projects

                ADD COLUMN IF NOT EXISTS

                {column} {data_type}

                """

            )

        # ----------------------------------------------------

        # DOCUMENT MIGRATION

        # ----------------------------------------------------

        document_columns = {

            "analysis": "TEXT",

            "updated_at": (

                "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"

            )

        }

        for column, data_type in document_columns.items():

            cur.execute(

                f"""

                ALTER TABLE documents

                ADD COLUMN IF NOT EXISTS

                {column} {data_type}

                """

            )

        # ----------------------------------------------------

        # INVESTOR MIGRATION

        # ----------------------------------------------------

        investor_columns = {

            "contact": "TEXT",

            "investment_capacity": (

                "DOUBLE PRECISION"

            ),

            "preferred_sector": "TEXT",

            "status": "TEXT DEFAULT 'new'",

            "notes": "TEXT",

            "updated_at": (

                "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"

            )

        }

        for column, data_type in investor_columns.items():

            cur.execute(

                f"""

                ALTER TABLE investors

                ADD COLUMN IF NOT EXISTS

                {column} {data_type}

                """

            )

        # ----------------------------------------------------

        # DEAL MIGRATION

        # ----------------------------------------------------

        deal_columns = {

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

            "updated_at": (

                "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"

            )

        }

        for column, data_type in deal_columns.items():

            cur.execute(

                f"""

                ALTER TABLE deals

                ADD COLUMN IF NOT EXISTS

                {column} {data_type}

                """

            )

        # ----------------------------------------------------

        # COMMIT

        # ----------------------------------------------------

        conn.commit()

        logger.info(

            "Geniosa database initialized successfully."

        )

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "Database initialization error"

        )

        raise

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# DATABASE TEST

# ============================================================

def database_is_available():

    """

    Returns True if PostgreSQL is reachable.

    """

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            "SELECT 1 AS ok"

        )

        row = cur.fetchone()

        return bool(

            row and row.get("ok") == 1

        )

    except Exception:

        logger.exception(

            "Database availability check failed"

        )

        return False

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# TELEGRAM MESSAGE SENDER

# ============================================================

def send_message(chat_id, text):

    """

    Sends a normal Telegram message.

    """

    if not text:

        return None

    try:

        response = requests.post(

            telegram_url("sendMessage"),

            json={

                "chat_id": chat_id,

                "text": str(text)

            },

            timeout=30

        )

        try:

            data = response.json()

        except Exception:

            data = None

        if response.status_code != 200:

            logger.error(

                "Telegram sendMessage HTTP %s: %s",

                response.status_code,

                response.text[:1000]

            )

            return data

        if isinstance(data, dict):

            if not data.get("ok", False):

                logger.error(

                    "Telegram sendMessage API error: %s",

                    str(data)[:1000]

                )

        return data

    except requests.RequestException as exc:

        logger.error(

            "Telegram sendMessage network error: %s",

            exc

        )

        return None

    except Exception:

        logger.exception(

            "send_message error"

        )

        return None

# ============================================================

# LONG MESSAGE SENDER

# ============================================================

def send_long_message(chat_id, text):

    """

    Telegram has a message length limit.

    This function automatically splits long responses.

    """

    if not text:

        return

    text = str(text)

    if len(text) <= MAX_TELEGRAM_MESSAGE:

        send_message(

            chat_id,

            text

        )

        return

    start = 0

    while start < len(text):

        end = min(

            start + MAX_TELEGRAM_MESSAGE,

            len(text)

        )

        chunk = text[start:end]

        if end < len(text):

            split_at = max(

                chunk.rfind("\n"),

                chunk.rfind(" ")

            )

            if split_at > 500:

                end = start + split_at

                chunk = text[start:end]

        send_message(

            chat_id,

            chunk

        )

        start = end

# ============================================================

# BASIC ROOT ENDPOINT

# ============================================================

@app.get("/")

def root():

    return {

        "service": "Geniosa",

        "version": "4.0",

        "status": "online"

    }

# ============================================================

# BASIC STATUS ENDPOINT

# ============================================================

@app.get("/status")

def status():

    database_ok = database_is_available()

    return {

        "service": "Geniosa",

        "version": "4.0",

        "status": (

            "online"

            if database_ok

            else "degraded"

        ),

        "telegram": bool(

            TELEGRAM_BOT_TOKEN

        ),

        "gemini": bool(

            GEMINI_API_KEY

        ),

        "database": database_ok

    }

# ============================================================

# HEALTH ENDPOINT

# ============================================================

@app.get("/health")

def health():

    database_ok = database_is_available()

    return {

        "service": "Geniosa",

        "version": "4.0",

        "status": (

            "healthy"

            if database_ok

            else "degraded"

        ),

        "database": database_ok,

        "telegram": bool(

            TELEGRAM_BOT_TOKEN

        ),

        "gemini": bool(

            GEMINI_API_KEY

        )

    }

# ============================================================

# PART 1 COMPLETE

# ============================================================

print(

    "GENIOSA 4.0 — PART 1/10 LOADED"# ============================================================

# PART 2/10

# MEMORY + PROJECTS + BASIC DATABASE OPERATIONS

# ============================================================

# ============================================================

# GENERIC DATABASE EXECUTOR

# ============================================================

def db_execute(

    query,

    params=None,

    fetchone=False,

    fetchall=False,

    commit=False

):

    """

    Safe helper for PostgreSQL queries.

    """

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            query,

            params or ()

        )

        result = None

        if fetchone:

            result = cur.fetchone()

        elif fetchall:

            result = cur.fetchall()

        if commit:

            conn.commit()

        return result

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "Database query failed"

        )

        raise

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# SAVE MESSAGE

# ============================================================

def save_message(

    chat_id,

    role,

    text

):

    """

    Saves a Telegram conversation message.

    """

    if not text:

        return

    try:

        db_execute(

            """

            INSERT INTO messages

            (

                chat_id,

                role,

                text

            )

            VALUES

            (

                %s,

                %s,

                %s

            )

            """,

            (

                int(chat_id),

                str(role),

                str(text)

            ),

            commit=True

        )

    except Exception:

        logger.exception(

            "save_message failed"

        )

# ============================================================

# GET RECENT MESSAGES

# ============================================================

def get_recent_messages(

    chat_id,

    limit=20

):

    """

    Returns recent conversation history.

    """

    try:

        rows = db_execute(

            """

            SELECT

                role,

                text,

                created_at

            FROM messages

            WHERE chat_id = %s

            ORDER BY id DESC

            LIMIT %s

            """,

            (

                int(chat_id),

                int(limit)

            ),

            fetchall=True

        )

        if not rows:

            return []

        return list(

            reversed(rows)

        )

    except Exception:

        logger.exception(

            "get_recent_messages failed"

        )

        return []

# ============================================================

# FORMAT CONVERSATION HISTORY

# ============================================================

def format_conversation_history(

    chat_id,

    limit=20

):

    """

    Converts recent messages into AI-readable context.

    """

    rows = get_recent_messages(

        chat_id,

        limit

    )

    if not rows:

        return ""

    lines = []

    for row in rows:

        role = str(

            row.get("role", "")

        ).strip()

        message = str(

            row.get("text", "")

        ).strip()

        if not message:

            continue

        if role == "user":

            prefix = "USER"

        elif role in (

            "assistant",

            "model"

        ):

            prefix = "GENIOSA"

        else:

            prefix = role.upper()

        lines.append(

            f"{prefix}: {message}"

        )

    return "\n".join(lines)

# ============================================================

# SAVE BUSINESS MEMORY

# ============================================================

def save_memory(

    chat_id,

    memory,

    category="general",

    importance=5

):

    """

    Stores a persistent business memory.

    Examples:

    - company information

    - investor preferences

    - project decisions

    - financial assumptions

    """

    if not memory:

        return False

    memory = str(

        memory

    ).strip()

    if not memory:

        return False

    try:

        db_execute(

            """

            INSERT INTO business_memory

            (

                chat_id,

                memory,

                category,

                importance

            )

            VALUES

            (

                %s,

                %s,

                %s,

                %s

            )

            """,

            (

                int(chat_id),

                memory,

                str(category),

                int(importance)

            ),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "save_memory failed"

        )

        return False

# ============================================================

# GET BUSINESS MEMORIES

# ============================================================

def get_memories(

    chat_id,

    limit=30

):

    """

    Returns important persistent memories.

    """

    try:

        rows = db_execute(

            """

            SELECT

                id,

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

            LIMIT %s

            """,

            (

                int(chat_id),

                int(limit)

            ),

            fetchall=True

        )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_memories failed"

        )

        return []

# ============================================================

# BUILD MEMORY CONTEXT

# ============================================================

def build_memory_context(

    chat_id,

    limit=30

):

    """

    Converts persistent business memories into

    context for Gemini.

    """

    memories = get_memories(

        chat_id,

        limit

    )

    if not memories:

        return ""

    lines = []

    for item in memories:

        memory = str(

            item.get("memory", "")

        ).strip()

        if not memory:

            continue

        category = str(

            item.get(

                "category",

                "general"

            )

        ).strip()

        lines.append(

            f"- [{category}] {memory}"

        )

    if not lines:

        return ""

    return (

        "PERSISTENT BUSINESS MEMORY:\n"

        + "\n".join(lines)

    )

# ============================================================

# DELETE MEMORY

# ============================================================

def delete_memory(

    chat_id,

    memory_id

):

    """

    Deletes one memory belonging to the user.

    """

    try:

        db_execute(

            """

            DELETE FROM business_memory

            WHERE

                id = %s

                AND chat_id = %s

            """,

            (

                int(memory_id),

                int(chat_id)

            ),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "delete_memory failed"

        )

        return False

# ============================================================

# CREATE PROJECT

# ============================================================

def create_project(

    chat_id,

    name,

    industry="other",

    location=None,

    description=None,

    land_area=None,

    saleable_area=None,

    construction_area=None,

    total_area=None,

    land_cost=None,

    construction_cost=None,

    operating_cost=None,

    financing_cost=None,

    other_cost=None,

    total_cost=None,

    revenue=None,

    expected_revenue=None,

    net_profit=None,

    expected_profit=None,

    investor_capital=None,

    investor_profit=None,

    investor_share=None,

    notes=None,

    status="active"

):

    """

    Creates a project record.

    """

    try:

        row = db_execute(

            """

            INSERT INTO projects

            (

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

            VALUES

            (

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

                int(chat_id),

                str(name),

                str(industry or "other"),

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

            ),

            fetchone=True,

            commit=True

        )

        if row:

            return row.get("id")

        return None

    except Exception:

        logger.exception(

            "create_project failed"

        )

        return None

# ============================================================

# GET PROJECTS

# ============================================================

def get_projects(

    chat_id,

    limit=50

):

    """

    Returns projects belonging to the user.

    """

    try:

        rows = db_execute(

            """

            SELECT *

            FROM projects

            WHERE chat_id = %s

            ORDER BY

                updated_at DESC,

                id DESC

            LIMIT %s

            """,

            (

                int(chat_id),

                int(limit)

            ),

            fetchall=True

        )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_projects failed"

        )

        return []

# ============================================================

# GET SINGLE PROJECT

# ============================================================

def get_project(

    chat_id,

    project_id

):

    """

    Returns one project belonging to the user.

    """

    try:

        row = db_execute(

            """

            SELECT *

            FROM projects

            WHERE

                id = %s

                AND chat_id = %s

            LIMIT 1

            """,

            (

                int(project_id),

                int(chat_id)

            ),

            fetchone=True

        )

        return row

    except Exception:

        logger.exception(

            "get_project failed"

        )

        return None

# ============================================================

# UPDATE PROJECT

# ============================================================

def update_project(

    chat_id,

    project_id,

    **fields

):

    """

    Updates allowed project fields only.

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

        "status"

    }

    updates = []

    values = []

    for key, value in fields.items():

        if key not in allowed_fields:

            continue

        updates.append(

            f"{key} = %s"

        )

        values.append(

            value

        )

    if not updates:

        return False

    updates.append(

        "updated_at = CURRENT_TIMESTAMP"

    )

    values.extend(

        [

            int(project_id),

            int(chat_id)

        ]

    )

    try:

        db_execute(

            f"""

            UPDATE projects

            SET

                {", ".join(updates)}

            WHERE

                id = %s

                AND chat_id = %s

            """,

            tuple(values),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "update_project failed"

        )

        return False

# ============================================================

# DELETE PROJECT

# ============================================================

def delete_project(

    chat_id,

    project_id

):

    """

    Deletes a project belonging to the user.

    """

    try:

        db_execute(

            """

            DELETE FROM projects

            WHERE

                id = %s

                AND chat_id = %s

            """,

            (

                int(project_id),

                int(chat_id)

            ),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "delete_project failed"

        )

        return False

# ============================================================

# PROJECT SUMMARY

# ============================================================

def project_summary(

    project

):

    """

    Human-readable project summary.

    """

    if not project:

        return "პროექტი ვერ მოიძებნა."

    lines = []

    name = project.get(

        "name"

    )

    industry = project.get(

        "industry"

    )

    location = project.get(

        "location"

    )

    description = project.get(

        "description"

    )

    if name:

        lines.append(

            f"🏗️ პროექტი: {name}"

        )

    if industry:

        lines.append(

            f"📂 სფერო: {industry}"

        )

    if location:

        lines.append(

            f"📍 მდებარეობა: {location}"

        )

    if description:

        lines.append(

            f"📝 აღწერა: {description}"

        )

    area_fields = [

        (

            "land_area",

            "🌍 მიწა",

            "მ²"

        ),

        (

            "saleable_area",

            "🏢 გასაყიდი ფართობი",

            "მ²"

        ),

        (

            "construction_area",

            "🔨 სამშენებლო ფართობი",

            "მ²"

        ),

        (

            "total_area",

            "📐 სრული ფართობი",

            "მ²"

        )

    ]

    for field, label, unit in area_fields:

        value = project.get(

            field

        )

        if value is not None:

            lines.append(

                f"{label}: {value:,.2f} {unit}"

            )

    money_fields = [

        (

            "land_cost",

            "🌍 მიწის ღირებულება"

        ),

        (

            "construction_cost",

            "🔨 მშენებლობის ღირებულება"

        ),

        (

            "operating_cost",

            "⚙️ საოპერაციო ხარჯი"

        ),

        (

            "financing_cost",

            "🏦 ფინანსირების ხარჯი"

        ),

        (

            "other_cost",

            "📦 სხვა ხარჯი"

        ),

        (

            "total_cost",

            "💰 სრული ხარჯი"

        ),

        (

            "revenue",

            "💵 შემოსავალი"

        ),

        (

            "expected_revenue",

            "📈 მოსალოდნელი შემოსავალი"

        ),

        (

            "net_profit",

            "🟢 წმინდა მოგება"

        ),

        (

            "expected_profit",

            "📊 მოსალოდნელი მოგება"

        )

    ]

    for field, label in money_fields:

        value = project.get(

            field

        )

        if value is not None:

            lines.append(

                f"{label}: ${value:,.2f}"

            )

    investor_fields = [

        (

            "investor_capital",

            "💼 ინვესტორის კაპიტალი",

            "$"

        ),

        (

            "investor_profit",

            "💼 ინვესტორის მოგება",

            "$"

        ),

        (

            "investor_share",

            "📊 ინვესტორის წილი",

            "%"

        )

    ]

    for field, label, unit in investor_fields:

        value = project.get(

            field

        )

        if value is not None:

            lines.append(

                f"{label}: {value:,.2f} {unit}"

            )

    status = project.get(

        "status"

    )

    if status:

        lines.append(

            f"🔄 სტატუსი: {status}"

        )

    notes = project.get(

        "notes"

    )

    if notes:

        lines.append(

            f"📌 შენიშვნა: {notes}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# PROJECTS SUMMARY

# ============================================================

def projects_summary(

    chat_id

):

    """

    Returns all projects as a readable list.

    """

    projects = get_projects(

        chat_id

    )

    if not projects:

        return (

            "🏗️ პროექტები ჯერ არ არის "

            "დამატებული."

        )

    lines = [

        "🏗️ GENIOSA — PROJECTS",

        ""

    ]

    for project in projects:

        project_id = project.get(

            "id"

        )

        name = project.get(

            "name",

            "Unnamed"

        )

        location = project.get(

            "location"

        )

        status = project.get(

            "status"

        )

        profit = project.get(

            "expected_profit"

        )

        line = (

            f"#{project_id} — {name}"

        )

        if location:

            line += (

                f" | 📍 {location}"

            )

        if status:

            line += (

                f" | 🔄 {status}"

            )

        if profit is not None:

            line += (

                f" | 🟢 ${profit:,.0f}"

            )

        lines.append(

            line

        )

    return "\n".join(

        lines

    )

# ============================================================

# FIND PROJECT BY NAME

# ============================================================

def find_project_by_name(

    chat_id,

    name

):

    """

    Finds a project by exact or partial name.

    """

    if not name:

        return None

    try:

        row = db_execute(

            """

            SELECT *

            FROM projects

            WHERE

                chat_id = %s

                AND LOWER(name)

                    LIKE LOWER(%s)

            ORDER BY id DESC

            LIMIT 1

            """,

            (

                int(chat_id),

                f"%{str(name).strip()}%"

            ),

            fetchone=True

        )

        return row

    except Exception:

        logger.exception(

            "find_project_by_name failed"

        )

        return None

# ============================================================

# INDUSTRY NAME

# ============================================================

def industry_name(

    industry

):

    """

    Converts internal industry codes

    into human-readable names.

    """

    if not industry:

        return "Другое"

    mapping = {

        "construction": "Строительство",

        "development": "Девелопмент",

        "real_estate": "Недвижимость",

        "hotel": "Гостиничный бизнес",

        "tourism": "Туризм",

        "restaurant": "Ресторанный бизнес",

        "casino": "Казино / Gaming",

        "finance": "Финансы",

        "technology": "Технологии",

        "retail": "Ритейл",

        "energy": "Энергетика",

        "infrastructure": "Инфраструктура",

        "other": "Другое"

    }

    return mapping.get(

        str(industry).lower(),

        str(industry)

    )

# ============================================================

# FORMAT PROJECT FOR AI

# ============================================================

def project_to_ai_context(

    project

):

    """

    Converts project data into compact AI context.

    """

    if not project:

        return ""

    lines = []

    for key, value in project.items():

        if key in (

            "id",

            "chat_id",

            "created_at",

            "updated_at"

        ):

            continue

        if value is None:

            continue

        lines.append(

            f"{key}: {value}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# BUILD BUSINESS CONTEXT

# ============================================================

def build_business_context(

    chat_id

):

    """

    Combines persistent memory, projects,

    investors and deals into one context block.

    Investor/deal functions are defined in later parts.

    Missing sections are simply skipped.

    """

    sections = []

    memory_context = build_memory_context(

        chat_id

    )

    if memory_context:

        sections.append(

            memory_context

        )

    projects = get_projects(

        chat_id,

        limit=20

    )

    if projects:

        project_lines = [

            "ACTIVE PROJECTS:"

        ]

        for project in projects:

            project_id = project.get(

                "id"

            )

            name = project.get(

                "name",

                "Unnamed"

            )

            location = project.get(

                "location"

            )

            expected_profit = project.get(

                "expected_profit"

            )

            line = (

                f"- Project #{project_id}: "

                f"{name}"

            )

            if location:

                line += (

                    f" | location={location}"

                )

            if expected_profit is not None:

                line += (

                    f" | expected_profit="

                    f"{expected_profit}"

                )

            project_lines.append(

                line

            )

        sections.append(

            "\n".join(project_lines)

        )

    return "\n\n".join(

        sections

    )

# ============================================================

# PART 2 COMPLETE

# ============================================================

print(

    "GENIOSA 4.0 — PART 2/10 LOADED"

)# ============================================================

# PART 3/10

# INVESTORS + DEALS / CRM

# ============================================================

# ============================================================

# CREATE INVESTOR

# ============================================================

def create_investor(

    chat_id,

    name,

    company=None,

    country=None,

    contact=None,

    investment_capacity=None,

    preferred_sector=None,

    status="new",

    notes=None

):

    """

    Creates an investor record.

    """

    if not name:

        return None

    try:

        row = db_execute(

            """

            INSERT INTO investors

            (

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

            VALUES

            (

                %s, %s, %s, %s, %s,

                %s, %s, %s, %s

            )

            RETURNING id

            """,

            (

                int(chat_id),

                str(name).strip(),

                company,

                country,

                contact,

                investment_capacity,

                preferred_sector,

                status,

                notes

            ),

            fetchone=True,

            commit=True

        )

        if row:

            return row.get("id")

        return None

    except Exception:

        logger.exception(

            "create_investor failed"

        )

        return None

# ============================================================

# GET INVESTORS

# ============================================================

def get_investors(

    chat_id,

    limit=100

):

    """

    Returns all investors belonging to the user.

    """

    try:

        rows = db_execute(

            """

            SELECT *

            FROM investors

            WHERE chat_id = %s

            ORDER BY

                updated_at DESC,

                id DESC

            LIMIT %s

            """,

            (

                int(chat_id),

                int(limit)

            ),

            fetchall=True

        )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_investors failed"

        )

        return []

# ============================================================

# GET SINGLE INVESTOR

# ============================================================

def get_investor(

    chat_id,

    investor_id

):

    """

    Returns one investor belonging to the user.

    """

    try:

        row = db_execute(

            """

            SELECT *

            FROM investors

            WHERE

                id = %s

                AND chat_id = %s

            LIMIT 1

            """,

            (

                int(investor_id),

                int(chat_id)

            ),

            fetchone=True

        )

        return row

    except Exception:

        logger.exception(

            "get_investor failed"

        )

        return None

# ============================================================

# UPDATE INVESTOR

# ============================================================

def update_investor(

    chat_id,

    investor_id,

    **fields

):

    """

    Updates allowed investor fields only.

    """

    allowed_fields = {

        "name",

        "company",

        "country",

        "contact",

        "investment_capacity",

        "preferred_sector",

        "status",

        "notes"

    }

    updates = []

    values = []

    for key, value in fields.items():

        if key not in allowed_fields:

            continue

        updates.append(

            f"{key} = %s"

        )

        values.append(

            value

        )

    if not updates:

        return False

    updates.append(

        "updated_at = CURRENT_TIMESTAMP"

    )

    values.extend(

        [

            int(investor_id),

            int(chat_id)

        ]

    )

    try:

        db_execute(

            f"""

            UPDATE investors

            SET

                {", ".join(updates)}

            WHERE

                id = %s

                AND chat_id = %s

            """,

            tuple(values),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "update_investor failed"

        )

        return False

# ============================================================

# DELETE INVESTOR

# ============================================================

def delete_investor(

    chat_id,

    investor_id

):

    """

    Deletes an investor belonging to the user.

    """

    try:

        db_execute(

            """

            DELETE FROM investors

            WHERE

                id = %s

                AND chat_id = %s

            """,

            (

                int(investor_id),

                int(chat_id)

            ),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "delete_investor failed"

        )

        return False

# ============================================================

# FIND INVESTOR

# ============================================================

def find_investor(

    chat_id,

    search_text

):

    """

    Searches investor by name, company,

    country or contact.

    """

    if not search_text:

        return None

    pattern = (

        f"%{str(search_text).strip()}%"

    )

    try:

        row = db_execute(

            """

            SELECT *

            FROM investors

            WHERE

                chat_id = %s

                AND

                (

                    name ILIKE %s

                    OR company ILIKE %s

                    OR country ILIKE %s

                    OR contact ILIKE %s

                )

            ORDER BY id DESC

            LIMIT 1

            """,

            (

                int(chat_id),

                pattern,

                pattern,

                pattern,

                pattern

            ),

            fetchone=True

        )

        return row

    except Exception:

        logger.exception(

            "find_investor failed"

        )

        return None

# ============================================================

# FORMAT INVESTOR

# ============================================================

def format_investor(

    investor

):

    """

    Converts investor record into readable text.

    """

    if not investor:

        return "ინვესტორი ვერ მოიძებნა."

    lines = []

    investor_id = investor.get(

        "id"

    )

    name = investor.get(

        "name"

    )

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

    )

    notes = investor.get(

        "notes"

    )

    if investor_id:

        lines.append(

            f"👤 ინვესტორი #{investor_id}"

        )

    if name:

        lines.append(

            f"სახელი: {name}"

        )

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

            f"${capacity:,.0f}"

        )

    if sector:

        lines.append(

            f"📂 სასურველი სექტორი: {sector}"

        )

    if status:

        lines.append(

            f"🔄 სტატუსი: {status}"

        )

    if notes:

        lines.append(

            f"📌 შენიშვნა: {notes}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# INVESTORS SUMMARY

# ============================================================

def investors_summary(

    chat_id

):

    """

    Returns a readable investor list.

    """

    investors = get_investors(

        chat_id

    )

    if not investors:

        return (

            "👥 ინვესტორები ჯერ არ არის "

            "დამატებული."

        )

    lines = [

        "👥 GENIOSA — INVESTORS",

        ""

    ]

    for investor in investors:

        investor_id = investor.get(

            "id"

        )

        name = investor.get(

            "name",

            "Unnamed"

        )

        company = investor.get(

            "company"

        )

        country = investor.get(

            "country"

        )

        status = investor.get(

            "status"

        )

        line = (

            f"#{investor_id} — {name}"

        )

        if company:

            line += (

                f" | 🏢 {company}"

            )

        if country:

            line += (

                f" | 🌍 {country}"

            )

        if status:

            line += (

                f" | 🔄 {status}"

            )

        lines.append(

            line

        )

    return "\n".join(

        lines

    )

# ============================================================

# CREATE DEAL

# ============================================================

def create_deal(

    chat_id,

    project_id=None,

    investor_id=None,

    stage="new",

    proposed_amount=None,

    proposed_share=None,

    valuation=None,

    notes=None,

    next_step=None

):

    """

    Creates an investment / CRM deal.

    """

    try:

        # ----------------------------------------------------

        # Validate project ownership

        # ----------------------------------------------------

        if project_id is not None:

            project = get_project(

                chat_id,

                project_id

            )

            if not project:

                logger.warning(

                    "Invalid project_id=%s "

                    "for chat_id=%s",

                    project_id,

                    chat_id

                )

                return None

        # ----------------------------------------------------

        # Validate investor ownership

        # ----------------------------------------------------

        if investor_id is not None:

            investor = get_investor(

                chat_id,

                investor_id

            )

            if not investor:

                logger.warning(

                    "Invalid investor_id=%s "

                    "for chat_id=%s",

                    investor_id,

                    chat_id

                )

                return None

        # ----------------------------------------------------

        # Insert deal

        # ----------------------------------------------------

        row = db_execute(

            """

            INSERT INTO deals

            (

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

            VALUES

            (

                %s, %s, %s, %s, %s,

                %s, %s, %s, %s

            )

            RETURNING id

            """,

            (

                int(chat_id),

                project_id,

                investor_id,

                stage,

                proposed_amount,

                proposed_share,

                valuation,

                notes,

                next_step

            ),

            fetchone=True,

            commit=True

        )

        if row:

            return row.get("id")

        return None

    except Exception:

        logger.exception(

            "create_deal failed"

        )

        return None

# ============================================================

# GET DEALS

# ============================================================

def get_deals(

    chat_id,

    limit=100

):

    """

    Returns deals belonging to the user.

    """

    try:

        rows = db_execute(

            """

            SELECT

                d.*,

                p.name AS project_name,

                i.name AS investor_name,

                i.company AS investor_company

            FROM deals d

            LEFT JOIN projects p

                ON p.id = d.project_id

            LEFT JOIN investors i

                ON i.id = d.investor_id

            WHERE d.chat_id = %s

            ORDER BY

                d.updated_at DESC,

                d.id DESC

            LIMIT %s

            """,

            (

                int(chat_id),

                int(limit)

            ),

            fetchall=True

        )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_deals failed"

        )

        return []

# ============================================================

# GET SINGLE DEAL

# ============================================================

def get_deal(

    chat_id,

    deal_id

):

    """

    Returns one deal belonging to the user.

    """

    try:

        row = db_execute(

            """

            SELECT

                d.*,

                p.name AS project_name,

                i.name AS investor_name,

                i.company AS investor_company

            FROM deals d

            LEFT JOIN projects p

                ON p.id = d.project_id

            LEFT JOIN investors i

                ON i.id = d.investor_id

            WHERE

                d.id = %s

                AND d.chat_id = %s

            LIMIT 1

            """,

            (

                int(deal_id),

                int(chat_id)

            ),

            fetchone=True

        )

        return row

    except Exception:

        logger.exception(

            "get_deal failed"

        )

        return None

# ============================================================

# UPDATE DEAL

# ============================================================

def update_deal(

    chat_id,

    deal_id,

    **fields

):

    """

    Updates allowed deal fields.

    """

    allowed_fields = {

        "project_id",

        "investor_id",

        "stage",

        "proposed_amount",

        "proposed_share",

        "valuation",

        "notes",

        "next_step"

    }

    updates = []

    values = []

    for key, value in fields.items():

        if key not in allowed_fields:

            continue

        updates.append(

            f"{key} = %s"

        )

        values.append(

            value

        )

    if not updates:

        return False

    updates.append(

        "updated_at = CURRENT_TIMESTAMP"

    )

    values.extend(

        [

            int(deal_id),

            int(chat_id)

        ]

    )

    try:

        db_execute(

            f"""

            UPDATE deals

            SET

                {", ".join(updates)}

            WHERE

                id = %s

                AND chat_id = %s

            """,

            tuple(values),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "update_deal failed"

        )

        return False

# ============================================================

# DELETE DEAL

# ============================================================

def delete_deal(

    chat_id,

    deal_id

):

    """

    Deletes a deal belonging to the user.

    """

    try:

        db_execute(

            """

            DELETE FROM deals

            WHERE

                id = %s

                AND chat_id = %s

            """,

            (

                int(deal_id),

                int(chat_id)

            ),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "delete_deal failed"

        )

        return False

# ============================================================

# FORMAT DEAL

# ============================================================

def format_deal(

    deal

):

    """

    Converts deal record into readable text.

    """

    if not deal:

        return "გარიგება ვერ მოიძებნა."

    lines = []

    deal_id = deal.get(

        "id"

    )

    project_name = deal.get(

        "project_name"

    )

    investor_name = deal.get(

        "investor_name"

    )

    investor_company = deal.get(

        "investor_company"

    )

    stage = deal.get(

        "stage"

    )

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

    if deal_id:

        lines.append(

            f"🤝 გარიგება #{deal_id}"

        )

    if project_name:

        lines.append(

            f"🏗️ პროექტი: {project_name}"

        )

    if investor_name:

        investor_line = (

            f"👤 ინვესტორი: {investor_name}"

        )

        if investor_company:

            investor_line += (

                f" ({investor_company})"

            )

        lines.append(

            investor_line

        )

    if stage:

        lines.append(

            f"🔄 ეტაპი: {stage}"

        )

    if proposed_amount is not None:

        lines.append(

            f"💰 შეთავაზებული თანხა: "

            f"${proposed_amount:,.0f}"

        )

    if proposed_share is not None:

        lines.append(

            f"📊 შეთავაზებული წილი: "

            f"{proposed_share:,.2f}%"

        )

    if valuation is not None:

        lines.append(

            f"🏦 შეფასება: "

            f"${valuation:,.0f}"

        )

    if notes:

        lines.append(

            f"📌 შენიშვნა: {notes}"

        )

    if next_step:

        lines.append(

            f"➡️ შემდეგი ნაბიჯი: {next_step}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# DEALS SUMMARY

# ============================================================

def deals_summary(

    chat_id

):

    """

    Returns a readable deal pipeline.

    """

    deals = get_deals(

        chat_id

    )

    if not deals:

        return (

            "🤝 გარიგებები ჯერ არ არის "

            "დამატებული."

        )

    lines = [

        "🤝 GENIOSA — DEAL PIPELINE",

        ""

    ]

    for deal in deals:

        deal_id = deal.get(

            "id"

        )

        project_name = deal.get(

            "project_name"

        ) or "პროექტი"

        investor_name = deal.get(

            "investor_name"

        ) or "ინვესტორი"

        stage = deal.get(

            "stage"

        ) or "new"

        amount = deal.get(

            "proposed_amount"

        )

        line = (

            f"#{deal_id} — "

            f"{project_name} ↔ "

            f"{investor_name}"

        )

        line += (

            f" | {stage}"

        )

        if amount is not None:

            line += (

                f" | ${amount:,.0f}"

            )

        lines.append(

            line

        )

    return "\n".join(

        lines

    )

# ============================================================

# FIND DEALS BY PROJECT

# ============================================================

def get_project_deals(

    chat_id,

    project_id

):

    """

    Returns all deals for a specific project.

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

            WHERE

                d.chat_id = %s

                AND d.project_id = %s

            ORDER BY

                d.updated_at DESC,

                d.id DESC

            """,

            (

                int(chat_id),

                int(project_id)

            ),

            fetchall=True

        )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_project_deals failed"

        )

        return []

# ============================================================

# BUILD CRM CONTEXT FOR AI

# ============================================================

def build_crm_context(

    chat_id

):

    """

    Builds compact investor + deal context

    for Gemini.

    """

    sections = []

    investors = get_investors(

        chat_id,

        limit=20

    )

    if investors:

        investor_lines = [

            "INVESTORS:"

        ]

        for investor in investors:

            investor_id = investor.get(

                "id"

            )

            name = investor.get(

                "name"

            ) or "Unnamed"

            company = investor.get(

                "company"

            )

            country = investor.get(

                "country"

            )

            status = investor.get(

                "status"

            )

            line = (

                f"- Investor #{investor_id}: "

                f"{name}"

            )

            if company:

                line += (

                    f" | company={company}"

                )

            if country:

                line += (

                    f" | country={country}"

                )

            if status:

                line += (

                    f" | status={status}"

                )

            investor_lines.append(

                line

            )

        sections.append(

            "\n".join(

                investor_lines

            )

        )

    deals = get_deals(

        chat_id,

        limit=20

    )

    if deals:

        deal_lines = [

            "DEALS:"

        ]

        for deal in deals:

            deal_id = deal.get(

                "id"

            )

            project_name = (

                deal.get(

                    "project_name"

                )

                or "Unknown project"

            )

            investor_name = (

                deal.get(

                    "investor_name"

                )

                or "Unknown investor"

            )

            stage = (

                deal.get(

                    "stage"

                )

                or "new"

            )

            amount = deal.get(

                "proposed_amount"

            )

            line = (

                f"- Deal #{deal_id}: "

                f"{project_name} / "

                f"{investor_name} / "

                f"stage={stage}"

            )

            if amount is not None:

                line += (

                    f" / amount={amount}"

                )

            deal_lines.append(

                line

            )

        sections.append(

            "\n".join(

                deal_lines

            )

        )

    return "\n\n".join(

        sections

    )

# ============================================================

# PART 3 COMPLETE

# ============================================================

print(

    "GENIOSA 4.0 — PART 3/10 LOADED"

)# ============================================================

# PART 4/10

# DOCUMENTS + FILE EXTRACTION + AI ANALYSIS PREPARATION

# ============================================================

# ============================================================

# DETECT FILE TYPE

# ============================================================

def detect_file_type(

    filename

):

    """

    Detects supported document type from filename.

    """

    if not filename:

        return "unknown"

    extension = (

        Path(str(filename))

        .suffix

        .lower()

        .strip()

    )

    mapping = {

        ".pdf": "pdf",

        ".docx": "docx",

        ".xlsx": "xlsx",

        ".xlsm": "xlsm",

        ".pptx": "pptx",

        ".txt": "txt",

        ".csv": "csv"

    }

    return mapping.get(

        extension,

        "unknown"

    )

# ============================================================

# SUPPORTED DOCUMENT TYPES

# ============================================================

SUPPORTED_DOCUMENT_TYPES = {

    "pdf",

    "docx",

    "xlsx",

    "xlsm",

    "pptx",

    "txt",

    "csv"

}

# ============================================================

# EXTRACT PDF TEXT

# ============================================================

def extract_pdf_text(

    file_path

):

    """

    Extracts text from PDF.

    """

    try:

        reader = PdfReader(

            str(file_path)

        )

        pages = []

        for index, page in enumerate(

            reader.pages,

            start=1

        ):

            try:

                text = page.extract_text()

            except Exception:

                text = ""

            if text:

                pages.append(

                    f"--- PAGE {index} ---\n"

                    f"{text}"

                )

        return "\n\n".join(

            pages

        ).strip()

    except Exception:

        logger.exception(

            "extract_pdf_text failed"

        )

        return ""

# ============================================================

# EXTRACT DOCX TEXT

# ============================================================

def extract_docx_text(

    file_path

):

    """

    Extracts paragraphs and table contents from DOCX.

    """

    try:

        document = Document(

            str(file_path)

        )

        parts = []

        # ----------------------------------------------------

        # PARAGRAPHS

        # ----------------------------------------------------

        for paragraph in document.paragraphs:

            text = (

                paragraph.text

                or ""

            ).strip()

            if text:

                parts.append(

                    text

                )

        # ----------------------------------------------------

        # TABLES

        # ----------------------------------------------------

        for table_index, table in enumerate(

            document.tables,

            start=1

        ):

            parts.append(

                f"--- TABLE {table_index} ---"

            )

            for row in table.rows:

                cells = []

                for cell in row.cells:

                    cell_text = (

                        cell.text

                        or ""

                    ).strip()

                    cells.append(

                        cell_text

                    )

                parts.append(

                    " | ".join(

                        cells

                    )

                )

        return "\n".join(

            parts

        ).strip()

    except Exception:

        logger.exception(

            "extract_docx_text failed"

        )

        return ""

# ============================================================

# EXTRACT XLSX / XLSM TEXT

# ============================================================

def extract_excel_text(

    file_path

):

    """

    Extracts workbook data from XLSX/XLSM.

    Formulas are loaded as formulas so the AI can

    inspect the actual model structure.

    """

    try:

        workbook = load_workbook(

            filename=str(file_path),

            data_only=False,

            read_only=True

        )

        parts = []

        for worksheet in workbook.worksheets:

            parts.append(

                f"--- SHEET: "

                f"{worksheet.title} ---"

            )

            row_count = 0

            for row in worksheet.iter_rows(

                values_only=True

            ):

                row_count += 1

                values = []

                for value in row:

                    if value is None:

                        values.append("")

                        continue

                    values.append(

                        str(value)

                    )

                # Skip completely empty rows.

                if not any(

                    value.strip()

                    for value in values

                ):

                    continue

                parts.append(

                    " | ".join(

                        values

                    )

                )

                # Safety limit per sheet.

                if row_count >= 5000:

                    parts.append(

                        "[SHEET TRUNCATED "

                        "AFTER 5000 ROWS]"

                    )

                    break

        try:

            workbook.close()

        except Exception:

            pass

        return "\n".join(

            parts

        ).strip()

    except Exception:

        logger.exception(

            "extract_excel_text failed"

        )

        return ""

# ============================================================

# EXTRACT PPTX TEXT

# ============================================================

def extract_pptx_text(

    file_path

):

    """

    Extracts text from PowerPoint slides.

    """

    try:

        presentation = Presentation(

            str(file_path)

        )

        parts = []

        for slide_number, slide in enumerate(

            presentation.slides,

            start=1

        ):

            parts.append(

                f"--- SLIDE {slide_number} ---"

            )

            for shape in slide.shapes:

                if not hasattr(

                    shape,

                    "text"

                ):

                    continue

                text = (

                    shape.text

                    or ""

                ).strip()

                if text:

                    parts.append(

                        text

                    )

        return "\n".join(

            parts

        ).strip()

    except Exception:

        logger.exception(

            "extract_pptx_text failed"

        )

        return ""

# ============================================================

# EXTRACT TXT TEXT

# ============================================================

def extract_txt_text(

    file_path

):

    """

    Reads plain text file.

    """

    encodings = [

        "utf-8",

        "utf-8-sig",

        "cp1251",

        "latin-1"

    ]

    for encoding in encodings:

        try:

            return Path(

                file_path

            ).read_text(

                encoding=encoding

            ).strip()

        except UnicodeDecodeError:

            continue

        except Exception:

            logger.exception(

                "extract_txt_text failed"

            )

            return ""

    return ""

# ============================================================

# EXTRACT CSV TEXT

# ============================================================

def extract_csv_text(

    file_path

):

    """

    Reads CSV as text.

    This intentionally keeps the table structure simple

    so Gemini can understand it reliably.

    """

    encodings = [

        "utf-8-sig",

        "utf-8",

        "cp1251",

        "latin-1"

    ]

    for encoding in encodings:

        try:

            text = Path(

                file_path

            ).read_text(

                encoding=encoding

            )

            return text.strip()

        except UnicodeDecodeError:

            continue

        except Exception:

            logger.exception(

                "extract_csv_text failed"

            )

            return ""

    return ""

# ============================================================

# EXTRACT FILE TEXT

# ============================================================

def extract_file_text(

    file_path,

    file_type=None

):

    """

    Main document text extraction dispatcher.

    """

    file_path = Path(

        file_path

    )

    if not file_path.exists():

        return ""

    if not file_type:

        file_type = detect_file_type(

            file_path.name

        )

    if file_type == "pdf":

        return extract_pdf_text(

            file_path

        )

    if file_type == "docx":

        return extract_docx_text(

            file_path

        )

    if file_type in (

        "xlsx",

        "xlsm"

    ):

        return extract_excel_text(

            file_path

        )

    if file_type == "pptx":

        return extract_pptx_text(

            file_path

        )

    if file_type == "txt":

        return extract_txt_text(

            file_path

        )

    if file_type == "csv":

        return extract_csv_text(

            file_path

        )

    return ""

# ============================================================

# SAVE DOCUMENT RECORD

# ============================================================

def save_document_record(

    chat_id,

    filename,

    file_type,

    extracted_text="",

    project_id=None,

    analysis=None

):

    """

    Saves uploaded document information.

    """

    try:

        row = db_execute(

            """

            INSERT INTO documents

            (

                chat_id,

                project_id,

                filename,

                file_type,

                extracted_text,

                analysis

            )

            VALUES

            (

                %s, %s, %s, %s, %s, %s

            )

            RETURNING id

            """,

            (

                int(chat_id),

                project_id,

                filename,

                file_type,

                extracted_text,

                analysis

            ),

            fetchone=True,

            commit=True

        )

        if row:

            return row.get("id")

        return None

    except Exception:

        logger.exception(

            "save_document_record failed"

        )

        return None

# ============================================================

# UPDATE DOCUMENT ANALYSIS

# ============================================================

def update_document_analysis(

    chat_id,

    document_id,

    analysis

):

    """

    Saves AI analysis for an existing document.

    """

    try:

        db_execute(

            """

            UPDATE documents

            SET

                analysis = %s,

                updated_at = CURRENT_TIMESTAMP

            WHERE

                id = %s

                AND chat_id = %s

            """,

            (

                analysis,

                int(document_id),

                int(chat_id)

            ),

            commit=True

        )

        return True

    except Exception:

        logger.exception(

            "update_document_analysis failed"

        )

        return False

# ============================================================

# GET DOCUMENTS

# ============================================================

def get_documents(

    chat_id,

    limit=50

):

    """

    Returns uploaded documents.

    """

    try:

        rows = db_execute(

            """

            SELECT *

            FROM documents

            WHERE chat_id = %s

            ORDER BY

                created_at DESC,

                id DESC

            LIMIT %s

            """,

            (

                int(chat_id),

                int(limit)

            ),

            fetchall=True

        )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_documents failed"

        )

        return []

# ============================================================

# GET SINGLE DOCUMENT

# ============================================================

def get_document(

    chat_id,

    document_id

):

    """

    Returns one document belonging to the user.

    """

    try:

        row = db_execute(

            """

            SELECT *

            FROM documents

            WHERE

                id = %s

                AND chat_id = %s

            LIMIT 1

            """,

            (

                int(document_id),

                int(chat_id)

            ),

            fetchone=True

        )

        return row

    except Exception:

        logger.exception(

            "get_document failed"

        )

        return None

# ============================================================

# DOCUMENT SUMMARY

# ============================================================

def documents_summary(

    chat_id

):

    """

    Returns uploaded document list.

    """

    documents = get_documents(

        chat_id

    )

    if not documents:

        return (

            "📄 დოკუმენტები ჯერ არ არის "

            "ატვირთული."

        )

    lines = [

        "📄 GENIOSA — DOCUMENTS",

        ""

    ]

    for document in documents:

        document_id = document.get(

            "id"

        )

        filename = document.get(

            "filename"

        ) or "Unnamed"

        file_type = document.get(

            "file_type"

        ) or "unknown"

        created_at = document.get(

            "created_at"

        )

        line = (

            f"#{document_id} — "

            f"{filename} "

            f"[{file_type}]"

        )

        if created_at:

            line += (

                f" | {created_at}"

            )

        lines.append(

            line

        )

    return "\n".join(

        lines

    )

# ============================================================

# DOCUMENT TEXT LIMIT

# ============================================================

MAX_DOCUMENT_AI_TEXT = 50000

# ============================================================

# PREPARE DOCUMENT FOR AI

# ============================================================

def prepare_document_for_ai(

    extracted_text

):

    """

    Prevents excessively large documents from exceeding

    Gemini request limits.

    """

    if not extracted_text:

        return ""

    text = str(

        extracted_text

    ).strip()

    if len(text) <= MAX_DOCUMENT_AI_TEXT:

        return text

    return (

        text[:MAX_DOCUMENT_AI_TEXT]

        + "\n\n"

        "[DOCUMENT TEXT TRUNCATED BY GENIOSA]"

    )

# ============================================================

# DOCUMENT ANALYSIS PROMPT

# ============================================================

def build_document_analysis_prompt(

    filename,

    extracted_text,

    user_request=None

):

    """

    Creates a professional business-document analysis prompt.

    """

    document_text = prepare_document_for_ai(

        extracted_text

    )

    request_text = (

        str(user_request).strip()

        if user_request

        else

        "Analyze this document comprehensively."

    )

    return f"""

You are GENIOSA, an expert business,

investment, construction and financial advisor.

Analyze the uploaded document professionally.

FILE:

{filename}

USER REQUEST:

{request_text}

DOCUMENT CONTENT:

{document_text}

Provide a practical analysis.

Depending on the document type, examine:

1. Executive summary

2. Key facts and figures

3. Financial information

4. Revenue and cost assumptions

5. Investment requirements

6. Risks and weaknesses

7. Opportunities

8. Important missing information

9. Internal inconsistencies or suspicious numbers

10. Recommendations

11. Questions that should be asked before making a decision

If this is a financial model:

- check calculations conceptually

- identify unrealistic assumptions

- distinguish revenue, cost, gross profit and net profit

- identify investor return implications

- identify financing risks

If this is a construction/development project:

- examine area data

- construction cost assumptions

- land cost

- sales assumptions

- timeline

- profitability

- investor structure

- key project risks

Do not invent facts that are not present in the document.

Clearly separate document facts from your professional assessment.

""".strip()

# ============================================================

# ANALYZE DOCUMENT WITH AI

# ============================================================

def analyze_document_with_ai(

    filename,

    extracted_text,

    user_request=None

):

    """

    Sends extracted document content to Gemini.

    gemini_generate() is implemented in a later part.

    """

    prompt = build_document_analysis_prompt(

        filename,

        extracted_text,

        user_request

    )

    try:

        # gemini_generate is defined later in app.py.

        result = gemini_generate(

            prompt

        )

        return result

    except Exception:

        logger.exception(

            "analyze_document_with_ai failed"

        )

        return (

            "დოკუმენტის AI-ანალიზი ვერ შესრულდა."

        )

# ============================================================

# PROCESS DOCUMENT FILE

# ============================================================

def process_document_file(

    chat_id,

    file_path,

    filename,

    project_id=None,

    user_request=None

):

    """

    Complete document processing pipeline:

    1. Detect type

    2. Extract text

    3. Save database record

    4. Analyze with Gemini

    5. Save analysis

    """

    file_type = detect_file_type(

        filename

    )

    if file_type == "unknown":

        extension = (

            Path(filename)

            .suffix

            .lower()

        )

        if extension in (

            ".doc",

            ".xls"

        ):

            return {

                "success": False,

                "error": (

                    f"ფაილი {extension} "

                    "ფორმატშია. გთხოვთ გადააკეთოთ "

                    "DOCX ან XLSX ფორმატში."

                )

            }

        return {

            "success": False,

            "error": (

                "ეს ფაილის ფორმატი "

                "Geniosa-ს მიერ ამ ეტაპზე "

                "არ არის მხარდაჭერილი."

            )

        }

    extracted_text = extract_file_text(

        file_path,

        file_type

    )

    if not extracted_text:

        return {

            "success": False,

            "error": (

                "ფაილიდან ტექსტის ამოღება ვერ მოხერხდა. "

                "შესაძლოა დოკუმენტი იყოს სკანირებული "

                "ან ცარიელი."

            )

        }

    document_id = save_document_record(

        chat_id=chat_id,

        filename=filename,

        file_type=file_type,

        extracted_text=extracted_text,

        project_id=project_id

    )

    if not document_id:

        return {

            "success": False,

            "error": (

                "დოკუმენტის მონაცემთა ბაზაში "

                "შენახვა ვერ მოხერხდა."

            )

        }

    analysis = analyze_document_with_ai(

        filename=filename,

        extracted_text=extracted_text,

        user_request=user_request

    )

    update_document_analysis(

        chat_id=chat_id,

        document_id=document_id,

        analysis=analysis

    )

    return {

        "success": True,

        "document_id": document_id,

        "filename": filename,

        "file_type": file_type,

        "extracted_text": extracted_text,

        "analysis": analysis

    }

# ============================================================

# PART 4 COMPLETE

# ============================================================

print(

    "GENIOSA 4.0 — PART 4/10 LOADED"

)# ============================================================

# PART 5/10

# GEMINI AI ENGINE + BUSINESS CONTEXT + AI CHAT

# ============================================================

# ============================================================

# AI SYSTEM PROMPT

# ============================================================

def ai_system_prompt():

    """

    Core personality and operating rules for Geniosa.

    """

    return """

You are GENIOSA 4.0 — an advanced private business,

investment and development advisor.

Your role is to help the user make better business,

investment, construction, real-estate and financial decisions.

You are especially strong in:

- Construction

- Real estate development

- Investment analysis

- Investor relations

- Financial modelling

- Project feasibility

- Business strategy

- CRM and investor management

- Negotiation strategy

- Risk analysis

- Market analysis

- Tourism and hospitality

- Hotels and serviced apartments

- Commercial real estate

- Project finance

- Joint ventures

- Business presentations

- Investment proposals

IMPORTANT RULES:

1. Be practical, direct and business-oriented.

2. Do not invent facts, prices, investors, companies,

   market statistics or financial results.

3. Clearly distinguish:

   - facts

   - assumptions

   - estimates

   - recommendations

4. When financial information is available,

   calculate carefully.

5. When the user gives project numbers,

   preserve them accurately unless the user asks

   you to change them.

6. If numbers appear inconsistent,

   point out the inconsistency instead of silently

   changing the numbers.

7. When evaluating an investment,

   examine:

   - capital requirement

   - revenue

   - total cost

   - profit

   - investor return

   - investor share

   - financing cost

   - timeline

   - risks

   - exit possibilities

8. For construction projects examine:

   - land

   - construction area

   - saleable area

   - construction cost

   - finishing cost

   - infrastructure

   - permits

   - sales assumptions

   - timeline

   - contingency

   - financing

9. When the user asks whether a deal is realistic,

   give an honest assessment.

10. Do not flatter the user or automatically agree.

    If an assumption is weak, say so clearly.

11. When the user asks for a proposal or investor material,

    write it professionally and in a form suitable

    for real business communication.

12. The user may communicate in Georgian or Russian.

    Reply in the language used by the user unless

    another language is requested.

13. Keep answers structured and easy to read.

14. When useful, use:

    - tables

    - bullet points

    - calculations

    - step-by-step recommendations

15. Never claim to have searched the live internet

    unless live web information was actually supplied

    to you.

16. Do not claim that a document, spreadsheet,

    investor or company exists unless it is present

    in the supplied context or database.

17. Treat the user's persistent business memory,

    projects, investors and deals as important context.

18. Protect user data and do not expose API keys,

    tokens or private credentials.

Your objective is not merely to answer questions.

Your objective is to help the user:

- understand the situation,

- identify opportunities,

- identify risks,

- calculate economics,

- structure deals,

- prepare investor communications,

- and make better business decisions.

""".strip()

# ============================================================

# GEMINI RESPONSE EXTRACTION

# ============================================================

def extract_gemini_text(

    data

):

    """

    Safely extracts generated text from Gemini API response.

    """

    if not isinstance(

        data,

        dict

    ):

        return ""

    candidates = data.get(

        "candidates"

    )

    if not candidates:

        return ""

    for candidate in candidates:

        if not isinstance(

            candidate,

            dict

        ):

            continue

        content = candidate.get(

            "content"

        )

        if not isinstance(

            content,

            dict

        ):

            continue

        parts = content.get(

            "parts"

        )

        if not isinstance(

            parts,

            list

        ):

            continue

        text_parts = []

        for part in parts:

            if not isinstance(

                part,

                dict

            ):

                continue

            text_value = part.get(

                "text"

            )

            if text_value:

                text_parts.append(

                    str(text_value)

                )

        if text_parts:

            return "\n".join(

                text_parts

            ).strip()

    return ""

# ============================================================

# GEMINI API ERROR EXTRACTION

# ============================================================

def extract_gemini_error(

    data

):

    """

    Extracts a useful Gemini error message.

    """

    if not isinstance(

        data,

        dict

    ):

        return ""

    error = data.get(

        "error"

    )

    if not isinstance(

        error,

        dict

    ):

        return ""

    message = error.get(

        "message"

    )

    if message:

        return str(

            message

        )

    return ""

# ============================================================

# GEMINI GENERATE

# ============================================================

def gemini_generate(

    prompt,

    model=None,

    temperature=0.4,

    max_output_tokens=4096

):

    """

    Calls Gemini generateContent API.

    This function is intentionally synchronous because

    Telegram polling in Geniosa is also synchronous.

    """

    if not GEMINI_API_KEY:

        raise RuntimeError(

            "GEMINI_API_KEY is not configured."

        )

    if not prompt:

        return (

            "ვერ მივიღე საკმარისი ინფორმაცია "

            "პასუხის გასაცემად."

        )

    selected_model = (

        model or GEMINI_MODEL

    ).strip()

    url = gemini_url(

        selected_model

    )

    headers = {

        "Content-Type": "application/json"

    }

    payload = {

        "systemInstruction": {

            "parts": [

                {

                    "text": ai_system_prompt()

                }

            ]

        },

        "contents": [

            {

                "role": "user",

                "parts": [

                    {

                        "text": str(prompt)

                    }

                ]

            }

        ],

        "generationConfig": {

            "temperature": float(

                temperature

            ),

            "maxOutputTokens": int(

                max_output_tokens

            )

        }

    }

    try:

        response = requests.post(

            url,

            headers=headers,

            params={

                "key": GEMINI_API_KEY

            },

            json=payload,

            timeout=90

        )

    except requests.Timeout:

        logger.error(

            "Gemini request timed out."

        )

        raise RuntimeError(

            "Gemini AI-ს პასუხის მიღებას "

            "დრო დასჭირდა."

        )

    except requests.RequestException as exc:

        logger.error(

            "Gemini network error: %s",

            exc

        )

        raise RuntimeError(

            "Gemini AI-სთან დაკავშირება ვერ მოხერხდა."

        )

    # --------------------------------------------------------

    # HTTP ERROR

    # --------------------------------------------------------

    if response.status_code != 200:

        try:

            data = response.json()

        except Exception:

            data = {}

        error_message = extract_gemini_error(

            data

        )

        logger.error(

            "Gemini HTTP %s: %s",

            response.status_code,

            error_message

            or response.text[:1000]

        )

        if response.status_code in (

            401,

            403

        ):

            raise RuntimeError(

                "Gemini API Key არასწორია ან "

                "წვდომა შეზღუდულია."

            )

        if response.status_code == 429:

            raise RuntimeError(

                "Gemini API-ის ლიმიტი ამოიწურა. "

                "ცოტა ხანში სცადე ხელახლა."

            )

        raise RuntimeError(

            "Gemini AI-მ შეცდომა დააბრუნა."

        )

    # --------------------------------------------------------

    # JSON

    # --------------------------------------------------------

    try:

        data = response.json()

    except Exception:

        logger.error(

            "Gemini returned invalid JSON."

        )

        raise RuntimeError(

            "Gemini AI-სგან არასწორი პასუხი მივიღეთ."

        )

    # --------------------------------------------------------

    # TEXT

    # --------------------------------------------------------

    result = extract_gemini_text(

        data

    )

    if result:

        return result

    # --------------------------------------------------------

    # BLOCKED / EMPTY RESPONSE

    # --------------------------------------------------------

    logger.warning(

        "Gemini returned no text: %s",

        str(data)[:2000]

    )

    return (

        "Gemini-მ ტექსტური პასუხი ვერ დააბრუნა. "

        "სცადე შეკითხვის სხვანაირად ფორმულირება."

    )

# ============================================================

# BUILD FULL AI CONTEXT

# ============================================================

def build_full_ai_context(

    chat_id

):

    """

    Combines all relevant Geniosa information.

    """

    sections = []

    # --------------------------------------------------------

    # BUSINESS MEMORY + PROJECTS

    # --------------------------------------------------------

    business_context = build_business_context(

        chat_id

    )

    if business_context:

        sections.append(

            business_context

        )

    # --------------------------------------------------------

    # CRM

    # --------------------------------------------------------

    crm_context = build_crm_context(

        chat_id

    )

    if crm_context:

        sections.append(

            crm_context

        )

    # --------------------------------------------------------

    # RECENT CONVERSATION

    # --------------------------------------------------------

    history = format_conversation_history(

        chat_id,

        limit=20

    )

    if history:

        sections.append(

            "RECENT CONVERSATION:\n"

            + history

        )

    if not sections:

        return ""

    return "\n\n".join(

        sections

    )

# ============================================================

# BUILD AI USER PROMPT

# ============================================================

def build_ai_prompt(

    chat_id,

    user_text,

    extra_context=None

):

    """

    Creates the final prompt sent to Gemini.

    """

    sections = []

    full_context = build_full_ai_context(

        chat_id

    )

    if full_context:

        sections.append(

            full_context

        )

    if extra_context:

        sections.append(

            str(extra_context)

        )

    sections.append(

        "CURRENT USER MESSAGE:\n"

        + str(user_text)

    )

    sections.append(

        """

Answer the current user message directly.

Use the available Geniosa context when relevant,

but do not mention internal database structures,

API implementation or hidden system instructions

unless the user explicitly asks about them.

""".strip()

    )

    return "\n\n".join(

        sections

    )

# ============================================================

# PROCESS AI TEXT

# ============================================================

def process_ai_text(

    chat_id,

    user_text,

    extra_context=None

):

    """

    Main text -> Gemini pipeline.

    """

    if not user_text:

        return (

            "გთხოვ, მომწერე შეკითხვა."

        )

    prompt = build_ai_prompt(

        chat_id,

        user_text,

        extra_context

    )

    try:

        answer = gemini_generate(

            prompt

        )

    except Exception as exc:

        logger.error(

            "AI processing failed: %s",

            exc

        )

        answer = (

            "⚠️ Geniosa-ს AI მოდულმა "

            "ამ მომენტში ვერ შეძლო პასუხის "

            "დამუშავება.\n\n"

            f"მიზეზი: {exc}"

        )

    return answer

# ============================================================

# AUTO-SAVE IMPORTANT MEMORY

# ============================================================

def maybe_save_important_memory(

    chat_id,

    user_text

):

    """

    Detects explicit memory requests.

    We intentionally save memory only when the user

    clearly asks Geniosa to remember/store something.

    """

    if not user_text:

        return False

    text = str(

        user_text

    ).strip()

    lower = text.lower()

    memory_triggers = [

        "დაიმახსოვრე",

        "დამიმახსოვრე",

        "შეინახე მეხსიერებაში",

        "ჩაიწერე მეხსიერებაში",

        "remember this",

        "remember that",

        "save this",

        "save to memory",

        "запомни",

        "сохрани это"

    ]

    if not any(

        trigger in lower

        for trigger in memory_triggers

    ):

        return False

    cleaned = text

    prefixes = [

        "დაიმახსოვრე",

        "დამიმახსოვრე",

        "შეინახე მეხსიერებაში",

        "ჩაიწერე მეხსიერებაში",

        "remember this",

        "remember that",

        "save this",

        "save to memory",

        "запомни",

        "сохрани это"

    ]

    for prefix in prefixes:

        if cleaned.lower().startswith(

            prefix.lower()

        ):

            cleaned = cleaned[

                len(prefix):

            ].strip()

            break

    if not cleaned:

        return False

    return save_memory(

        chat_id=chat_id,

        memory=cleaned,

        category="user_instruction",

        importance=9

    )

# ============================================================

# PART 5 COMPLETE

# ============================================================

print(

    "GENIOSA 4.0 — PART 5/10 LOADED"

)# ============================================================

# PART 6/10

# FINANCIAL ANALYSIS ENGINE

# ============================================================

# ============================================================

# SAFE NUMBER CONVERSION

# ============================================================

def to_float(

    value,

    default=None

):

    """

    Safely converts a value to float.

    """

    if value is None:

        return default

    if isinstance(

        value,

        bool

    ):

        return default

    try:

        if isinstance(

            value,

            str

        ):

            cleaned = (

                value

                .replace(",", "")

                .replace("$", "")

                .replace("€", "")

                .replace("₾", "")

                .strip()

            )

            if not cleaned:

                return default

            return float(

                cleaned

            )

        return float(

            value

        )

    except (

        TypeError,

        ValueError

    ):

        return default

# ============================================================

# SAFE PERCENTAGE

# ============================================================

def normalize_percentage(

    value,

    default=None

):

    """

    Converts percentage input to a number.

    """

    number = to_float(

        value,

        default

    )

    if number is None:

        return default

    return number

# ============================================================

# CALCULATE TOTAL COST

# ============================================================

def calculate_total_cost(

    land_cost=0,

    construction_cost=0,

    operating_cost=0,

    financing_cost=0,

    other_cost=0

):

    """

    Calculates total project cost.

    """

    values = [

        land_cost,

        construction_cost,

        operating_cost,

        financing_cost,

        other_cost

    ]

    total = 0.0

    for value in values:

        number = to_float(

            value,

            0

        )

        total += number

    return total

# ============================================================

# CALCULATE PROFIT

# ============================================================

def calculate_profit(

    revenue,

    total_cost

):

    """

    Calculates project profit.

    """

    revenue_value = to_float(

        revenue,

        0

    )

    cost_value = to_float(

        total_cost,

        0

    )

    return (

        revenue_value

        - cost_value

    )

# ============================================================

# CALCULATE PROFIT MARGIN

# ============================================================

def calculate_profit_margin(

    revenue,

    profit

):

    """

    Profit margin as percentage of revenue.

    """

    revenue_value = to_float(

        revenue,

        0

    )

    profit_value = to_float(

        profit,

        0

    )

    if revenue_value == 0:

        return 0.0

    return (

        profit_value

        / revenue_value

        * 100

    )

# ============================================================

# CALCULATE ROI

# ============================================================

def calculate_roi(

    profit,

    invested_capital

):

    """

    ROI = profit / invested capital.

    """

    profit_value = to_float(

        profit,

        0

    )

    capital_value = to_float(

        invested_capital,

        0

    )

    if capital_value == 0:

        return 0.0

    return (

        profit_value

        / capital_value

        * 100

    )

# ============================================================

# CALCULATE INVESTOR PROFIT

# ============================================================

def calculate_investor_profit(

    net_profit,

    investor_share

):

    """

    Calculates investor's share of net profit.

    """

    profit_value = to_float(

        net_profit,

        0

    )

    share_value = normalize_percentage(

        investor_share,

        0

    )

    return (

        profit_value

        * share_value

        / 100

    )

# ============================================================

# CALCULATE OPERATOR PROFIT

# ============================================================

def calculate_operator_profit(

    net_profit,

    investor_share

):

    """

    Calculates the remaining project profit.

    """

    profit_value = to_float(

        net_profit,

        0

    )

    investor_share_value = normalize_percentage(

        investor_share,

        0

    )

    operator_share = (

        100

        - investor_share_value

    )

    return (

        profit_value

        * operator_share

        / 100

    )

# ============================================================

# CALCULATE BREAK-EVEN REVENUE

# ============================================================

def calculate_break_even_revenue(

    total_cost

):

    """

    Revenue required to cover all costs.

    """

    return to_float(

        total_cost,

        0

    )

# ============================================================

# CALCULATE REQUIRED SALE PRICE

# ============================================================

def calculate_required_sale_price(

    total_cost,

    saleable_area,

    target_profit=0

):

    """

    Calculates average required sale price per m².

    Formula:

    (total cost + target profit) / saleable area

    """

    cost = to_float(

        total_cost,

        0

    )

    profit = to_float(

        target_profit,

        0

    )

    area = to_float(

        saleable_area,

        0

    )

    if area <= 0:

        return 0.0

    return (

        cost + profit

    ) / area

# ============================================================

# CALCULATE REVENUE FROM AREA + PRICE

# ============================================================

def calculate_area_revenue(

    area,

    price_per_m2

):

    """

    Calculates revenue from area and price per m².

    """

    area_value = to_float(

        area,

        0

    )

    price_value = to_float(

        price_per_m2,

        0

    )

    return (

        area_value

        * price_value

    )

# ============================================================

# FINANCIAL MODEL

# ============================================================

def financial_model(

    revenue,

    land_cost=0,

    construction_cost=0,

    operating_cost=0,

    financing_cost=0,

    other_cost=0,

    investor_capital=0,

    investor_share=0,

    saleable_area=0

):

    """

    Produces a complete basic project financial model.

    """

    revenue = to_float(

        revenue,

        0

    )

    land_cost = to_float(

        land_cost,

        0

    )

    construction_cost = to_float(

        construction_cost,

        0

    )

    operating_cost = to_float(

        operating_cost,

        0

    )

    financing_cost = to_float(

        financing_cost,

        0

    )

    other_cost = to_float(

        other_cost,

        0

    )

    investor_capital = to_float(

        investor_capital,

        0

    )

    investor_share = normalize_percentage(

        investor_share,

        0

    )

    saleable_area = to_float(

        saleable_area,

        0

    )

    total_cost = calculate_total_cost(

        land_cost=land_cost,

        construction_cost=construction_cost,

        operating_cost=operating_cost,

        financing_cost=financing_cost,

        other_cost=other_cost

    )

    net_profit = calculate_profit(

        revenue,

        total_cost

    )

    profit_margin = calculate_profit_margin(

        revenue,

        net_profit

    )

    roi = calculate_roi(

        net_profit,

        investor_capital

    )

    investor_profit = calculate_investor_profit(

        net_profit,

        investor_share

    )

    operator_profit = calculate_operator_profit(

        net_profit,

        investor_share

    )

    required_sale_price = (

        calculate_required_sale_price(

            total_cost,

            saleable_area

        )

        if saleable_area > 0

        else 0

    )

    return {

        "revenue": revenue,

        "land_cost": land_cost,

        "construction_cost": construction_cost,

        "operating_cost": operating_cost,

        "financing_cost": financing_cost,

        "other_cost": other_cost,

        "total_cost": total_cost,

        "net_profit": net_profit,

        "profit_margin": profit_margin,

        "investor_capital": investor_capital,

        "investor_share": investor_share,

        "investor_profit": investor_profit,

        "operator_profit": operator_profit,

        "roi": roi,

        "break_even_revenue": total_cost,

        "required_sale_price_per_m2": required_sale_price

    }

# ============================================================

# SAVE FINANCIAL ANALYSIS

# ============================================================

def save_financial_analysis(

    chat_id,

    analysis_type,

    input_data,

    result_data,

    project_id=None

):

    """

    Saves financial analysis in PostgreSQL.

    """

    try:

        if not isinstance(

            input_data,

            str

        ):

            input_data = json.dumps(

                input_data,

                ensure_ascii=False,

                default=str

            )

        if not isinstance(

            result_data,

            str

        ):

            result_data = json.dumps(

                result_data,

                ensure_ascii=False,

                default=str

            )

        row = db_execute(

            """

            INSERT INTO financial_analyses

            (

                chat_id,

                project_id,

                analysis_type,

                input_data,

                result_data

            )

            VALUES

            (

                %s, %s, %s, %s, %s

            )

            RETURNING id

            """,

            (

                int(chat_id),

                project_id,

                analysis_type,

                input_data,

                result_data

            ),

            fetchone=True,

            commit=True

        )

        if row:

            return row.get(

                "id"

            )

        return None

    except Exception:

        logger.exception(

            "save_financial_analysis failed"

        )

        return None

# ============================================================

# GET FINANCIAL ANALYSES

# ============================================================

def get_financial_analyses(

    chat_id,

    project_id=None,

    limit=50

):

    """

    Returns saved financial analyses.

    """

    try:

        if project_id is None:

            rows = db_execute(

                """

                SELECT *

                FROM financial_analyses

                WHERE chat_id = %s

                ORDER BY

                    created_at DESC,

                    id DESC

                LIMIT %s

                """,

                (

                    int(chat_id),

                    int(limit)

                ),

                fetchall=True

            )

        else:

            rows = db_execute(

                """

                SELECT *

                FROM financial_analyses

                WHERE

                    chat_id = %s

                    AND project_id = %s

                ORDER BY

                    created_at DESC,

                    id DESC

                LIMIT %s

                """,

                (

                    int(chat_id),

                    int(project_id),

                    int(limit)

                ),

                fetchall=True

            )

        return list(

            rows or []

        )

    except Exception:

        logger.exception(

            "get_financial_analyses failed"

        )

        return []

# ============================================================

# FORMAT FINANCIAL MODEL

# ============================================================

def format_financial_model(

    model

):

    """

    Converts financial model into readable Telegram text.

    """

    if not model:

        return (

            "ფინანსური მოდელის მონაცემები "

            "არ არის ხელმისაწვდომი."

        )

    def money(

        value

    ):

        return (

            f"${to_float(value, 0):,.0f}"

        )

    def percent(

        value

    ):

        return (

            f"{to_float(value, 0):,.2f}%"

        )

    lines = [

        "📊 GENIOSA — FINANCIAL ANALYSIS",

        "",

        f"💵 შემოსავალი: "

        f"{money(model.get('revenue'))}",

        f"🌍 მიწა: "

        f"{money(model.get('land_cost'))}",

        f"🔨 მშენებლობა: "

        f"{money(model.get('construction_cost'))}",

        f"⚙️ საოპერაციო ხარჯი: "

        f"{money(model.get('operating_cost'))}",

        f"🏦 ფინანსირების ხარჯი: "

        f"{money(model.get('financing_cost'))}",

        f"📦 სხვა ხარჯი: "

        f"{money(model.get('other_cost'))}",

        "",

        f"💰 სრული ხარჯი: "

        f"{money(model.get('total_cost'))}",

        f"🟢 წმინდა მოგება: "

        f"{money(model.get('net_profit'))}",

        f"📈 მოგების მარჟა: "

        f"{percent(model.get('profit_margin'))}",

        "",

        f"💼 ინვესტორის კაპიტალი: "

        f"{money(model.get('investor_capital'))}",

        f"📊 ინვესტორის წილი: "

        f"{percent(model.get('investor_share'))}",

        f"💼 ინვესტორის მოგება: "

        f"{money(model.get('investor_profit'))}",

        f"🏢 ოპერატორის მოგება: "

        f"{money(model.get('operator_profit'))}",

        f"📈 ROI: "

        f"{percent(model.get('roi'))}",

        "",

        f"⚖️ Break-even revenue: "

        f"{money(model.get('break_even_revenue'))}"

    ]

    required_price = model.get(

        "required_sale_price_per_m2"

    )

    if required_price:

        lines.append(

            f"🏷️ საჭირო საშუალო ფასი: "

            f"${required_price:,.2f}/მ²"

        )

    return "\n".join(

        lines

    )

# ============================================================

# PROJECT FINANCIAL ANALYSIS

# ============================================================

def analyze_project_financials(

    chat_id,

    project_id

):

    """

    Performs financial analysis using project data.

    """

    project = get_project(

        chat_id,

        project_id

    )

    if not project:

        return {

            "success": False,

            "error": "პროექტი ვერ მოიძებნა."

        }

    revenue = project.get(

        "expected_revenue"

    )

    if revenue is None:

        revenue = project.get(

            "revenue"

        )

    total_cost = project.get(

        "total_cost"

    )

    if total_cost is None:

        total_cost = calculate_total_cost(

            project.get(

                "land_cost"

            ),

            project.get(

                "construction_cost"

            ),

            project.get(

                "operating_cost"

            ),

            project.get(

                "financing_cost"

            ),

            project.get(

                "other_cost"

            )

        )

    profit = project.get(

        "expected_profit"

    )

    if profit is None:

        profit = project.get(

            "net_profit"

        )

    if profit is None:

        profit = calculate_profit(

            revenue,

            total_cost

        )

    investor_capital = (

        project.get(

            "investor_capital"

        )

        or 0

    )

    investor_share = (

        project.get(

            "investor_share"

        )

        or 0

    )

    model = financial_model(

        revenue=revenue or 0,

        land_cost=project.get(

            "land_cost"

        ) or 0,

        construction_cost=project.get(

            "construction_cost"

        ) or 0,

        operating_cost=project.get(

            "operating_cost"

        ) or 0,

        financing_cost=project.get(

            "financing_cost"

        ) or 0,

        other_cost=project.get(

            "other_cost"

        ) or 0,

        investor_capital=investor_capital,

        investor_share=investor_share,

        saleable_area=project.get(

            "saleable_area"

        ) or 0

    )

    # Preserve project-level expected profit

    # if explicitly supplied.

    if project.get(

        "expected_profit"

    ) is not None:

        model[

            "net_profit"

        ] = to_float(

            project.get(

                "expected_profit"

            ),

            0

        )

        model[

            "profit_margin"

        ] = calculate_profit_margin(

            model["revenue"],

            model["net_profit"]

        )

        model[

            "investor_profit"

        ] = calculate_investor_profit(

            model["net_profit"],

            model["investor_share"]

        )

        model[

            "operator_profit"

        ] = calculate_operator_profit(

            model["net_profit"],

            model["investor_share"]

        )

        model[

            "roi"

        ] = calculate_roi(

            model["net_profit"],

            model["investor_capital"]

        )

    analysis_id = save_financial_analysis(

        chat_id=chat_id,

        project_id=project_id,

        analysis_type="project_financial_analysis",

        input_data={

            "project_id": project_id,

            "project": dict(project)

        },

        result_data=model

    )

    return {

        "success": True,

        "analysis_id": analysis_id,

        "project": project,

        "model": model,

        "profit_source": (

            "expected_profit"

            if project.get(

                "expected_profit"

            ) is not None

            else "calculated"

        )

    }

# ============================================================

# SCENARIO ANALYSIS

# ============================================================

def scenario_analysis(

    revenue,

    total_cost,

    scenarios=None

):

    """

    Runs simple sensitivity analysis.

    Default scenarios:

    - 80% revenue

    - 90% revenue

    - 100% revenue

    - 110% revenue

    - 120% revenue

    """

    revenue_value = to_float(

        revenue,

        0

    )

    cost_value = to_float(

        total_cost,

        0

    )

    if scenarios is None:

        scenarios = [

            0.80,

            0.90,

            1.00,

            1.10,

            1.20

        ]

    results = []

    for multiplier in scenarios:

        multiplier_value = to_float(

            multiplier,

            None

        )

        if multiplier_value is None:

            continue

        scenario_revenue = (

            revenue_value

            * multiplier_value

        )

        scenario_profit = (

            scenario_revenue

            - cost_value

        )

        margin = calculate_profit_margin(

            scenario_revenue,

            scenario_profit

        )

        results.append(

            {

                "revenue_multiplier":

                    multiplier_value,

                "revenue":

                    scenario_revenue,

                "profit":

                    scenario_profit,

                "profit_margin":

                    margin

            }

        )

    return results

# ============================================================

# FORMAT SCENARIOS

# ============================================================

def format_scenario_analysis(

    results

):

    """

    Formats sensitivity analysis.

    """

    if not results:

        return (

            "სცენარის ანალიზი ვერ შესრულდა."

        )

    lines = [

        "📉 SENSITIVITY ANALYSIS",

        ""

    ]

    for result in results:

        multiplier = (

            result.get(

                "revenue_multiplier"

            )

            or 0

        )

        revenue = (

            result.get(

                "revenue"

            )

            or 0

        )

        profit = (

            result.get(

                "profit"

            )

            or 0

        )

        margin = (

            result.get(

                "profit_margin"

            )

            or 0

        )

        lines.append(

            f"Revenue {multiplier * 100:.0f}% "

            f"→ ${revenue:,.0f} | "

            f"Profit ${profit:,.0f} | "

            f"Margin {margin:.1f}%"

        )

    return "\n".join(

        lines

    )

# ============================================================

# PART 6 COMPLETE

# ============================================================

print(

    "GENIOSA 4.0 — PART 6/10 LOADED"

)# ============================================================

# GENIOSA 4.0 — PART 7/10

# Telegram files + documents + image/vision analysis

# ============================================================

def telegram_api(method, params=None, timeout=60):

    """

    Universal Telegram Bot API helper.

    """

    url = f"{TELEGRAM_API}/{method}"

    try:

        response = requests.post(

            url,

            json=params or {},

            timeout=timeout

        )

        if not response.ok:

            logging.error(

                "Telegram API error %s: %s",

                response.status_code,

                response.text[:1000]

            )

            return None

        data = response.json()

        if not data.get("ok"):

            logging.error(

                "Telegram API returned error: %s",

                data

            )

            return None

        return data.get("result")

    except requests.RequestException as e:

        logging.error("Telegram request failed: %s", e)

        return None

    except Exception as e:

        logging.exception("Telegram API unexpected error: %s", e)

        return None

def telegram_get_file(file_id):

    """

    Gets Telegram file metadata.

    """

    if not file_id:

        return None

    return telegram_api(

        "getFile",

        {"file_id": file_id},

        timeout=60

    )

def guess_mime_type(filename):

    """

    Determines MIME type from filename.

    """

    if not filename:

        return "application/octet-stream"

    name = filename.lower()

    mime_map = {

        ".jpg": "image/jpeg",

        ".jpeg": "image/jpeg",

        ".png": "image/png",

        ".webp": "image/webp",

        ".gif": "image/gif",

        ".bmp": "image/bmp",

        ".pdf": "application/pdf",

        ".txt": "text/plain",

        ".csv": "text/csv",

        ".docx": (

            "application/vnd.openxmlformats-officedocument."

            "wordprocessingml.document"

        ),

        ".xlsx": (

            "application/vnd.openxmlformats-officedocument."

            "spreadsheetml.sheet"

        ),

        ".xlsm": (

            "application/vnd.ms-excel.sheet.macroEnabled.12"

        ),

        ".pptx": (

            "application/vnd.openxmlformats-officedocument."

            "presentationml.presentation"

        ),

    }

    for extension, mime in mime_map.items():

        if name.endswith(extension):

            return mime

    return "application/octet-stream"

def safe_filename(filename, default_name="file"):

    """

    Makes a safe local filename.

    """

    if not filename:

        filename = default_name

    filename = os.path.basename(str(filename))

    filename = re.sub(

        r"[^a-zA-Z0-9а-яА-ЯёЁ._-]+",

        "_",

        filename

    )

    if not filename:

        filename = default_name

    return filename[:180]

def download_telegram_file(file_id, filename=None):

    """

    Downloads a Telegram file into Geniosa temporary storage.

    Returns:

        local file path or None

    """

    file_info = telegram_get_file(file_id)

    if not file_info:

        return None

    telegram_path = file_info.get("file_path")

    if not telegram_path:

        logging.error("Telegram file_path missing")

        return None

    if not filename:

        filename = os.path.basename(telegram_path)

    filename = safe_filename(filename)

    local_path = STORAGE_DIR / filename

    try:

        url = f"{TELEGRAM_FILE_API}/{telegram_path}"

        response = requests.get(

            url,

            timeout=120

        )

        if not response.ok:

            logging.error(

                "Telegram file download failed: %s",

                response.status_code

            )

            return None

        local_path.write_bytes(response.content)

        return str(local_path)

    except requests.RequestException as e:

        logging.error(

            "Telegram file download request failed: %s",

            e

        )

        return None

    except Exception as e:

        logging.exception(

            "Telegram file download unexpected error: %s",

            e

        )

        return None

def send_document(chat_id, file_path, caption=None):

    """

    Sends a generated/local file to Telegram.

    """

    if not file_path:

        return False

    if not os.path.exists(file_path):

        logging.error(

            "send_document: file does not exist: %s",

            file_path

        )

        return False

    url = f"{TELEGRAM_API}/sendDocument"

    try:

        with open(file_path, "rb") as file:

            files = {

                "document": (

                    os.path.basename(file_path),

                    file

                )

            }

            data = {

                "chat_id": str(chat_id)

            }

            if caption:

                data["caption"] = str(caption)[:1000]

            response = requests.post(

                url,

                data=data,

                files=files,

                timeout=120

            )

        if not response.ok:

            logging.error(

                "sendDocument failed: %s",

                response.text[:1000]

            )

            return False

        result = response.json()

        if not result.get("ok"):

            logging.error(

                "sendDocument Telegram error: %s",

                result

            )

            return False

        return True

    except Exception as e:

        logging.exception(

            "send_document error: %s",

            e

        )

        return False

def save_uploaded_document(

    chat_id,

    filename,

    file_path,

    project_id=None,

    user_request=None

):

    """

    Downloads/extracts/analyzes a Telegram document.

    """

    if not file_path:

        return {

            "success": False,

            "message": "ფაილის ჩამოტვირთვა ვერ მოხერხდა."

        }

    try:

        result = process_document_file(

            chat_id=chat_id,

            file_path=file_path,

            filename=filename,

            project_id=project_id,

            user_request=user_request

        )

        return result

    except Exception as e:

        logging.exception(

            "save_uploaded_document error: %s",

            e

        )

        return {

            "success": False,

            "message": f"დოკუმენტის დამუშავების შეცდომა: {e}"

        }

def analyze_image_with_ai(

    filename,

    image_bytes,

    mime_type="image/jpeg",

    user_request=None

):

    """

    Sends an image to Gemini Vision and returns business-oriented analysis.

    """

    if not image_bytes:

        return "სურათი ცარიელია ან ვერ წავიკითხე."

    if len(image_bytes) > 12 * 1024 * 1024:

        return (

            "სურათი ძალიან დიდია. გთხოვ, გამოგზავნე "

            "12 MB-ზე ნაკლები ზომის ფაილი."

        )

    encoded_image = base64.b64encode(image_bytes).decode("utf-8")

    request_text = user_request or (

        "გაანალიზე ეს სურათი პროფესიონალურად. "

        "თუ სურათი ეხება უძრავ ქონებას, სამშენებლო პროექტს, "

        "არქიტექტურას, ფინანსურ დოკუმენტს, ობიექტს ან ბიზნესს, "

        "გამოყავი პრაქტიკული და საინვესტიციო მნიშვნელობის ინფორმაცია. "

        "არ მოიგონო ფაქტები. რაც ზუსტად არ ჩანს, მიუთითე როგორც "

        "შეფასება ან ვარაუდი."

    )

    prompt = f"""

ფაილის სახელი: {filename}

მომხმარებლის მოთხოვნა:

{request_text}

უპასუხე მომხმარებლის ენაზე.

თუ სურათიდან შესაძლებელია ტექსტის წაკითხვა, ამოიღე მნიშვნელოვანი ტექსტიც.

თუ შესაძლებელია ფართობების, ფასების, რაოდენობების ან სხვა ციფრების დანახვა,

მიუთითე ისინი მკაფიოდ და არ შეცვალო ერთეულები.

ანალიზი იყოს პრაქტიკული, სტრუქტურირებული და პროფესიონალური.

"""

    payload = {

        "systemInstruction": {

            "parts": [

                {

                    "text": ai_system_prompt()

                }

            ]

        },

        "contents": [

            {

                "role": "user",

                "parts": [

                    {

                        "text": prompt

                    },

                    {

                        "inlineData": {

                            "mimeType": mime_type,

                            "data": encoded_image

                        }

                    }

                ]

            }

        ],

        "generationConfig": {

            "temperature": 0.3,

            "maxOutputTokens": 5000

        }

    }

    url = gemini_url()

    try:

        response = requests.post(

            url,

            headers={

                "Content-Type": "application/json"

            },

            json=payload,

            timeout=120

        )

        if not response.ok:

            logging.error(

                "Gemini vision error %s: %s",

                response.status_code,

                response.text[:1500]

            )

            return (

                "სურათის AI ანალიზი ვერ შესრულდა.\n"

                f"Gemini პასუხი: HTTP {response.status_code}"

            )

        data = response.json()

        text_result = extract_gemini_text(data)

        if not text_result:

            return (

                "Gemini-მ სურათი მიიღო, მაგრამ "

                "ანალიზის ტექსტური პასუხი ვერ დააბრუნა."

            )

        return text_result

    except requests.Timeout:

        return (

            "სურათის ანალიზს ძალიან დიდი დრო დასჭირდა. "

            "გთხოვ, სცადე ხელახლა."

        )

    except requests.RequestException as e:

        logging.error(

            "Gemini vision network error: %s",

            e

        )

        return (

            "Gemini-სთან დაკავშირება ვერ მოხერხდა "

            "სურათის ანალიზის დროს."

        )

    except Exception as e:

        logging.exception(

            "analyze_image_with_ai error: %s",

            e

        )

        return (

            "სურათის ანალიზის დროს მოხდა ტექნიკური შეცდომა."

        )

def analyze_telegram_image(

    chat_id,

    file_id,

    filename="image.jpg",

    user_request=None

):

    """

    Downloads Telegram image and analyzes it with Gemini Vision.

    """

    local_path = download_telegram_file(

        file_id=file_id,

        filename=filename

    )

    if not local_path:

        return (

            "❌ სურათის ჩამოტვირთვა ვერ მოხერხდა."

        )

    try:

        with open(local_path, "rb") as file:

            image_bytes = file.read()

        mime_type = guess_mime_type(filename)

        analysis = analyze_image_with_ai(

            filename=filename,

            image_bytes=image_bytes,

            mime_type=mime_type,

            user_request=user_request

        )

        return analysis

    except Exception as e:

        logging.exception(

            "analyze_telegram_image error: %s",

            e

        )

        return (

            "❌ სურათის დამუშავებისას მოხდა შეცდომა."

        )

    finally:

        try:

            if os.path.exists(local_path):

                os.remove(local_path)

        except Exception:

            pass

def get_best_photo_file_id(photo_sizes):

    """

    Telegram photo contains several resolutions.

    Select the largest one.

    """

    if not photo_sizes:

        return None

    try:

        best = max(

            photo_sizes,

            key=lambda item: (

                int(item.get("width", 0))

                * int(item.get("height", 0))

            )

        )

        return best.get("file_id")

    except Exception:

        try:

            return photo_sizes[-1].get("file_id")

        except Exception:

            return None

def build_image_request(caption):

    """

    Converts Telegram caption into an image-analysis request.

    """

    if caption:

        return caption.strip()

    return (

        "გაანალიზე ეს სურათი დეტალურად ბიზნესის, "

        "უძრავი ქონების და საინვესტიციო პერსპექტივიდან, "

        "თუ ასეთი ინფორმაცია ჩანს."

    )

def handle_photo_message(chat_id, message):

    """

    Handles Telegram photo messages.

    """

    photo_sizes = message.get("photo") or []

    file_id = get_best_photo_file_id(photo_sizes)

    if not file_id:

        send_message(

            chat_id,

            "❌ ფოტოს ფაილის მიღება ვერ მოხერხდა."

        )

        return

    caption = message.get("caption") or ""

    send_message(

        chat_id,

        "🖼️ ფოტო მივიღე.\n"

        "ვიწყებ AI ანალიზს..."

    )

    analysis_request = build_image_request(caption)

    filename = f"telegram_image_{int(time.time())}.jpg"

    analysis = analyze_telegram_image(

        chat_id=chat_id,

        file_id=file_id,

        filename=filename,

        user_request=analysis_request

    )

    save_message(

        chat_id,

        "user",

        f"[ფოტო] {caption}".strip()

    )

    save_message(

        chat_id,

        "assistant",

        analysis

    )

    send_long_message(

        chat_id,

        "🖼️ **ფოტოს AI ანალიზი**\n\n" + analysis

    )

def build_document_request(caption):

    """

    Converts Telegram document caption into analysis request.

    """

    if caption:

        return caption.strip()

    return (

        "გაანალიზე ეს დოკუმენტი სრულად. "

        "გამოყავი მნიშვნელოვანი ფინანსური, კომერციული, "

        "სამშენებლო, იურიდიული ან საინვესტიციო ინფორმაცია "

        "იმდენად, რამდენადაც დოკუმენტიდან ამის დადგენა შესაძლებელია."

    )

def handle_document_message(chat_id, message):

    """

    Handles Telegram document uploads.

    """

    document = message.get("document") or {}

    file_id = document.get("file_id")

    if not file_id:

        send_message(

            chat_id,

            "❌ დოკუმენტის ფაილის ID ვერ მივიღე."

        )

        return

    original_filename = (

        document.get("file_name")

        or f"document_{int(time.time())}"

    )

    filename = safe_filename(original_filename)

    caption = message.get("caption") or ""

    send_message(

        chat_id,

        "📄 დოკუმენტი მივიღე.\n"

        "ვტვირთავ და ვამზადებ AI ანალიზისთვის..."

    )

    local_path = download_telegram_file(

        file_id=file_id,

        filename=filename

    )

    if not local_path:

        send_message(

            chat_id,

            "❌ დოკუმენტის ჩამოტვირთვა ვერ მოხერხდა."

        )

        return

    try:

        user_request = build_document_request(

            caption

        )

        result = save_uploaded_document(

            chat_id=chat_id,

            filename=filename,

            file_path=local_path,

            project_id=None,

            user_request=user_request

        )

        if isinstance(result, dict):

            success = result.get("success", False)

            if success:

                document_id = result.get("document_id")

                analysis = result.get("analysis") or ""

                header = "📄 **დოკუმენტის ანალიზი**"

                if document_id:

                    header += (

                        f"\nDocument ID: {document_id}"

                    )

                send_long_message(

                    chat_id,

                    header + "\n\n" + analysis

                )

            else:

                send_long_message(

                    chat_id,

                    "❌ დოკუმენტის დამუშავება ვერ მოხერხდა.\n\n"

                    + str(

                        result.get(

                            "message",

                            "უცნობი შეცდომა"

                        )

                    )

                )

        else:

            send_long_message(

                chat_id,

                str(result)

            )

    except Exception as e:

        logging.exception(

            "handle_document_message error: %s",

            e

        )

        send_message(

            chat_id,

            "❌ დოკუმენტის დამუშავებისას მოხდა ტექნიკური შეცდომა."

        )

    finally:

        try:

            if os.path.exists(local_path):

                os.remove(local_path)

        except Exception:

            pass

def get_telegram_file_size(message):

    """

    Returns known Telegram file size when available.

    """

    document = message.get("document") or {}

    try:

        return int(

            document.get("file_size") or 0

        )

    except Exception:

        return 0

def validate_document_size(message):

    """

    Prevents unnecessarily large document processing.

    Telegram Bot API can provide file_size for documents.

    """

    size = get_telegram_file_size(message)

    if size <= 0:

        return True

    max_size = 20 * 1024 * 1024

    return size <= max_size

print("GENIOSA 4.0 — PART 7/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 8A/10

# Telegram commands

# ============================================================

def safe_int(value, default=None):

    try:

        return int(value)

    except Exception:

        return default

def extract_command_args(text):

    if not text:

        return ""

    parts = text.strip().split(maxsplit=1)

    if len(parts) == 1:

        return ""

    return parts[1].strip()

def command_name(text):

    if not text:

        return ""

    first = text.strip().split()[0]

    if not first.startswith("/"):

        return ""

    first = first[1:]

    if "@" in first:

        first = first.split("@", 1)[0]

    return first.lower()

def help_text():

    return """

🤖 GENIOSA 4.0 — Business & Investment Advisor

🏗️ პროექტები

• პროექტების შექმნა და მართვა

• ფართობები და ღირებულებები

• შემოსავლები და მოგება

• ფინანსური ანალიზი

💼 ინვესტორები

• ინვესტორების ბაზა

• საკონტაქტო ინფორმაცია

• საინვესტიციო შესაძლებლობები

🤝 Deals / CRM

• პროექტი + ინვესტორი

• შეთავაზება

• ინვესტორის წილი

• შეფასება

• შემდეგი ნაბიჯი

📊 ფინანსები

• ROI

• Profit

• Margin

• Break-even

• საჭირო გასაყიდი ფასი

• სცენარების ანალიზი

📄 დოკუმენტები

• PDF

• DOCX

• XLSX / XLSM

• PPTX

• TXT

• CSV

🖼️ სურათები

• ფოტოს AI ანალიზი

• არქიტექტურული მასალები

• ცხრილები და დიაგრამები

• უძრავი ქონების ვიზუალური ანალიზი

ძირითადი ბრძანებები:

/start

/help

/status

/memory

/projects

/project ID

/investors

/investor ID

/deals

/deal ID

/documents

/finance

/finance ID

ან უბრალოდ მომწერე ჩვეულებრივი ტექსტით.

"""

def start_text():

    return """

🤖 GENIOSA 4.0

მოგესალმები.

მე ვარ შენი პირადი Business & Investment Advisor.

შემიძლია დაგეხმარო:

🏗️ სამშენებლო და დეველოპერულ პროექტებში

💰 ფინანსურ ანალიზში

🤝 ინვესტორებთან მუშაობაში

📊 ROI / Profit / Break-even ანალიზში

📄 დოკუმენტების ანალიზში

🖼️ ფოტოების ანალიზში

🧠 ბიზნეს-მეხსიერებაში

უბრალოდ მომწერე შენი მოთხოვნა.

მაგალითად:

„მაჩვენე ჩემი პროექტები“

„გამოთვალე ამ პროექტის ROI“

„დაამატე ახალი ინვესტორი“

„გაანალიზე ეს დოკუმენტი“

/help — ყველა ფუნქცია

"""

def status_text():

    telegram_ok = bool(TELEGRAM_BOT_TOKEN)

    database_ok = database_is_available()

    gemini_ok = bool(GEMINI_API_KEY)

    return (

        "🟢 GENIOSA STATUS\n\n"

        f"Telegram: {'✅' if telegram_ok else '❌'}\n"

        f"Database: {'✅' if database_ok else '❌'}\n"

        f"Gemini: {'✅' if gemini_ok else '❌'}\n"

        "Application: ✅"

    )

def memory_command(chat_id):

    memories = get_memories(

        chat_id,

        limit=50

    )

    if not memories:

        return (

            "🧠 შენახული ბიზნეს-მეხსიერება ჯერ არ არის."

        )

    lines = [

        "🧠 შენახული მეხსიერება",

        ""

    ]

    for memory in memories:

        memory_id = memory.get("id")

        category = memory.get("category") or "general"

        importance = memory.get("importance") or 0

        text_value = memory.get("memory") or ""

        lines.append(

            f"#{memory_id} [{category}] "

            f"(მნიშვნელობა: {importance})"

        )

        lines.append(text_value)

        lines.append("")

    return "\n".join(lines)

def projects_command(chat_id):

    projects = get_projects(

        chat_id,

        limit=50

    )

    if not projects:

        return (

            "🏗️ პროექტები ჯერ არ არის.\n\n"

            "მაგალითი:\n"

            "„შემიქმენი პროექტი Nikkea 12“"

        )

    return projects_summary(chat_id)

def investors_command(chat_id):

    investors = get_investors(

        chat_id,

        limit=100

    )

    if not investors:

        return (

            "💼 ინვესტორები ჯერ არ არის.\n\n"

            "მაგალითი:\n"

            "„დაამატე ინვესტორი John Smith, კომპანია ABC, UAE“"

        )

    return investors_summary(chat_id)

def deals_command(chat_id):

    deals = get_deals(

        chat_id,

        limit=100

    )

    if not deals:

        return "🤝 გარიგებები ჯერ არ არის."

    return deals_summary(chat_id)

def documents_command(chat_id):

    documents = get_documents(

        chat_id,

        limit=50

    )

    if not documents:

        return (

            "📄 დოკუმენტები ჯერ არ არის.\n\n"

            "გამომიგზავნე PDF, DOCX, XLSX, PPTX, "

            "TXT ან CSV ფაილი."

        )

    return documents_summary(chat_id)

def finance_command(chat_id, args):

    args = (args or "").strip()

    if not args:

        projects = get_projects(

            chat_id,

            limit=50

        )

        if not projects:

            return (

                "📊 ფინანსური ანალიზისთვის ჯერ შექმენი პროექტი."

            )

        lines = [

            "📊 ფინანსური ანალიზი",

            "",

            "მიუთითე Project ID:",

            ""

        ]

        for project in projects:

            lines.append(

                f"#{project.get('id')} — "

                f"{project.get('name')}"

            )

        lines.extend([

            "",

            "მაგალითი:",

            "/finance 1"

        ])

        return "\n".join(lines)

    project_id = safe_int(

        args.split()[0]

    )

    if not project_id:

        return (

            "❌ Project ID უნდა იყოს რიცხვი.\n"

            "მაგალითი: /finance 1"

        )

    model = analyze_project_financials(

        chat_id,

        project_id

    )

    if isinstance(model, dict) and model.get("error"):

        return (

            "❌ ფინანსური ანალიზი ვერ შესრულდა.\n\n"

            + str(model.get("error"))

        )

    return (

        "📊 PROJECT FINANCIAL ANALYSIS\n\n"

        + format_financial_model(model)

    )

def project_command(chat_id, args):

    args = (args or "").strip()

    if not args:

        return projects_command(chat_id)

    project_id = safe_int(

        args.split()[0]

    )

    if not project_id:

        return "❌ Project ID უნდა იყოს რიცხვი."

    project = get_project(

        chat_id,

        project_id

    )

    if not project:

        return (

            f"❌ პროექტი #{project_id} ვერ მოიძებნა."

        )

    lines = [

        project_summary(project)

    ]

    deals = get_project_deals(

        chat_id,

        project_id

    )

    if deals:

        lines.append("")

        lines.append("🤝 დაკავშირებული Deals:")

        for deal in deals:

            lines.append(

                format_deal(deal)

            )

    return "\n".join(lines)

def investor_command(chat_id, args):

    args = (args or "").strip()

    if not args:

        return investors_command(chat_id)

    investor_id = safe_int(

        args.split()[0]

    )

    if not investor_id:

        return "❌ Investor ID უნდა იყოს რიცხვი."

    investor = get_investor(

        chat_id,

        investor_id

    )

    if not investor:

        return (

            f"❌ ინვესტორი #{investor_id} ვერ მოიძებნა."

        )

    lines = [

        format_investor(investor)

    ]

    deals = [

        deal

        for deal in get_deals(chat_id, limit=100)

        if deal.get("investor_id") == investor_id

    ]

    if deals:

        lines.append("")

        lines.append("🤝 Deals:")

        for deal in deals:

            lines.append(

                format_deal(deal)

            )

    return "\n".join(lines)

def deal_command(chat_id, args):

    args = (args or "").strip()

    if not args:

        return deals_command(chat_id)

    deal_id = safe_int(

        args.split()[0]

    )

    if not deal_id:

        return "❌ Deal ID უნდა იყოს რიცხვი."

    deal = get_deal(

        chat_id,

        deal_id

    )

    if not deal:

        return (

            f"❌ Deal #{deal_id} ვერ მოიძებნა."

        )

    return format_deal(deal)

print("GENIOSA 4.0 — PART 8A/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 8B/10

# Natural language commands + text message handling

# ============================================================

def parse_key_value_text(text):

    """

    ამოიცნობს მარტივ key:value ან key=value ფორმატს.

    გამოიყენება პროექტებისა და ინვესტორების შექმნისას.

    """

    result = {}

    if not text:

        return result

    parts = re.split(

        r"[;\n]+",

        text

    )

    for part in parts:

        part = part.strip()

        if not part:

            continue

        match = re.match(

            r"^\s*([^:=]+?)\s*[:=]\s*(.+?)\s*$",

            part

        )

        if not match:

            continue

        key = match.group(1).strip().lower()

        value = match.group(2).strip()

        result[key] = value

    return result

def first_number(text):

    if not text:

        return None

    match = re.search(

        r"-?\d+(?:[.,]\d+)?",

        str(text)

    )

    if not match:

        return None

    try:

        return float(

            match.group(0).replace(",", ".")

        )

    except Exception:

        return None

def normalize_text_value(value):

    if value is None:

        return None

    value = str(value).strip()

    if not value:

        return None

    return value

def create_project_from_text(chat_id, text):

    """

    ცდილობს მომხმარებლის ჩვეულებრივი ტექსტიდან

    პროექტის შექმნას.

    """

    text = (text or "").strip()

    if not text:

        return (

            "❌ პროექტის ინფორმაცია არ არის მითითებული."

        )

    data = parse_key_value_text(text)

    name = (

        data.get("name")

        or data.get("project")

        or data.get("პროექტი")

        or data.get("სახელი")

    )

    if not name:

        match = re.search(

            r"(?:პროექტი|project)\s*[:\-]?\s*(.+)",

            text,

            re.IGNORECASE

        )

        if match:

            name = match.group(1).strip()

    if not name:

        name = text[:120]

    industry = (

        data.get("industry")

        or data.get("sector")

        or data.get("ინდუსტრია")

        or data.get("სექტორი")

        or "real_estate"

    )

    location = (

        data.get("location")

        or data.get("city")

        or data.get("ადგილმდებარეობა")

    )

    description = (

        data.get("description")

        or data.get("აღწერა")

        or text

    )

    land_area = (

        first_number(data.get("land_area"))

        or first_number(data.get("land"))

        or first_number(data.get("მიწა"))

    )

    saleable_area = (

        first_number(data.get("saleable_area"))

        or first_number(data.get("saleable"))

        or first_number(data.get("გასაყიდი"))

    )

    construction_area = (

        first_number(data.get("construction_area"))

        or first_number(data.get("construction"))

        or first_number(data.get("მშენებლობა"))

    )

    total_area = (

        first_number(data.get("total_area"))

        or first_number(data.get("total"))

        or first_number(data.get("საერთო"))

    )

    revenue = (

        first_number(data.get("revenue"))

        or first_number(data.get("შემოსავალი"))

    )

    total_cost = (

        first_number(data.get("total_cost"))

        or first_number(data.get("cost"))

        or first_number(data.get("ღირებულება"))

    )

    net_profit = (

        first_number(data.get("net_profit"))

        or first_number(data.get("profit"))

        or first_number(data.get("მოგება"))

    )

    investor_capital = (

        first_number(data.get("investor_capital"))

        or first_number(data.get("capital"))

        or first_number(data.get("ინვესტიცია"))

    )

    investor_profit = (

        first_number(data.get("investor_profit"))

    )

    investor_share = (

        first_number(data.get("investor_share"))

        or first_number(data.get("share"))

        or first_number(data.get("წილი"))

    )

    land_cost = (

        first_number(data.get("land_cost"))

        or first_number(data.get("land_price"))

    )

    construction_cost = (

        first_number(data.get("construction_cost"))

    )

    financing_cost = (

        first_number(data.get("financing_cost"))

    )

    other_cost = (

        first_number(data.get("other_cost"))

    )

    expected_revenue = (

        first_number(data.get("expected_revenue"))

    )

    expected_profit = (

        first_number(data.get("expected_profit"))

    )

    notes = (

        data.get("notes")

        or data.get("note")

        or data.get("შენიშვნა")

    )

    try:

        project_id = create_project(

            chat_id=chat_id,

            name=name,

            industry=industry,

            location=location,

            description=description,

            land_area=land_area,

            saleable_area=saleable_area,

            construction_area=construction_area,

            total_area=total_area,

            revenue=revenue,

            total_cost=total_cost,

            operating_cost=None,

            net_profit=net_profit,

            investor_capital=investor_capital,

            investor_profit=investor_profit,

            investor_share=investor_share,

            status="active",

            land_cost=land_cost,

            construction_cost=construction_cost,

            financing_cost=financing_cost,

            other_cost=other_cost,

            expected_revenue=expected_revenue,

            expected_profit=expected_profit,

            notes=notes

        )

        project = get_project(

            chat_id,

            project_id

        )

        return (

            "✅ პროექტი შეიქმნა.\n\n"

            + project_summary(project)

        )

    except TypeError:

        # თავსებადობა იმ შემთხვევისთვის,

        # თუ create_project-ის ძველი signature გამოიყენება.

        try:

            project_id = create_project(

                chat_id=chat_id,

                name=name,

                industry=industry,

                location=location,

                description=description,

                land_area=land_area,

                saleable_area=saleable_area,

                construction_area=construction_area,

                total_area=total_area,

                revenue=revenue,

                total_cost=total_cost,

                operating_cost=None,

                net_profit=net_profit,

                investor_capital=investor_capital,

                investor_profit=investor_profit,

                investor_share=investor_share,

                status="active"

            )

            project = get_project(

                chat_id,

                project_id

            )

            return (

                "✅ პროექტი შეიქმნა.\n\n"

                + project_summary(project)

            )

        except Exception as exc:

            logging.exception(

                "Project creation failed"

            )

            return (

                "❌ პროექტის შექმნა ვერ მოხერხდა.\n\n"

                + str(exc)

            )

    except Exception as exc:

        logging.exception(

            "Project creation failed"

        )

        return (

            "❌ პროექტის შექმნა ვერ მოხერხდა.\n\n"

            + str(exc)

        )

def create_investor_from_text(chat_id, text):

    """

    ცდილობს ჩვეულებრივი ტექსტიდან ინვესტორის შექმნას.

    """

    text = (text or "").strip()

    if not text:

        return (

            "❌ ინვესტორის ინფორმაცია არ არის მითითებული."

        )

    data = parse_key_value_text(text)

    name = (

        data.get("name")

        or data.get("investor")

        or data.get("სახელი")

        or data.get("ინვესტორი")

    )

    if not name:

        match = re.search(

            r"(?:ინვესტორი|investor)\s*[:\-]?\s*(.+)",

            text,

            re.IGNORECASE

        )

        if match:

            name = match.group(1).strip()

    if not name:

        name = text[:120]

    company = (

        data.get("company")

        or data.get("კომპანია")

    )

    country = (

        data.get("country")

        or data.get("ქვეყანა")

    )

    contact = (

        data.get("contact")

        or data.get("email")

        or data.get("phone")

        or data.get("კონტაქტი")

    )

    investment_capacity = (

        first_number(

            data.get("investment_capacity")

        )

        or first_number(

            data.get("capacity")

        )

        or first_number(

            data.get("ინვესტიციის_უნარი")

        )

    )

    preferred_sector = (

        data.get("preferred_sector")

        or data.get("sector")

        or data.get("industry")

        or data.get("სექტორი")

        or data.get("ინდუსტრია")

    )

    status = (

        data.get("status")

        or data.get("სტატუსი")

        or "new"

    )

    notes = (

        data.get("notes")

        or data.get("note")

        or data.get("შენიშვნა")

        or text

    )

    try:

        investor_id = create_investor(

            chat_id=chat_id,

            name=name,

            company=company,

            country=country,

            contact=contact,

            investment_capacity=investment_capacity,

            preferred_sector=preferred_sector,

            status=status,

            notes=notes

        )

        investor = get_investor(

            chat_id,

            investor_id

        )

        return (

            "✅ ინვესტორი შეიქმნა.\n\n"

            + format_investor(investor)

        )

    except TypeError:

        try:

            investor_id = create_investor(

                chat_id=chat_id,

                name=name,

                company=company,

                country=country,

                contact=contact

            )

            investor = get_investor(

                chat_id,

                investor_id

            )

            return (

                "✅ ინვესტორი შეიქმნა.\n\n"

                + format_investor(investor)

            )

        except Exception as exc:

            logging.exception(

                "Investor creation failed"

            )

            return (

                "❌ ინვესტორის შექმნა ვერ მოხერხდა.\n\n"

                + str(exc)

            )

    except Exception as exc:

        logging.exception(

            "Investor creation failed"

        )

        return (

            "❌ ინვესტორის შექმნა ვერ მოხერხდა.\n\n"

            + str(exc)

        )

def handle_command(chat_id, text):

    """

    Telegram command router.

    """

    name = command_name(text)

    args = extract_command_args(text)

    if name == "start":

        return start_text()

    if name == "help":

        return help_text()

    if name == "status":

        return status_text()

    if name == "memory":

        return memory_command(chat_id)

    if name == "projects":

        return projects_command(chat_id)

    if name == "project":

        return project_command(

            chat_id,

            args

        )

    if name == "investors":

        return investors_command(chat_id)

    if name == "investor":

        return investor_command(

            chat_id,

            args

        )

    if name == "deals":

        return deals_command(chat_id)

    if name == "deal":

        return deal_command(

            chat_id,

            args

        )

    if name == "documents":

        return documents_command(chat_id)

    if name == "finance":

        return finance_command(

            chat_id,

            args

        )

    return None

def looks_like_project_request(text):

    if not text:

        return False

    lower = text.lower()

    keywords = [

        "შემიქმენი პროექტი",

        "შექმენი პროექტი",

        "დაამატე პროექტი",

        "ახალი პროექტი",

        "create project",

        "new project",

        "add project"

    ]

    return any(

        keyword in lower

        for keyword in keywords

    )

def looks_like_investor_request(text):

    if not text:

        return False

    lower = text.lower()

    keywords = [

        "დაამატე ინვესტორი",

        "შექმენი ინვესტორი",

        "ახალი ინვესტორი",

        "შემიქმენი ინვესტორი",

        "add investor",

        "create investor",

        "new investor"

    ]

    return any(

        keyword in lower

        for keyword in keywords

    )

def handle_text_message(chat_id, text):

    """

    მთავარი ტექსტური დამუშავება.

    პრიორიტეტი:

    1. Telegram commands

    2. Project creation

    3. Investor creation

    4. ჩვეულებრივი AI კითხვა

    """

    text = (text or "").strip()

    if not text:

        return "❌ ცარიელი შეტყობინებაა."

    # --------------------------------------------------------

    # 1. Telegram command

    # --------------------------------------------------------

    if text.startswith("/"):

        result = handle_command(

            chat_id,

            text

        )

        if result is not None:

            return result

        return (

            "❌ უცნობი ბრძანებაა.\n\n"

            "გამოიყენე /help"

        )

    # --------------------------------------------------------

    # 2. Project creation

    # --------------------------------------------------------

    if looks_like_project_request(text):

        return create_project_from_text(

            chat_id,

            text

        )

    # --------------------------------------------------------

    # 3. Investor creation

    # --------------------------------------------------------

    if looks_like_investor_request(text):

        return create_investor_from_text(

            chat_id,

            text

        )

    # --------------------------------------------------------

    # 4. Save user message

    # --------------------------------------------------------

    try:

        save_message(

            chat_id=chat_id,

            role="user",

            content=text

        )

    except Exception:

        logging.exception(

            "Could not save user message"

        )

    # --------------------------------------------------------

    # 5. Build business context

    # --------------------------------------------------------

    try:

        context = build_full_ai_context(

            chat_id

        )

    except Exception:

        logging.exception(

            "Could not build AI context"

        )

        context = ""

    # --------------------------------------------------------

    # 6. Build prompt

    # --------------------------------------------------------

    try:

        prompt = build_ai_prompt(

            chat_id,

            text,

            context=context

        )

    except TypeError:

        try:

            prompt = build_ai_prompt(

                chat_id,

                text

            )

        except Exception:

            prompt = text

    except Exception:

        logging.exception(

            "Could not build AI prompt"

        )

        prompt = text

    # --------------------------------------------------------

    # 7. Gemini

    # --------------------------------------------------------

    try:

        ai_response = gemini_generate(

            prompt,

            system_instruction=ai_system_prompt()

        )

    except TypeError:

        try:

            ai_response = gemini_generate(

                prompt

            )

        except Exception as exc:

            logging.exception(

                "Gemini generation failed"

            )

            return (

                "❌ AI პასუხის მიღება ვერ მოხერხდა.\n\n"

                + str(exc)

            )

    except Exception as exc:

        logging.exception(

            "Gemini generation failed"

        )

        return (

            "❌ AI პასუხის მიღება ვერ მოხერხდა.\n\n"

            + str(exc)

        )

    # --------------------------------------------------------

    # 8. Normalize AI response

    # --------------------------------------------------------

    if isinstance(ai_response, dict):

        response_text = extract_gemini_text(

            ai_response

        )

        if not response_text:

            error_text = extract_gemini_error(

                ai_response

            )

            response_text = (

                "❌ Gemini-მ პასუხი ვერ დააბრუნა."

            )

            if error_text:

                response_text += (

                    "\n\n" + error_text

                )

    else:

        response_text = str(

            ai_response or ""

        )

    response_text = response_text.strip()

    if not response_text:

        response_text = (

            "❌ ცარიელი პასუხი მივიღე AI-სგან."

        )

    # --------------------------------------------------------

    # 9. Process AI text

    # --------------------------------------------------------

    try:

        processed_response = process_ai_text(

            chat_id,

            text,

            response_text

        )

        if processed_response:

            response_text = processed_response

    except Exception:

        logging.exception(

            "AI post-processing failed"

        )

    # --------------------------------------------------------

    # 10. Save assistant message

    # --------------------------------------------------------

    try:

        save_message(

            chat_id=chat_id,

            role="assistant",

            content=response_text

        )

    except Exception:

        logging.exception(

            "Could not save assistant message"

        )

    # --------------------------------------------------------

    # 11. Important memory detection

    # --------------------------------------------------------

    try:

        maybe_save_important_memory(

            chat_id,

            text,

            response_text

        )

    except Exception:

        logging.exception(

            "Memory processing failed"

        )

    return response_text

print("GENIOSA 4.0 — PART 8B/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 9/10

# Excel + PowerPoint generation

# ============================================================

def generated_asset_save(

    chat_id,

    project_id,

    asset_type,

    filename,

    description=""

):

    """

    ინახავს გენერირებულ ფაილს generated_assets ცხრილში.

    """

    try:

        with db() as conn:

            with conn.cursor() as cur:

                cur.execute(

                    """

                    INSERT INTO generated_assets

                    (

                        chat_id,

                        project_id,

                        asset_type,

                        filename,

                        description

                    )

                    VALUES (%s, %s, %s, %s, %s)

                    RETURNING id

                    """,

                    (

                        chat_id,

                        project_id,

                        asset_type,

                        filename,

                        description

                    )

                )

                row = cur.fetchone()

                return row[0] if row else None

    except Exception:

        logging.exception(

            "Could not save generated asset"

        )

        return None

def format_money(value):

    try:

        number = float(value or 0)

        return f"${number:,.0f}"

    except Exception:

        return "$0"

def format_number(value):

    try:

        number = float(value or 0)

        if number.is_integer():

            return f"{int(number):,}"

        return f"{number:,.2f}"

    except Exception:

        return "0"

def generate_project_excel(

    chat_id,

    project_id

):

    """

    ქმნის პროექტის ფინანსურ Excel მოდელს.

    """

    project = get_project(

        chat_id,

        project_id

    )

    if not project:

        return {

            "error": f"პროექტი #{project_id} ვერ მოიძებნა."

        }

    try:

        model = analyze_project_financials(

            chat_id,

            project_id

        )

        if isinstance(model, dict) and model.get("error"):

            return model

        workbook = Workbook()

        ws = workbook.active

        ws.title = "Project Summary"

        ws["A1"] = "GENIOSA 4.0"

        ws["A2"] = "Project Financial Model"

        ws["A4"] = "Project ID"

        ws["B4"] = project.get("id")

        ws["A5"] = "Project Name"

        ws["B5"] = project.get("name")

        ws["A6"] = "Industry"

        ws["B6"] = industry_name(

            project.get("industry")

        )

        ws["A7"] = "Location"

        ws["B7"] = project.get("location") or ""

        ws["A9"] = "AREA"

        ws["B9"] = "VALUE"

        area_rows = [

            (

                "Land Area (m²)",

                project.get("land_area")

            ),

            (

                "Construction Area (m²)",

                project.get("construction_area")

            ),

            (

                "Saleable Area (m²)",

                project.get("saleable_area")

            ),

            (

                "Total Area (m²)",

                project.get("total_area")

            )

        ]

        row_number = 10

        for label, value in area_rows:

            ws.cell(

                row=row_number,

                column=1,

                value=label

            )

            ws.cell(

                row=row_number,

                column=2,

                value=float(value or 0)

            )

            row_number += 1

        row_number += 1

        ws.cell(

            row=row_number,

            column=1,

            value="FINANCIALS"

        )

        ws.cell(

            row=row_number,

            column=2,

            value="VALUE"

        )

        row_number += 1

        financial_rows = [

            (

                "Land Cost",

                model.get("land_cost")

            ),

            (

                "Construction Cost",

                model.get("construction_cost")

            ),

            (

                "Financing Cost",

                model.get("financing_cost")

            ),

            (

                "Other Cost",

                model.get("other_cost")

            ),

            (

                "Total Cost",

                model.get("total_cost")

            ),

            (

                "Expected Revenue",

                model.get("expected_revenue")

            ),

            (

                "Expected Profit",

                model.get("expected_profit")

            ),

            (

                "Profit Margin (%)",

                model.get("profit_margin")

            ),

            (

                "ROI (%)",

                model.get("roi")

            ),

            (

                "Investor Capital",

                model.get("investor_capital")

            ),

            (

                "Investor Profit",

                model.get("investor_profit")

            ),

            (

                "Investor Share (%)",

                model.get("investor_share")

            ),

            (

                "Operator Profit",

                model.get("operator_profit")

            ),

            (

                "Break-even Revenue",

                model.get("break_even_revenue")

            ),

            (

                "Required Sale Price / m²",

                model.get("required_sale_price")

            )

        ]

        for label, value in financial_rows:

            ws.cell(

                row=row_number,

                column=1,

                value=label

            )

            try:

                numeric_value = float(value or 0)

            except Exception:

                numeric_value = 0

            ws.cell(

                row=row_number,

                column=2,

                value=numeric_value

            )

            row_number += 1

        # ----------------------------------------------------

        # Scenario sheet

        # ----------------------------------------------------

        scenario_sheet = workbook.create_sheet(

            "Scenarios"

        )

        scenario_sheet["A1"] = "SCENARIO ANALYSIS"

        scenarios = scenario_analysis(

            chat_id,

            project_id

        )

        scenario_sheet["A3"] = "Scenario"

        scenario_sheet["B3"] = "Revenue"

        scenario_sheet["C3"] = "Cost"

        scenario_sheet["D3"] = "Profit"

        scenario_sheet["E3"] = "Margin %"

        scenario_sheet["F3"] = "ROI %"

        row_number = 4

        if isinstance(scenarios, dict):

            scenario_items = scenarios.get(

                "scenarios",

                scenarios

            )

            if isinstance(

                scenario_items,

                dict

            ):

                for scenario_name, scenario in scenario_items.items():

                    if not isinstance(

                        scenario,

                        dict

                    ):

                        continue

                    scenario_sheet.cell(

                        row=row_number,

                        column=1,

                        value=str(scenario_name)

                    )

                    scenario_sheet.cell(

                        row=row_number,

                        column=2,

                        value=float(

                            scenario.get(

                                "revenue",

                                0

                            ) or 0

                        )

                    )

                    scenario_sheet.cell(

                        row=row_number,

                        column=3,

                        value=float(

                            scenario.get(

                                "total_cost",

                                scenario.get(

                                    "cost",

                                    0

                                )

                            ) or 0

                        )

                    )

                    scenario_sheet.cell(

                        row=row_number,

                        column=4,

                        value=float(

                            scenario.get(

                                "profit",

                                0

                            ) or 0

                        )

                    )

                    scenario_sheet.cell(

                        row=row_number,

                        column=5,

                        value=float(

                            scenario.get(

                                "profit_margin",

                                0

                            ) or 0

                        )

                    )

                    scenario_sheet.cell(

                        row=row_number,

                        column=6,

                        value=float(

                            scenario.get(

                                "roi",

                                0

                            ) or 0

                        )

                    )

                    row_number += 1

        # ----------------------------------------------------

        # Project data sheet

        # ----------------------------------------------------

        data_sheet = workbook.create_sheet(

            "Project Data"

        )

        fields = [

            "id",

            "chat_id",

            "name",

            "industry",

            "location",

            "description",

            "land_area",

            "saleable_area",

            "construction_area",

            "total_area",

            "revenue",

            "total_cost",

            "operating_cost",

            "net_profit",

            "investor_capital",

            "investor_profit",

            "investor_share",

            "status",

            "land_cost",

            "construction_cost",

            "financing_cost",

            "other_cost",

            "expected_revenue",

            "expected_profit",

            "notes"

        ]

        for col, field in enumerate(

            fields,

            start=1

        ):

            data_sheet.cell(

                row=1,

                column=col,

                value=field

            )

            data_sheet.cell(

                row=2,

                column=col,

                value=project.get(field)

            )

        # ----------------------------------------------------

        # Formatting

        # ----------------------------------------------------

        for sheet in workbook.worksheets:

            for column_cells in sheet.columns:

                max_length = 0

                column_letter = (

                    column_cells[0].column_letter

                )

                for cell in column_cells:

                    try:

                        value_length = len(

                            str(cell.value or "")

                        )

                        max_length = max(

                            max_length,

                            value_length

                        )

                    except Exception:

                        pass

                sheet.column_dimensions[

                    column_letter

                ].width = min(

                    max(max_length + 2, 12),

                    45

                )

        filename = safe_filename(

            f"Geniosa_Project_{project_id}_Financial_Model.xlsx"

        )

        filepath = (

            STORAGE_DIR / filename

        )

        workbook.save(

            filepath

        )

        generated_asset_save(

            chat_id=chat_id,

            project_id=project_id,

            asset_type="xlsx",

            filename=filename,

            description=(

                "Project financial model"

            )

        )

        return {

            "success": True,

            "filepath": str(filepath),

            "filename": filename,

            "asset_type": "xlsx"

        }

    except Exception as exc:

        logging.exception(

            "Excel generation failed"

        )

        return {

            "error": str(exc)

        }

def generate_project_presentation(

    chat_id,

    project_id

):

    """

    ქმნის პროექტის საინვესტიციო PowerPoint პრეზენტაციას.

    """

    project = get_project(

        chat_id,

        project_id

    )

    if not project:

        return {

            "error": f"პროექტი #{project_id} ვერ მოიძებნა."

        }

    try:

        model = analyze_project_financials(

            chat_id,

            project_id

        )

        if isinstance(model, dict) and model.get("error"):

            return model

        prs = Presentation()

        # ----------------------------------------------------

        # Slide 1 — Cover

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[0]

        )

        slide.shapes.title.text = (

            project.get("name")

            or "Investment Project"

        )

        slide.placeholders[1].text = (

            "GENIOSA 4.0\n"

            "Business & Investment Analysis"

        )

        # ----------------------------------------------------

        # Slide 2 — Project Overview

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[1]

        )

        slide.shapes.title.text = (

            "Project Overview"

        )

        overview = [

            f"Project: {project.get('name') or '-'}",

            f"Industry: {industry_name(project.get('industry'))}",

            f"Location: {project.get('location') or '-'}",

            f"Land Area: {format_number(project.get('land_area'))} m²",

            f"Construction Area: {format_number(project.get('construction_area'))} m²",

            f"Saleable Area: {format_number(project.get('saleable_area'))} m²",

            f"Total Area: {format_number(project.get('total_area'))} m²"

        ]

        slide.placeholders[1].text = (

            "\n".join(overview)

        )

        # ----------------------------------------------------

        # Slide 3 — Financial Overview

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[1]

        )

        slide.shapes.title.text = (

            "Financial Overview"

        )

        financial_text = [

            f"Total Cost: {format_money(model.get('total_cost'))}",

            f"Expected Revenue: {format_money(model.get('expected_revenue'))}",

            f"Expected Profit: {format_money(model.get('expected_profit'))}",

            f"Profit Margin: {format_number(model.get('profit_margin'))}%",

            f"ROI: {format_number(model.get('roi'))}%",

            f"Break-even Revenue: {format_money(model.get('break_even_revenue'))}",

            f"Required Sale Price / m²: {format_money(model.get('required_sale_price'))}"

        ]

        slide.placeholders[1].text = (

            "\n".join(financial_text)

        )

        # ----------------------------------------------------

        # Slide 4 — Investment Structure

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[1]

        )

        slide.shapes.title.text = (

            "Investment Structure"

        )

        investment_text = [

            f"Investor Capital: {format_money(model.get('investor_capital'))}",

            f"Investor Profit: {format_money(model.get('investor_profit'))}",

            f"Investor Share: {format_number(model.get('investor_share'))}%",

            f"Operator Profit: {format_money(model.get('operator_profit'))}"

        ]

        slide.placeholders[1].text = (

            "\n".join(investment_text)

        )

        # ----------------------------------------------------

        # Slide 5 — Scenario Analysis

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[1]

        )

        slide.shapes.title.text = (

            "Scenario Analysis"

        )

        scenarios = scenario_analysis(

            chat_id,

            project_id

        )

        scenario_lines = []

        if isinstance(scenarios, dict):

            scenario_items = scenarios.get(

                "scenarios",

                scenarios

            )

            if isinstance(

                scenario_items,

                dict

            ):

                for scenario_name, scenario in scenario_items.items():

                    if not isinstance(

                        scenario,

                        dict

                    ):

                        continue

                    scenario_lines.append(

                        f"{scenario_name}: "

                        f"Revenue {format_money(scenario.get('revenue'))}, "

                        f"Profit {format_money(scenario.get('profit'))}, "

                        f"ROI {format_number(scenario.get('roi'))}%"

                    )

        if not scenario_lines:

            scenario_lines.append(

                "Scenario data is not available."

            )

        slide.placeholders[1].text = (

            "\n".join(scenario_lines)

        )

        # ----------------------------------------------------

        # Slide 6 — Project Description

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[1]

        )

        slide.shapes.title.text = (

            "Project Description"

        )

        description = (

            project.get("description")

            or project.get("notes")

            or "No description provided."

        )

        slide.placeholders[1].text = (

            str(description)

        )

        # ----------------------------------------------------

        # Slide 7 — Investment Highlights

        # ----------------------------------------------------

        slide = prs.slides.add_slide(

            prs.slide_layouts[1]

        )

        slide.shapes.title.text = (

            "Investment Highlights"

        )

        highlights = [

            "• Clearly defined development project",

            "• Structured financial analysis",

            "• Revenue and profitability assessment",

            "• Investor return analysis",

            "• Scenario-based financial planning",

            "• Professional project presentation"

        ]

        slide.placeholders[1].text = (

            "\n".join(highlights)

        )

        # ----------------------------------------------------

        # Save

        # ----------------------------------------------------

        filename = safe_filename(

            f"Geniosa_Project_{project_id}_Investor_Presentation.pptx"

        )

        filepath = (

            STORAGE_DIR / filename

        )

        prs.save(

            filepath

        )

        generated_asset_save(

            chat_id=chat_id,

            project_id=project_id,

            asset_type="pptx",

            filename=filename,

            description=(

                "Investor presentation"

            )

        )

        return {

            "success": True,

            "filepath": str(filepath),

            "filename": filename,

            "asset_type": "pptx"

        }

    except Exception as exc:

        logging.exception(

            "PowerPoint generation failed"

        )

        return {

            "error": str(exc)

        }

def generate_project_files(

    chat_id,

    project_id

):

    """

    ქმნის ორივე ფაილს:

    XLSX + PPTX

    """

    excel_result = generate_project_excel(

        chat_id,

        project_id

    )

    if not excel_result.get("success"):

        return {

            "error": (

                "Excel-ის შექმნა ვერ მოხერხდა: "

                + str(

                    excel_result.get("error")

                )

            )

        }

    ppt_result = generate_project_presentation(

        chat_id,

        project_id

    )

    if not ppt_result.get("success"):

        return {

            "error": (

                "PowerPoint-ის შექმნა ვერ მოხერხდა: "

                + str(

                    ppt_result.get("error")

                )

            )

        }

    return {

        "success": True,

        "excel": excel_result,

        "powerpoint": ppt_result

    }

def generated_files_command(

    chat_id,

    args

):

    """

    /generate ID

    /generate ID xlsx

    /generate ID pptx

    """

    args = (args or "").strip()

    if not args:

        projects = get_projects(

            chat_id,

            limit=50

        )

        if not projects:

            return (

                "📁 ფაილის შესაქმნელად ჯერ პროექტი უნდა არსებობდეს."

            )

        lines = [

            "📁 ფაილების გენერაცია",

            "",

            "მიუთითე Project ID:",

            ""

        ]

        for project in projects:

            lines.append(

                f"#{project.get('id')} — "

                f"{project.get('name')}"

            )

        lines.extend([

            "",

            "მაგალითი:",

            "/generate 1",

            "",

            "ან:",

            "/generate 1 xlsx",

            "/generate 1 pptx"

        ])

        return "\n".join(lines)

    parts = args.split()

    project_id = safe_int(

        parts[0]

    )

    if not project_id:

        return (

            "❌ Project ID უნდა იყოს რიცხვი."

        )

    file_type = (

        parts[1].lower()

        if len(parts) > 1

        else "all"

    )

    if file_type not in (

        "all",

        "xlsx",

        "excel",

        "pptx",

        "powerpoint",

        "ppt"

    ):

        return (

            "❌ ფაილის ტიპი არასწორია.\n\n"

            "გამოიყენე:\n"

            "/generate 1\n"

            "/generate 1 xlsx\n"

            "/generate 1 pptx"

        )

    if file_type in (

        "xlsx",

        "excel"

    ):

        result = generate_project_excel(

            chat_id,

            project_id

        )

        if not result.get("success"):

            return (

                "❌ Excel ვერ შეიქმნა.\n\n"

                + str(result.get("error"))

            )

        return {

            "type": "generated_file",

            "files": [

                result

            ],

            "message": (

                "✅ Excel ფინანსური მოდელი მზად არის."

            )

        }

    if file_type in (

        "pptx",

        "powerpoint",

        "ppt"

    ):

        result = generate_project_presentation(

            chat_id,

            project_id

        )

        if not result.get("success"):

            return (

                "❌ PowerPoint ვერ შეიქმნა.\n\n"

                + str(result.get("error"))

            )

        return {

            "type": "generated_file",

            "files": [

                result

            ],

            "message": (

                "✅ PowerPoint პრეზენტაცია მზად არის."

            )

        }

    result = generate_project_files(

        chat_id,

        project_id

    )

    if not result.get("success"):

        return (

            "❌ ფაილების შექმნა ვერ მოხერხდა.\n\n"

            + str(result.get("error"))

        )

    return {

        "type": "generated_file",

        "files": [

            result.get("excel"),

            result.get("powerpoint")

        ],

        "message": (

            "✅ ორივე ფაილი მზად არის."

        )

    }

def generated_asset_command(

    chat_id

):

    """

    აჩვენებს გენერირებული ფაილების ისტორიას.

    """

    try:

        with db() as conn:

            with conn.cursor(

                cursor_factory=RealDictCursor

            ) as cur:

                cur.execute(

                    """

                    SELECT *

                    FROM generated_assets

                    WHERE chat_id = %s

                    ORDER BY created_at DESC

                    LIMIT 50

                    """,

                    (chat_id,)

                )

                rows = cur.fetchall()

        if not rows:

            return (

                "📁 გენერირებული ფაილები ჯერ არ არის."

            )

        lines = [

            "📁 გენერირებული ფაილები",

            ""

        ]

        for row in rows:

            lines.append(

                f"#{row.get('id')} — "

                f"{row.get('filename')}"

            )

            if row.get("description"):

                lines.append(

                    str(row.get("description"))

                )

            lines.append("")

        return "\n".join(lines)

    except Exception as exc:

        logging.exception(

            "Could not load generated assets"

        )

        return (

            "❌ გენერირებული ფაილების წაკითხვა ვერ მოხერხდა.\n\n"

            + str(exc)

        )

print("GENIOSA 4.0 — PART 9/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 10/10

# Telegram polling + file sending + PostgreSQL lock

# + startup / shutdown

# ============================================================

def send_document_to_chat(

    chat_id,

    filepath,

    caption=""

):

    """

    აგზავნის გენერირებულ ან ატვირთულ ფაილს Telegram-ში.

    """

    if not filepath:

        return False

    path = Path(

        str(filepath)

    )

    if not path.exists():

        logging.error(

            "File does not exist: %s",

            filepath

        )

        return False

    try:

        url = (

            f"{TELEGRAM_API}/sendDocument"

        )

        with open(

            path,

            "rb"

        ) as file_handle:

            response = requests.post(

                url,

                data={

                    "chat_id": chat_id,

                    "caption": caption[:1024]

                },

                files={

                    "document": (

                        path.name,

                        file_handle

                    )

                },

                timeout=120

            )

        if response.status_code != 200:

            logging.error(

                "Telegram sendDocument failed: %s",

                response.text[:2000]

            )

            return False

        return True

    except Exception:

        logging.exception(

            "Could not send Telegram document"

        )

        return False

def send_generated_files(

    chat_id,

    result

):

    """

    ამუშავებს generated_file პასუხს

    და აგზავნის ფაილებს Telegram-ში.

    """

    if not isinstance(

        result,

        dict

    ):

        return False

    files = result.get(

        "files",

        []

    )

    if not files:

        return False

    success_count = 0

    for file_info in files:

        if not isinstance(

            file_info,

            dict

        ):

            continue

        filepath = file_info.get(

            "filepath"

        )

        filename = file_info.get(

            "filename"

        )

        asset_type = file_info.get(

            "asset_type",

            "file"

        )

        if not filepath:

            continue

        if send_document_to_chat(

            chat_id,

            filepath,

            caption=(

                f"📁 GENIOSA 4.0\n"

                f"{filename or asset_type}"

            )

        ):

            success_count += 1

    return success_count > 0

def handle_incoming_update(

    update

):

    """

    ამუშავებს Telegram update-ს.

    """

    if not isinstance(

        update,

        dict

    ):

        return

    message = update.get(

        "message"

    )

    if not message:

        return

    chat = message.get(

        "chat"

    )

    if not chat:

        return

    chat_id = chat.get(

        "id"

    )

    if chat_id is None:

        return

    # --------------------------------------------------------

    # Access control

    # --------------------------------------------------------

    if not user_allowed(

        chat_id

    ):

        try:

            send_message(

                chat_id,

                "⛔ წვდომა შეზღუდულია."

            )

        except Exception:

            pass

        return

    # --------------------------------------------------------

    # Photo

    # --------------------------------------------------------

    photos = message.get(

        "photo"

    )

    if photos:

        try:

            response = handle_photo_message(

                chat_id,

                message

            )

            if response:

                send_long_message(

                    chat_id,

                    response

                )

        except Exception as exc:

            logging.exception(

                "Photo handler failed"

            )

            send_message(

                chat_id,

                "❌ ფოტოს დამუშავება ვერ მოხერხდა.\n\n"

                + str(exc)

            )

        return

    # --------------------------------------------------------

    # Document

    # --------------------------------------------------------

    document = message.get(

        "document"

    )

    if document:

        try:

            response = handle_document_message(

                chat_id,

                message

            )

            if isinstance(

                response,

                dict

            ) and response.get("type") == "generated_file":

                send_long_message(

                    chat_id,

                    response.get(

                        "message",

                        "✅ ფაილი მზად არის."

                    )

                )

                send_generated_files(

                    chat_id,

                    response

                )

            elif response:

                send_long_message(

                    chat_id,

                    str(response)

                )

        except Exception as exc:

            logging.exception(

                "Document handler failed"

            )

            send_message(

                chat_id,

                "❌ დოკუმენტის დამუშავება ვერ მოხერხდა.\n\n"

                + str(exc)

            )

        return

    # --------------------------------------------------------

    # Text

    # --------------------------------------------------------

    text_value = message.get(

        "text"

    )

    if text_value:

        try:

            # /generate is handled separately because

            # it returns files.

            if command_name(

                text_value

            ) == "generate":

                args = extract_command_args(

                    text_value

                )

                result = generated_files_command(

                    chat_id,

                    args

                )

                if isinstance(

                    result,

                    dict

                ) and result.get(

                    "type"

                ) == "generated_file":

                    send_long_message(

                        chat_id,

                        result.get(

                            "message",

                            "✅ ფაილები მზად არის."

                        )

                    )

                    send_generated_files(

                        chat_id,

                        result

                    )

                else:

                    send_long_message(

                        chat_id,

                        str(result)

                    )

                return

            # /files

            if command_name(

                text_value

            ) in (

                "files",

                "generated"

            ):

                send_long_message(

                    chat_id,

                    generated_asset_command(

                        chat_id

                    )

                )

                return

            response = handle_text_message(

                chat_id,

                text_value

            )

            if response:

                send_long_message(

                    chat_id,

                    response

                )

        except Exception as exc:

            logging.exception(

                "Text handler failed"

            )

            send_message(

                chat_id,

                "❌ შეტყობინების დამუშავება ვერ მოხერხდა.\n\n"

                + str(exc)

            )

        return

def acquire_polling_lock():

    """

    PostgreSQL advisory lock.

    მიზანი:

    Render-ზე მხოლოდ ერთმა Geniosa instance-მა

    გამოიყენოს Telegram getUpdates polling.

    """

    global POLLING_LOCK_CONN

    global POLLING_LOCK_ACQUIRED

    if not DATABASE_URL:

        return True

    try:

        conn = psycopg2.connect(

            DATABASE_URL,

            connect_timeout=10

        )

        conn.autocommit = True

        with conn.cursor() as cur:

            cur.execute(

                """

                SELECT pg_try_advisory_lock(

                    987654321

                )

                """

            )

            row = cur.fetchone()

            acquired = bool(

                row and row[0]

            )

        if not acquired:

            conn.close()

            POLLING_LOCK_ACQUIRED = False

            logging.warning(

                "Telegram polling lock is already held "

                "by another Geniosa instance."

            )

            return False

        POLLING_LOCK_CONN = conn

        POLLING_LOCK_ACQUIRED = True

        logging.info(

            "Telegram polling lock acquired."

        )

        return True

    except Exception:

        logging.exception(

            "Could not acquire polling lock"

        )

        POLLING_LOCK_ACQUIRED = False

        return False

def release_polling_lock():

    global POLLING_LOCK_CONN

    global POLLING_LOCK_ACQUIRED

    try:

        if (

            POLLING_LOCK_CONN

            and POLLING_LOCK_ACQUIRED

        ):

            try:

                with POLLING_LOCK_CONN.cursor() as cur:

                    cur.execute(

                        """

                        SELECT pg_advisory_unlock(

                            987654321

                        )

                        """

                    )

            except Exception:

                pass

            try:

                POLLING_LOCK_CONN.close()

            except Exception:

                pass

    finally:

        POLLING_LOCK_CONN = None

        POLLING_LOCK_ACQUIRED = False

        logging.info(

            "Telegram polling lock released."

        )

def telegram_delete_webhook():

    if not TELEGRAM_BOT_TOKEN:

        return False

    try:

        response = requests.post(

            f"{TELEGRAM_API}/deleteWebhook",

            json={

                "drop_pending_updates": False

            },

            timeout=20

        )

        if response.status_code != 200:

            logging.warning(

                "deleteWebhook failed: %s",

                response.text[:1000]

            )

            return False

        return True

    except Exception:

        logging.exception(

            "Could not delete Telegram webhook"

        )

        return False

def telegram_get_updates(

    offset=None,

    timeout=25

):

    params = {

        "timeout": timeout,

        "allowed_updates": [

            "message"

        ]

    }

    if offset is not None:

        params["offset"] = offset

    try:

        response = requests.get(

            f"{TELEGRAM_API}/getUpdates",

            params=params,

            timeout=timeout + 10

        )

        if response.status_code != 200:

            logging.error(

                "Telegram getUpdates HTTP %s: %s",

                response.status_code,

                response.text[:2000]

            )

            return None

        data = response.json()

        if not data.get(

            "ok",

            False

        ):

            logging.error(

                "Telegram getUpdates error: %s",

                data

            )

            return None

        return data.get(

            "result",

            []

        )

    except requests.exceptions.Timeout:

        return []

    except Exception:

        logging.exception(

            "Telegram getUpdates failed"

        )

        return None

def telegram_polling_loop():

    global TELEGRAM_OFFSET

    logging.info(

        "GENIOSA Telegram polling loop started."

    )

    # --------------------------------------------------------

    # Only one poller allowed

    # --------------------------------------------------------

    if not acquire_polling_lock():

        logging.warning(

            "Polling loop stopped because another "

            "instance owns the lock."

        )

        return

    try:

        telegram_delete_webhook()

        consecutive_errors = 0

        while not POLLING_STOP.is_set():

            updates = telegram_get_updates(

                offset=TELEGRAM_OFFSET,

                timeout=25

            )

            if updates is None:

                consecutive_errors += 1

                wait_seconds = min(

                    30,

                    2 ** min(

                        consecutive_errors,

                        4

                    )

                )

                logging.warning(

                    "Telegram polling error. "

                    "Retrying in %s seconds.",

                    wait_seconds

                )

                POLLING_STOP.wait(

                    wait_seconds

                )

                continue

            consecutive_errors = 0

            for update in updates:

                if POLLING_STOP.is_set():

                    break

                try:

                    update_id = update.get(

                        "update_id"

                    )

                    if update_id is not None:

                        TELEGRAM_OFFSET = (

                            int(update_id) + 1

                        )

                    handle_incoming_update(

                        update

                    )

                except Exception:

                    logging.exception(

                        "Error handling Telegram update"

                    )

    finally:

        release_polling_lock()

        logging.info(

            "GENIOSA Telegram polling loop stopped."

        )

def start_telegram_polling():

    global POLLING_THREAD

    global POLLING_STOP

    with POLLING_THREAD_LOCK:

        if (

            POLLING_THREAD

            and POLLING_THREAD.is_alive()

        ):

            logging.info(

                "Telegram polling is already running."

            )

            return

        POLLING_STOP.clear()

        POLLING_THREAD = threading.Thread(

            target=telegram_polling_loop,

            name="geniosa-telegram-poller",

            daemon=True

        )

        POLLING_THREAD.start()

        logging.info(

            "Telegram polling thread started."

        )

def stop_telegram_polling():

    global POLLING_THREAD

    global POLLING_STOP

    POLLING_STOP.set()

    thread = POLLING_THREAD

    if (

        thread

        and thread.is_alive()

        and thread is not threading.current_thread()

    ):

        thread.join(

            timeout=10

        )

    POLLING_THREAD = None

    logging.info(

        "Telegram polling stopped."

    )

def startup_application():

    logging.info(

        "GENIOSA 4.0 startup started."

    )

    try:

        init_db()

        logging.info(

            "Database initialized."

        )

    except Exception:

        logging.exception(

            "Database initialization failed."

        )

    if TELEGRAM_BOT_TOKEN:

        start_telegram_polling()

    else:

        logging.warning(

            "TELEGRAM_BOT_TOKEN is not configured."

        )

    logging.info(

        "GENIOSA 4.0 startup completed."

    )

def shutdown_application():

    logging.info(

        "GENIOSA 4.0 shutdown started."

    )

    stop_telegram_polling()

    release_polling_lock()

    logging.info(

        "GENIOSA 4.0 shutdown completed."

    )

# ============================================================

# Replace FastAPI lifespan

# ============================================================

@asynccontextmanager

async def geniosa_lifespan(app_instance):

    startup_application()

    try:

        yield

    finally:

        shutdown_application()

app.router.lifespan_context = geniosa_lifespan

print("GENIOSA 4.0 — PART 10/10 LOADED")

print("GENIOSA 4.0 — COMPLETE BUILD LOADED")
