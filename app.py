import os

import re

import json

import time

import logging

import threading

import base64

import mimetypes

from pathlib import Path

from datetime import datetime

import requests

import psycopg2

from psycopg2.extras import RealDictCursor

from fastapi import FastAPI

from openpyxl import Workbook, load_workbook

from pptx import Presentation

from pptx.util import Inches, Pt

from pypdf import PdfReader

from docx import Document

# ============================================================

# GENIOSA 4.0

# Universal Business & Investment Intelligence Platform

# ============================================================

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s %(levelname)s %(message)s"

)

logger = logging.getLogger("geniosa")

# ============================================================

# ENVIRONMENT

# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(

    "TELEGRAM_BOT_TOKEN"

)

GEMINI_API_KEY = os.getenv(

    "GEMINI_API_KEY"

)

DATABASE_URL = os.getenv(

    "DATABASE_URL"

)

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

    model = (

        model or GEMINI_MODEL

    ).strip()

    return (

        "https://generativelanguage.googleapis.com/"

        f"v1beta/models/{model}:generateContent"

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

MAX_TELEGRAM_MESSAGE = 3900

# ============================================================

# GLOBAL STATE

# ============================================================

POLLING_THREAD = None

POLLING_STOP = threading.Event()

TELEGRAM_OFFSET = None

# ============================================================

# SECURITY

# ============================================================

def user_allowed(chat_id):

    """

    If GENIOSA_OWNER_ID is configured,

    only that Telegram user can use Geniosa.

    If empty, the bot remains open.

    """

    if not OWNER_ID:

        return True

    return str(chat_id) == OWNER_ID

# ============================================================

# DATABASE CONNECTION

# ============================================================

def db():

    """

    Creates a PostgreSQL connection.

    Render PostgreSQL DATABASE_URL is used.

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

    Creates all Geniosa tables if they do not exist.

    """

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        # ----------------------------------------------------

        # Messages

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

        # Business memory

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

        # Projects

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

                revenue DOUBLE PRECISION,

                total_cost DOUBLE PRECISION,

                operating_cost DOUBLE PRECISION,

                net_profit DOUBLE PRECISION,

                investor_capital DOUBLE PRECISION,

                investor_profit DOUBLE PRECISION,

                investor_share DOUBLE PRECISION,

                status TEXT DEFAULT 'active',

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

            """

        )

        # ----------------------------------------------------

        # Documents

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

        # Investors

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

        # Deals / CRM

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

        # Research

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

        # Financial analyses

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

        # Generated assets

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

        # Securities

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

        # Crypto assets

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

# TELEGRAM SEND MESSAGE

# ============================================================

def send_message(chat_id, text):

    """

    Sends a Telegram message.

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

        if response.status_code != 200:

            logger.error(

                "Telegram sendMessage error %s: %s",

                response.status_code,

                response.text[:1000]

            )

        return response.json()

    except Exception:

        logger.exception(

            "send_message error"

        )

        return None

# ============================================================

# LONG TELEGRAM MESSAGE

# ============================================================

def send_long_message(chat_id, text):

    """

    Telegram has a message length limit.

    This function automatically splits long answers.

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

        # Try not to cut a word or line.

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

# BASIC STATUS ENDPOINT

# ============================================================

@app.get("/")

def root():

    return {

        "service": "Geniosa",

        "version": "4.0",

        "status": "online"

    }

@app.get("/status")

def status():

    return {

        "service": "Geniosa",

        "version": "4.0",

        "telegram": bool(

            TELEGRAM_BOT_TOKEN

        ),

        "gemini": bool(

            GEMINI_API_KEY

        ),

        "database": bool(

            DATABASE_URL

        ),

        "status": "online"

    }

# ============================================================

# HEALTH CHECK

# ============================================================

@app.get("/health")

def health():

    database_ok = False

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            "SELECT 1 AS ok"

        )

        result = cur.fetchone()

        database_ok = bool(

            result and result.get("ok") == 1

        )

        cur.close()

        conn.close()

    except Exception:

        logger.exception(

            "Health database check failed"

        )

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

print("GENIOSA PART 1/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 2/10

# MESSAGE HISTORY / BUSINESS MEMORY / PROJECTS

# ============================================================

# ============================================================

# MESSAGE FUNCTIONS

# ============================================================

def save_message(

    chat_id,

    role,

    text=None,

    content=None,

    message_type=None

):

    if content is not None:

        text = content

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            INSERT INTO messages (

                chat_id,

                role,

                text

            )

            VALUES (%s, %s, %s)

            """,

            (

                chat_id,

                role,

                text

            )

        )

        conn.commit()

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "save_message error"

        )

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_messages(chat_id, limit=20):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

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

                chat_id,

                int(limit)

            )

        )

        rows = cur.fetchall()

        return list(

            reversed(rows)

        )

    except Exception:

        logger.exception(

            "get_messages error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# BUSINESS MEMORY

# ============================================================

def save_memory(

    chat_id,

    memory,

    category="general",

    importance=5

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            INSERT INTO business_memory (

                chat_id,

                memory,

                category,

                importance

            )

            VALUES (%s, %s, %s, %s)

            RETURNING *

            """,

            (

                chat_id,

                memory,

                category,

                int(importance)

            )

        )

        result = cur.fetchone()

        conn.commit()

        return result

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "save_memory error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_memories(chat_id, limit=20):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            SELECT *

            FROM business_memory

            WHERE chat_id = %s

            ORDER BY

                importance DESC,

                updated_at DESC,

                id DESC

            LIMIT %s

            """,

            (

                chat_id,

                int(limit)

            )

        )

        return cur.fetchall()

    except Exception:

        logger.exception(

            "get_memories error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def update_memory(

    chat_id,

    memory_id,

    memory=None,

    category=None,

    importance=None

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        fields = []

        values = []

        if memory is not None:

            fields.append(

                "memory = %s"

            )

            values.append(

                memory

            )

        if category is not None:

            fields.append(

                "category = %s"

            )

            values.append(

                category

            )

        if importance is not None:

            fields.append(

                "importance = %s"

            )

            values.append(

                int(importance)

            )

        if not fields:

            return None

        fields.append(

            "updated_at = CURRENT_TIMESTAMP"

        )

        values.extend(

            [

                chat_id,

                memory_id

            ]

        )

        query = f"""

            UPDATE business_memory

            SET {", ".join(fields)}

            WHERE chat_id = %s

            AND id = %s

            RETURNING *

        """

        cur.execute(

            query,

            values

        )

        result = cur.fetchone()

        conn.commit()

        return result

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "update_memory error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# PROJECT FUNCTIONS

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

    revenue=None,

    total_cost=None,

    operating_cost=None,

    net_profit=None,

    investor_capital=None,

    investor_profit=None,

    investor_share=None,

    status="active"

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

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

                revenue,

                total_cost,

                operating_cost,

                net_profit,

                investor_capital,

                investor_profit,

                investor_share,

                status

            )

            VALUES (

                %s, %s, %s, %s, %s,

                %s, %s, %s, %s, %s,

                %s, %s, %s, %s, %s,

                %s, %s

            )

            RETURNING *

            """,

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

                revenue,

                total_cost,

                operating_cost,

                net_profit,

                investor_capital,

                investor_profit,

                investor_share,

                status

            )

        )

        project = cur.fetchone()

        conn.commit()

        return project

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "create_project error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_projects(

    chat_id,

    include_deleted=False

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        if include_deleted:

            cur.execute(

                """

                SELECT *

                FROM projects

                WHERE chat_id = %s

                ORDER BY

                    updated_at DESC,

                    id DESC

                """,

                (chat_id,)

            )

        else:

            cur.execute(

                """

                SELECT *

                FROM projects

                WHERE chat_id = %s

                AND COALESCE(status, 'active')

                    <> 'deleted'

                ORDER BY

                    updated_at DESC,

                    id DESC

                """,

                (chat_id,)

            )

        return cur.fetchall()

    except Exception:

        logger.exception(

            "get_projects error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_project(

    chat_id,

    project_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            SELECT *

            FROM projects

            WHERE chat_id = %s

            AND id = %s

            """,

            (

                chat_id,

                project_id

            )

        )

        return cur.fetchone()

    except Exception:

        logger.exception(

            "get_project error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def update_project(

    chat_id,

    project_id,

    **updates

):

    allowed_fields = {

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

        "status"

    }

    fields = []

    values = []

    for key, value in updates.items():

        if key not in allowed_fields:

            continue

        fields.append(

            f"{key} = %s"

        )

        values.append(

            value

        )

    if not fields:

        return None

    fields.append(

        "updated_at = CURRENT_TIMESTAMP"

    )

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        values.extend(

            [

                chat_id,

                project_id

            ]

        )

        query = f"""

            UPDATE projects

            SET {", ".join(fields)}

            WHERE chat_id = %s

            AND id = %s

            RETURNING *

        """

        cur.execute(

            query,

            values

        )

        project = cur.fetchone()

        conn.commit()

        return project

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "update_project error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def delete_project(

    chat_id,

    project_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            UPDATE projects

            SET

                status = 'deleted',

                updated_at = CURRENT_TIMESTAMP

            WHERE chat_id = %s

            AND id = %s

            """,

            (

                chat_id,

                project_id

            )

        )

        deleted = cur.rowcount

        conn.commit()

        return deleted

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "delete_project error"

        )

        return 0

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# INDUSTRY HELPERS

# ============================================================

INDUSTRY_NAMES = {

    "construction": "Construction",

    "real_estate": "Real Estate",

    "development": "Development",

    "hospitality": "Hospitality",

    "hotel": "Hotel",

    "tourism": "Tourism",

    "retail": "Retail",

    "restaurant": "Restaurant",

    "finance": "Finance",

    "technology": "Technology",

    "energy": "Energy",

    "infrastructure": "Infrastructure",

    "manufacturing": "Manufacturing",

    "other": "Other"

}

def industry_name(value):

    if not value:

        return "Other"

    key = str(

        value

    ).strip().lower()

    return INDUSTRY_NAMES.get(

        key,

        str(value)

    )

# ============================================================

# PROJECT CONTEXT

# ============================================================

def get_primary_project(chat_id):

    projects = get_projects(

        chat_id

    )

    if not projects:

        return None

    return projects[0]

def build_project_context(chat_id):

    projects = get_projects(

        chat_id,

        include_deleted=False

    )

    if not projects:

        return "შენახული პროექტები არ არის."

    lines = []

    for project in projects[:10]:

        lines.append(

            f"""

პროექტი #{project.get('id')}

სახელი: {project.get('name') or '-'}

ინდუსტრია: {industry_name(project.get('industry'))}

მდებარეობა: {project.get('location') or '-'}

აღწერა: {project.get('description') or '-'}

მიწის ფართობი: {project.get('land_area') or '-'} მ²

გასაყიდი ფართობი: {project.get('saleable_area') or '-'} მ²

სამშენებლო ფართობი: {project.get('construction_area') or '-'} მ²

სრული ფართობი: {project.get('total_area') or '-'} მ²

შემოსავალი: {project.get('revenue') or '-'}

სრული ხარჯი: {project.get('total_cost') or '-'}

ოპერაციული ხარჯი: {project.get('operating_cost') or '-'}

წმინდა მოგება: {project.get('net_profit') or '-'}

ინვესტორის კაპიტალი: {project.get('investor_capital') or '-'}

ინვესტორის მოგება: {project.get('investor_profit') or '-'}

ინვესტორის წილი: {project.get('investor_share') or '-'}%

სტატუსი: {project.get('status') or '-'}

"""

        )

    return "\n".join(

        lines

    )

# ============================================================

# BUSINESS MEMORY CONTEXT

# ============================================================

def build_memory_context(chat_id):

    memories = get_memories(

        chat_id,

        limit=20

    )

    if not memories:

        return "შენახული ბიზნეს-მეხსიერება არ არის."

    lines = []

    for memory in memories:

        lines.append(

            f"- [{memory.get('category') or 'general'}] "

            f"{memory.get('memory') or ''}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# UNIVERSAL BUSINESS CONTEXT

# ============================================================

def build_business_context(chat_id):

    project_context = build_project_context(

        chat_id

    )

    memory_context = build_memory_context(

        chat_id

    )

    return f"""

=== GENIOSA BUSINESS CONTEXT ===

მომხმარებლის შენახული პროექტები:

{project_context}

მომხმარებლის ბიზნეს-მეხსიერება:

{memory_context}

=== END BUSINESS CONTEXT ===

"""

print("GENIOSA PART 2/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 3/10

# INVESTORS / DEALS / FINANCIAL ENGINE

# ============================================================

# ============================================================

# SAFE NUMBER HELPERS

# ============================================================

def to_float(value, default=0.0):

    """

    Safely converts a value to float.

    Supports numbers written with commas or spaces.

    """

    if value is None:

        return default

    if isinstance(value, (int, float)):

        return float(value)

    try:

        text = str(value).strip()

        if not text:

            return default

        text = (

            text

            .replace(",", "")

            .replace(" ", "")

            .replace("$", "")

            .replace("€", "")

            .replace("₾", "")

        )

        return float(text)

    except Exception:

        return default

def safe_percent(part, whole):

    """

    Returns percentage.

    """

    part = to_float(part)

    whole = to_float(whole)

    if whole == 0:

        return 0.0

    return (part / whole) * 100

def format_number(value, decimals=2):

    """

    Formats numbers for readable reports.

    """

    value = to_float(value)

    if decimals == 0:

        return f"{value:,.0f}"

    return f"{value:,.{decimals}f}"

def format_money(value, currency="$"):

    """

    Formats financial values.

    """

    value = to_float(value)

    return f"{currency}{value:,.2f}"

def format_percent(value, decimals=2):

    value = to_float(value)

    return f"{value:.{decimals}f}%"

# ============================================================

# INVESTOR FUNCTIONS

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

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

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

            RETURNING *

            """,

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

        )

        investor = cur.fetchone()

        conn.commit()

        return investor

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "create_investor error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_investors(

    chat_id,

    status=None,

    limit=100

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        if status:

            cur.execute(

                """

                SELECT *

                FROM investors

                WHERE chat_id = %s

                AND status = %s

                ORDER BY

                    updated_at DESC,

                    id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    status,

                    int(limit)

                )

            )

        else:

            cur.execute(

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

                    chat_id,

                    int(limit)

                )

            )

        return cur.fetchall()

    except Exception:

        logger.exception(

            "get_investors error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_investor(

    chat_id,

    investor_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            SELECT *

            FROM investors

            WHERE chat_id = %s

            AND id = %s

            """,

            (

                chat_id,

                investor_id

            )

        )

        return cur.fetchone()

    except Exception:

        logger.exception(

            "get_investor error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def update_investor(

    chat_id,

    investor_id,

    **updates

):

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

    fields = []

    values = []

    for key, value in updates.items():

        if key not in allowed_fields:

            continue

        fields.append(

            f"{key} = %s"

        )

        values.append(

            value

        )

    if not fields:

        return None

    fields.append(

        "updated_at = CURRENT_TIMESTAMP"

    )

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        values.extend(

            [

                chat_id,

                investor_id

            ]

        )

        query = f"""

            UPDATE investors

            SET {", ".join(fields)}

            WHERE chat_id = %s

            AND id = %s

            RETURNING *

        """

        cur.execute(

            query,

            values

        )

        investor = cur.fetchone()

        conn.commit()

        return investor

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "update_investor error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def delete_investor(

    chat_id,

    investor_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            DELETE FROM investors

            WHERE chat_id = %s

            AND id = %s

            """,

            (

                chat_id,

                investor_id

            )

        )

        deleted = cur.rowcount

        conn.commit()

        return deleted

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "delete_investor error"

        )

        return 0

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# DEAL / CRM FUNCTIONS

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

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

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

                %s, %s, %s, %s, %s,

                %s, %s, %s, %s

            )

            RETURNING *

            """,

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

        )

        deal = cur.fetchone()

        conn.commit()

        return deal

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "create_deal error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_deals(

    chat_id,

    stage=None,

    limit=100

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        if stage:

            cur.execute(

                """

                SELECT

                    d.*,

                    p.name AS project_name,

                    i.name AS investor_name,

                    i.company AS investor_company

                FROM deals d

                LEFT JOIN projects p

                    ON d.project_id = p.id

                LEFT JOIN investors i

                    ON d.investor_id = i.id

                WHERE d.chat_id = %s

                AND d.stage = %s

                ORDER BY

                    d.updated_at DESC,

                    d.id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    stage,

                    int(limit)

                )

            )

        else:

            cur.execute(

                """

                SELECT

                    d.*,

                    p.name AS project_name,

                    i.name AS investor_name,

                    i.company AS investor_company

                FROM deals d

                LEFT JOIN projects p

                    ON d.project_id = p.id

                LEFT JOIN investors i

                    ON d.investor_id = i.id

                WHERE d.chat_id = %s

                ORDER BY

                    d.updated_at DESC,

                    d.id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    int(limit)

                )

            )

        return cur.fetchall()

    except Exception:

        logger.exception(

            "get_deals error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_deal(

    chat_id,

    deal_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            SELECT

                d.*,

                p.name AS project_name,

                i.name AS investor_name,

                i.company AS investor_company

            FROM deals d

            LEFT JOIN projects p

                ON d.project_id = p.id

            LEFT JOIN investors i

                ON d.investor_id = i.id

            WHERE d.chat_id = %s

            AND d.id = %s

            """,

            (

                chat_id,

                deal_id

            )

        )

        return cur.fetchone()

    except Exception:

        logger.exception(

            "get_deal error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def update_deal(

    chat_id,

    deal_id,

    **updates

):

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

    fields = []

    values = []

    for key, value in updates.items():

        if key not in allowed_fields:

            continue

        fields.append(

            f"{key} = %s"

        )

        values.append(

            value

        )

    if not fields:

        return None

    fields.append(

        "updated_at = CURRENT_TIMESTAMP"

    )

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        values.extend(

            [

                chat_id,

                deal_id

            ]

        )

        query = f"""

            UPDATE deals

            SET {", ".join(fields)}

            WHERE chat_id = %s

            AND id = %s

            RETURNING *

        """

        cur.execute(

            query,

            values

        )

        deal = cur.fetchone()

        conn.commit()

        return deal

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "update_deal error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def delete_deal(

    chat_id,

    deal_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            DELETE FROM deals

            WHERE chat_id = %s

            AND id = %s

            """,

            (

                chat_id,

                deal_id

            )

        )

        deleted = cur.rowcount

        conn.commit()

        return deleted

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "delete_deal error"

        )

        return 0

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# FINANCIAL ENGINE

# ============================================================

def calculate_financials(

    revenue=0,

    construction_cost=0,

    land_cost=0,

    operating_cost=0,

    financing_cost=0,

    other_cost=0,

    investor_capital=0

):

    """

    Universal project financial calculation.

    """

    revenue = to_float(revenue)

    construction_cost = to_float(

        construction_cost

    )

    land_cost = to_float(

        land_cost

    )

    operating_cost = to_float(

        operating_cost

    )

    financing_cost = to_float(

        financing_cost

    )

    other_cost = to_float(

        other_cost

    )

    investor_capital = to_float(

        investor_capital

    )

    total_cost = (

        construction_cost

        + land_cost

        + operating_cost

        + financing_cost

        + other_cost

    )

    gross_profit = (

        revenue

        - construction_cost

        - land_cost

    )

    net_profit = (

        revenue

        - total_cost

    )

    gross_margin = safe_percent(

        gross_profit,

        revenue

    )

    net_margin = safe_percent(

        net_profit,

        revenue

    )

    roi = safe_percent(

        net_profit,

        total_cost

    )

    investor_roi = safe_percent(

        net_profit,

        investor_capital

    )

    return {

        "revenue": revenue,

        "construction_cost": construction_cost,

        "land_cost": land_cost,

        "operating_cost": operating_cost,

        "financing_cost": financing_cost,

        "other_cost": other_cost,

        "total_cost": total_cost,

        "gross_profit": gross_profit,

        "net_profit": net_profit,

        "gross_margin": gross_margin,

        "net_margin": net_margin,

        "roi": roi,

        "investor_capital": investor_capital,

        "investor_roi": investor_roi

    }

def calculate_unit_economics(

    saleable_area=0,

    sale_price_per_m2=0,

    construction_area=0,

    construction_cost_per_m2=0,

    land_cost=0,

    other_cost=0

):

    """

    Calculates project economics based on m².

    """

    saleable_area = to_float(

        saleable_area

    )

    sale_price_per_m2 = to_float(

        sale_price_per_m2

    )

    construction_area = to_float(

        construction_area

    )

    construction_cost_per_m2 = to_float(

        construction_cost_per_m2

    )

    land_cost = to_float(

        land_cost

    )

    other_cost = to_float(

        other_cost

    )

    revenue = (

        saleable_area

        * sale_price_per_m2

    )

    construction_cost = (

        construction_area

        * construction_cost_per_m2

    )

    total_cost = (

        construction_cost

        + land_cost

        + other_cost

    )

    profit = (

        revenue

        - total_cost

    )

    margin = safe_percent(

        profit,

        revenue

    )

    roi = safe_percent(

        profit,

        total_cost

    )

    return {

        "saleable_area": saleable_area,

        "sale_price_per_m2": sale_price_per_m2,

        "construction_area": construction_area,

        "construction_cost_per_m2":

            construction_cost_per_m2,

        "revenue": revenue,

        "construction_cost":

            construction_cost,

        "land_cost": land_cost,

        "other_cost": other_cost,

        "total_cost": total_cost,

        "profit": profit,

        "margin": margin,

        "roi": roi

    }

def calculate_investor_split(

    net_profit,

    investor_share=80,

    operator_share=20,

    investor_capital=0

):

    """

    Calculates profit distribution.

    """

    net_profit = to_float(

        net_profit

    )

    investor_share = to_float(

        investor_share

    )

    operator_share = to_float(

        operator_share

    )

    investor_capital = to_float(

        investor_capital

    )

    investor_profit = (

        net_profit

        * investor_share

        / 100

    )

    operator_profit = (

        net_profit

        * operator_share

        / 100

    )

    total_received_by_investor = (

        investor_capital

        + investor_profit

    )

    investor_roi = safe_percent(

        investor_profit,

        investor_capital

    )

    return {

        "net_profit": net_profit,

        "investor_share": investor_share,

        "operator_share": operator_share,

        "investor_profit": investor_profit,

        "operator_profit": operator_profit,

        "investor_capital": investor_capital,

        "total_received_by_investor":

            total_received_by_investor,

        "investor_roi": investor_roi

    }

def calculate_scenario(

    revenue,

    total_cost,

    revenue_change=0,

    cost_change=0

):

    """

    Scenario analysis:

    revenue_change = percentage change

    cost_change = percentage change

    """

    revenue = to_float(

        revenue

    )

    total_cost = to_float(

        total_cost

    )

    revenue_change = to_float(

        revenue_change

    )

    cost_change = to_float(

        cost_change

    )

    adjusted_revenue = (

        revenue

        * (1 + revenue_change / 100)

    )

    adjusted_cost = (

        total_cost

        * (1 + cost_change / 100)

    )

    profit = (

        adjusted_revenue

        - adjusted_cost

    )

    margin = safe_percent(

        profit,

        adjusted_revenue

    )

    roi = safe_percent(

        profit,

        adjusted_cost

    )

    return {

        "revenue": adjusted_revenue,

        "cost": adjusted_cost,

        "profit": profit,

        "margin": margin,

        "roi": roi

    }

def calculate_break_even(

    fixed_cost,

    variable_cost_per_unit,

    selling_price_per_unit

):

    """

    Break-even units.

    """

    fixed_cost = to_float(

        fixed_cost

    )

    variable_cost_per_unit = to_float(

        variable_cost_per_unit

    )

    selling_price_per_unit = to_float(

        selling_price_per_unit

    )

    contribution = (

        selling_price_per_unit

        - variable_cost_per_unit

    )

    if contribution <= 0:

        return None

    units = (

        fixed_cost

        / contribution

    )

    return {

        "fixed_cost": fixed_cost,

        "variable_cost_per_unit":

            variable_cost_per_unit,

        "selling_price_per_unit":

            selling_price_per_unit,

        "contribution_per_unit":

            contribution,

        "break_even_units": units

    }

def calculate_payback_period(

    investment,

    annual_cash_flow

):

    """

    Simple payback period in years.

    """

    investment = to_float(

        investment

    )

    annual_cash_flow = to_float(

        annual_cash_flow

    )

    if annual_cash_flow <= 0:

        return None

    return investment / annual_cash_flow

# ============================================================

# FINANCIAL ANALYSIS STORAGE

# ============================================================

def save_financial_analysis(

    chat_id,

    project_id,

    analysis_type,

    input_data,

    result_data

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

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

        cur.execute(

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

            RETURNING *

            """,

            (

                chat_id,

                project_id,

                analysis_type,

                input_data,

                result_data

            )

        )

        result = cur.fetchone()

        conn.commit()

        return result

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "save_financial_analysis error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_financial_analyses(

    chat_id,

    project_id=None,

    limit=20

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        if project_id is None:

            cur.execute(

                """

                SELECT *

                FROM financial_analyses

                WHERE chat_id = %s

                ORDER BY id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    int(limit)

                )

            )

        else:

            cur.execute(

                """

                SELECT *

                FROM financial_analyses

                WHERE chat_id = %s

                AND project_id = %s

                ORDER BY id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    project_id,

                    int(limit)

                )

            )

        return cur.fetchall()

    except Exception:

        logger.exception(

            "get_financial_analyses error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# PROJECT FINANCIAL SNAPSHOT

# ============================================================

def build_project_financial_snapshot(

    project

):

    if not project:

        return None

    revenue = to_float(

        project.get("revenue")

    )

    total_cost = to_float(

        project.get("total_cost")

    )

    operating_cost = to_float(

        project.get("operating_cost")

    )

    net_profit = to_float(

        project.get("net_profit")

    )

    if net_profit == 0 and revenue:

        net_profit = (

            revenue

            - total_cost

            - operating_cost

        )

    margin = safe_percent(

        net_profit,

        revenue

    )

    roi = safe_percent(

        net_profit,

        total_cost

    )

    return {

        "project_id":

            project.get("id"),

        "project_name":

            project.get("name"),

        "revenue":

            revenue,

        "total_cost":

            total_cost,

        "operating_cost":

            operating_cost,

        "net_profit":

            net_profit,

        "net_margin":

            margin,

        "roi":

            roi,

        "investor_capital":

            to_float(

                project.get(

                    "investor_capital"

                )

            ),

        "investor_profit":

            to_float(

                project.get(

                    "investor_profit"

                )

            ),

        "investor_share":

            to_float(

                project.get(

                    "investor_share"

                )

            )

    }

# ============================================================

# TEXT FINANCIAL REPORT

# ============================================================

def format_financial_report(

    result,

    title="Financial Analysis"

):

    if not result:

        return (

            f"{title}\n\n"

            "ფინანსური მონაცემები ვერ მოიძებნა."

        )

    lines = [

        f"📊 {title}",

        "",

        f"შემოსავალი: "

        f"{format_money(result.get('revenue'))}",

        f"სრული ხარჯი: "

        f"{format_money(result.get('total_cost'))}",

        f"წმინდა მოგება: "

        f"{format_money(result.get('net_profit'))}",

        f"წმინდა მარჟა: "

        f"{format_percent(result.get('net_margin'))}",

        f"ROI: "

        f"{format_percent(result.get('roi'))}"

    ]

    if result.get("investor_capital"):

        lines.extend(

            [

                "",

                "ინვესტორი:",

                f"კაპიტალი: "

                f"{format_money(result.get('investor_capital'))}",

                f"მოგება: "

                f"{format_money(result.get('investor_profit'))}",

                f"წილი: "

                f"{format_percent(result.get('investor_share'))}"

            ]

        )

    return "\n".join(

        lines

    )

# ============================================================

# INVESTOR FORMATTER

# ============================================================

def format_investor(

    investor

):

    if not investor:

        return "ინვესტორი ვერ მოიძებნა."

    lines = [

        f"👤 ინვესტორი #{investor.get('id')}",

        f"სახელი: {investor.get('name') or '-'}",

        f"კომპანია: {investor.get('company') or '-'}",

        f"ქვეყანა: {investor.get('country') or '-'}",

        f"კონტაქტი: {investor.get('contact') or '-'}",

        f"საინვესტიციო შესაძლებლობა: "

        f"{format_money(investor.get('investment_capacity'))}",

        f"სასურველი სექტორი: "

        f"{investor.get('preferred_sector') or '-'}",

        f"სტატუსი: {investor.get('status') or '-'}",

        f"შენიშვნა: {investor.get('notes') or '-'}"

    ]

    return "\n".join(

        lines

    )

# ============================================================

# DEAL FORMATTER

# ============================================================

def format_deal(

    deal

):

    if not deal:

        return "გარიგება ვერ მოიძებნა."

    lines = [

        f"🤝 გარიგება #{deal.get('id')}",

        f"პროექტი: {deal.get('project_name') or '-'}",

        f"ინვესტორი: "

        f"{deal.get('investor_name') or '-'}",

        f"კომპანია: "

        f"{deal.get('investor_company') or '-'}",

        f"ეტაპი: {deal.get('stage') or '-'}",

        f"შეთავაზებული თანხა: "

        f"{format_money(deal.get('proposed_amount'))}",

        f"შეთავაზებული წილი: "

        f"{format_percent(deal.get('proposed_share'))}",

        f"შეფასება: "

        f"{format_money(deal.get('valuation'))}",

        f"შემდეგი ნაბიჯი: "

        f"{deal.get('next_step') or '-'}",

        f"შენიშვნა: "

        f"{deal.get('notes') or '-'}"

    ]

    return "\n".join(

        lines

    )

# ============================================================

# LIST SUMMARIES

# ============================================================

def investors_summary(

    chat_id

):

    investors = get_investors(

        chat_id

    )

    if not investors:

        return (

            "👤 ინვესტორების ბაზა ცარიელია."

        )

    lines = [

        f"👥 ინვესტორები: {len(investors)}",

        ""

    ]

    for investor in investors[:30]:

        capacity = investor.get(

            "investment_capacity"

        )

        capacity_text = (

            format_money(capacity)

            if capacity

            else "-"

        )

        lines.append(

            f"#{investor.get('id')} "

            f"{investor.get('name') or '-'}"

            f" | {investor.get('company') or '-'}"

            f" | {investor.get('country') or '-'}"

            f" | {capacity_text}"

            f" | {investor.get('status') or 'new'}"

        )

    return "\n".join(

        lines

    )

def deals_summary(

    chat_id

):

    deals = get_deals(

        chat_id

    )

    if not deals:

        return (

            "🤝 გარიგებების ბაზა ცარიელია."

        )

    lines = [

        f"🤝 გარიგებები: {len(deals)}",

        ""

    ]

    for deal in deals[:30]:

        amount = deal.get(

            "proposed_amount"

        )

        amount_text = (

            format_money(amount)

            if amount

            else "-"

        )

        lines.append(

            f"#{deal.get('id')} "

            f"{deal.get('project_name') or '-'}"

            f" → {deal.get('investor_name') or '-'}"

            f" | {deal.get('stage') or 'new'}"

            f" | {amount_text}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# PART 3 CHECK

# ============================================================

print("GENIOSA PART 3/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 4/10

# DOCUMENT PROCESSING / FILE EXTRACTION / AI ANALYSIS

# ============================================================

# ============================================================

# DOCUMENT TYPE DETECTION

# ============================================================

SUPPORTED_DOCUMENT_TYPES = {

    ".pdf": "PDF",

    ".docx": "DOCX",

    ".xlsx": "XLSX",

    ".xlsm": "XLSM",

    ".pptx": "PPTX",

    ".txt": "TXT",

    ".csv": "CSV"

}

def detect_file_type(

    filename

):

    """

    Detects document type from file extension.

    """

    if not filename:

        return "unknown"

    suffix = (

        Path(str(filename))

        .suffix

        .lower()

    )

    return SUPPORTED_DOCUMENT_TYPES.get(

        suffix,

        "unknown"

    )

# ============================================================

# FILE SIZE / TEXT LIMITS

# ============================================================

MAX_DOCUMENT_TEXT = 60000

MAX_EXCEL_ROWS = 500

MAX_EXCEL_COLUMNS = 50

MAX_PPTX_SLIDES = 100

def trim_document_text(

    text,

    max_chars=MAX_DOCUMENT_TEXT

):

    """

    Prevents extremely large documents

    from creating oversized AI requests.

    """

    if not text:

        return ""

    text = str(text)

    if len(text) <= max_chars:

        return text

    return (

        text[:max_chars]

        + "\n\n"

        "[DOCUMENT TEXT TRUNCATED]"

    )

# ============================================================

# TEXT FILE EXTRACTION

# ============================================================

def extract_text_file(

    file_path

):

    try:

        path = Path(

            file_path

        )

        raw = path.read_bytes()

        for encoding in (

            "utf-8",

            "utf-8-sig",

            "cp1252",

            "latin-1"

        ):

            try:

                return raw.decode(

                    encoding

                )

            except UnicodeDecodeError:

                continue

        return raw.decode(

            "utf-8",

            errors="replace"

        )

    except Exception:

        logger.exception(

            "extract_text_file error"

        )

        return ""

# ============================================================

# PDF EXTRACTION

# ============================================================

def extract_pdf_text(

    file_path

):

    try:

        reader = PdfReader(

            str(file_path)

        )

        pages = []

        for index, page in enumerate(

            reader.pages

        ):

            try:

                page_text = (

                    page.extract_text()

                    or ""

                )

                if page_text.strip():

                    pages.append(

                        f"\n--- PAGE {index + 1} ---\n"

                        f"{page_text}"

                    )

            except Exception:

                logger.exception(

                    "PDF page extraction error"

                )

        return trim_document_text(

            "\n".join(pages)

        )

    except Exception:

        logger.exception(

            "extract_pdf_text error"

        )

        return ""

# ============================================================

# DOCX EXTRACTION

# ============================================================

def extract_docx_text(

    file_path

):

    try:

        document = Document(

            str(file_path)

        )

        parts = []

        # ----------------------------------------------------

        # Paragraphs

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

        # Tables

        # ----------------------------------------------------

        for table_index, table in enumerate(

            document.tables

        ):

            parts.append(

                f"\n--- TABLE {table_index + 1} ---"

            )

            for row in table.rows:

                cells = []

                for cell in row.cells:

                    cells.append(

                        (

                            cell.text

                            or ""

                        ).replace(

                            "\n",

                            " "

                        ).strip()

                    )

                parts.append(

                    " | ".join(cells)

                )

        return trim_document_text(

            "\n".join(parts)

        )

    except Exception:

        logger.exception(

            "extract_docx_text error"

        )

        return ""

# ============================================================

# EXCEL EXTRACTION

# ============================================================

def extract_excel_text(

    file_path

):

    try:

        workbook = load_workbook(

            filename=str(file_path),

            read_only=True,

            data_only=True

        )

        parts = []

        for sheet in workbook.worksheets:

            parts.append(

                f"\n--- SHEET: {sheet.title} ---"

            )

            row_count = 0

            for row in sheet.iter_rows(

                values_only=True

            ):

                row_count += 1

                if row_count > MAX_EXCEL_ROWS:

                    parts.append(

                        "[SHEET ROW LIMIT REACHED]"

                    )

                    break

                values = []

                for value in row[

                    :MAX_EXCEL_COLUMNS

                ]:

                    if value is None:

                        values.append("")

                    else:

                        values.append(

                            str(value)

                        )

                if any(

                    value.strip()

                    for value in values

                ):

                    parts.append(

                        " | ".join(values)

                    )

        workbook.close()

        return trim_document_text(

            "\n".join(parts)

        )

    except Exception:

        logger.exception(

            "extract_excel_text error"

        )

        return ""

# ============================================================

# POWERPOINT EXTRACTION

# ============================================================

def extract_pptx_text(

    file_path

):

    try:

        presentation = Presentation(

            str(file_path)

        )

        parts = []

        slides = presentation.slides

        for index, slide in enumerate(

            slides[:MAX_PPTX_SLIDES]

        ):

            parts.append(

                f"\n--- SLIDE {index + 1} ---"

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

        return trim_document_text(

            "\n".join(parts)

        )

    except Exception:

        logger.exception(

            "extract_pptx_text error"

        )

        return ""

# ============================================================

# UNIVERSAL DOCUMENT EXTRACTION

# ============================================================

def extract_file_text(

    file_path,

    filename=None

):

    """

    Universal document extractor.

    """

    path = Path(

        file_path

    )

    if not filename:

        filename = path.name

    suffix = (

        path.suffix

        .lower()

    )

    # --------------------------------------------------------

    # TXT

    # --------------------------------------------------------

    if suffix == ".txt":

        return trim_document_text(

            extract_text_file(

                path

            )

        )

    # --------------------------------------------------------

    # CSV

    # --------------------------------------------------------

    if suffix == ".csv":

        return trim_document_text(

            extract_text_file(

                path

            )

        )

    # --------------------------------------------------------

    # PDF

    # --------------------------------------------------------

    if suffix == ".pdf":

        return extract_pdf_text(

            path

        )

    # --------------------------------------------------------

    # DOCX

    # --------------------------------------------------------

    if suffix == ".docx":

        return extract_docx_text(

            path

        )

    # --------------------------------------------------------

    # XLSX / XLSM

    # --------------------------------------------------------

    if suffix in (

        ".xlsx",

        ".xlsm"

    ):

        return extract_excel_text(

            path

        )

    # --------------------------------------------------------

    # PPTX

    # --------------------------------------------------------

    if suffix == ".pptx":

        return extract_pptx_text(

            path

        )

    # --------------------------------------------------------

    # Unsupported old Microsoft formats

    # --------------------------------------------------------

    if suffix in (

        ".doc",

        ".xls"

    ):

        return (

            "ეს არის ძველი Microsoft Office ფორმატი "

            f"({suffix}). "

            "გთხოვთ შეინახოთ ფაილი DOCX ან XLSX ფორმატში "

            "და ხელახლა ატვირთოთ."

        )

    return (

        f"ფაილის ფორმატი {suffix or 'უცნობია'} "

        "ამჟამად არ არის მხარდაჭერილი."

    )

# ============================================================

# DOCUMENT DATABASE

# ============================================================

def save_document_record(

    chat_id,

    project_id,

    filename,

    file_type,

    extracted_text

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

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

            RETURNING *

            """,

            (

                chat_id,

                project_id,

                filename,

                file_type,

                trim_document_text(

                    extracted_text

                )

            )

        )

        document = cur.fetchone()

        conn.commit()

        return document

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "save_document_record error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def update_document_analysis(

    chat_id,

    document_id,

    analysis

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            UPDATE documents

            SET

                analysis = %s,

                updated_at = CURRENT_TIMESTAMP

            WHERE chat_id = %s

            AND id = %s

            RETURNING *

            """,

            (

                analysis,

                chat_id,

                document_id

            )

        )

        document = cur.fetchone()

        conn.commit()

        return document

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "update_document_analysis error"

        )

        return None

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

def get_documents(

    chat_id,

    project_id=None,

    limit=30

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        if project_id is None:

            cur.execute(

                """

                SELECT *

                FROM documents

                WHERE chat_id = %s

                ORDER BY id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    int(limit)

                )

            )

        else:

            cur.execute(

                """

                SELECT *

                FROM documents

                WHERE chat_id = %s

                AND project_id = %s

                ORDER BY id DESC

                LIMIT %s

                """,

                (

                    chat_id,

                    project_id,

                    int(limit)

                )

            )

        return cur.fetchall()

    except Exception:

        logger.exception(

            "get_documents error"

        )

        return []

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# DOCUMENT ANALYSIS PROMPT

# ============================================================

def build_document_analysis_prompt(

    chat_id,

    filename,

    file_type,

    extracted_text

):

    project_context = build_project_context(

        chat_id

    )

    memory_context = build_memory_context(

        chat_id

    )

    return f"""

შენ ხარ Geniosa — პროფესიონალური ბიზნესისა და

საინვესტიციო ანალიზის AI ასისტენტი.

გაანალიზე მომხმარებლის მიერ ატვირთული დოკუმენტი.

ფაილი:

{filename}

ფორმატი:

{file_type}

შენახული პროექტების კონტექსტი:

{project_context}

ბიზნეს-მეხსიერება:

{memory_context}

დოკუმენტის ტექსტი:

--------------------

{extracted_text}

--------------------

მოამზადე პრაქტიკული და პროფესიონალური ანალიზი.

აუცილებლად გამოყავი:

1. დოკუმენტის მოკლე შინაარსი

2. ძირითადი ფაქტები და ციფრები

3. ფინანსური მაჩვენებლები

4. შემოსავლები

5. ხარჯები

6. მოგება

7. ინვესტიციის მოცულობა

8. რისკები

9. საეჭვო ან ერთმანეთთან შეუსაბამო მონაცემები

10. მნიშვნელოვანი საკითხები, რომლებიც დამატებით გადამოწმებას საჭიროებს

11. საინვესტიციო მიმზიდველობა

12. რა ინფორმაცია აკლია დოკუმენტს

13. კონკრეტული რეკომენდაციები შემდეგი ნაბიჯებისთვის

თუ დოკუმენტში რაიმე ინფორმაცია არ არის,

არ მოიგონო მონაცემები.

მიუთითე მკაფიოდ:

"მონაცემი დოკუმენტში არ არის".

თუ ციფრებს შორის წინააღმდეგობაა,

აჩვენე ორივე მონაცემი და ახსენი განსხვავება.

პასუხი დაწერე ქართულად,

პროფესიონალური ბიზნეს-სტილით.

"""

# ============================================================

# AI DOCUMENT ANALYSIS

# ============================================================

def analyze_document_with_ai(

    chat_id,

    filename,

    file_type,

    extracted_text

):

    if not extracted_text:

        return (

            "დოკუმენტიდან ტექსტის ამოღება ვერ მოხერხდა."

        )

    prompt = build_document_analysis_prompt(

        chat_id=chat_id,

        filename=filename,

        file_type=file_type,

        extracted_text=extracted_text

    )

    try:

        result = gemini_generate(

            prompt,

            temperature=0.2,

            max_output_tokens=5000

        )

        if not result:

            return (

                "AI ანალიზის მიღება ვერ მოხერხდა."

            )

        return result

    except Exception:

        logger.exception(

            "analyze_document_with_ai error"

        )

        return (

            "დოკუმენტის AI ანალიზის დროს "

            "დაფიქსირდა შეცდომა."

        )

# ============================================================

# DOCUMENT PROCESSING PIPELINE

# ============================================================

def process_document_file(

    chat_id,

    file_path,

    filename,

    project_id=None

):

    """

    Complete document pipeline:

    1. Detect type

    2. Extract text

    3. Save document

    4. Analyze with AI

    5. Save analysis

    """

    file_type = detect_file_type(

        filename

    )

    if file_type == "unknown":

        return {

            "success": False,

            "message": (

                "ეს ფაილის ფორმატი Geniosa-ს "

                "მიერ ამჟამად არ არის მხარდაჭერილი."

            )

        }

    extracted_text = extract_file_text(

        file_path,

        filename

    )

    if not extracted_text:

        return {

            "success": False,

            "message": (

                "ფაილი მივიღე, მაგრამ მისგან "

                "წასაკითხი ტექსტის ამოღება ვერ მოხერხდა."

            )

        }

    document = save_document_record(

        chat_id=chat_id,

        project_id=project_id,

        filename=filename,

        file_type=file_type,

        extracted_text=extracted_text

    )

    if not document:

        return {

            "success": False,

            "message": (

                "დოკუმენტი დამუშავდა, მაგრამ "

                "ბაზაში შენახვა ვერ მოხერხდა."

            )

        }

    analysis = analyze_document_with_ai(

        chat_id=chat_id,

        filename=filename,

        file_type=file_type,

        extracted_text=extracted_text

    )

    update_document_analysis(

        chat_id=chat_id,

        document_id=document.get("id"),

        analysis=analysis

    )

    return {

        "success": True,

        "document_id": document.get("id"),

        "filename": filename,

        "file_type": file_type,

        "extracted_text": extracted_text,

        "analysis": analysis

    }

# ============================================================

# DOCUMENT SUMMARY

# ============================================================

def documents_summary(

    chat_id

):

    documents = get_documents(

        chat_id

    )

    if not documents:

        return (

            "📄 ატვირთული დოკუმენტები არ არის."

        )

    lines = [

        f"📄 დოკუმენტები: {len(documents)}",

        ""

    ]

    for document in documents[:30]:

        lines.append(

            f"#{document.get('id')} "

            f"{document.get('filename') or '-'}"

            f" | {document.get('file_type') or '-'}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# DOCUMENT ANALYSIS DISPLAY

# ============================================================

def format_document_analysis(

    document

):

    if not document:

        return (

            "დოკუმენტი ვერ მოიძებნა."

        )

    filename = (

        document.get("filename")

        or "უცნობი ფაილი"

    )

    analysis = (

        document.get("analysis")

        or "AI ანალიზი ჯერ არ არსებობს."

    )

    return (

        f"📄 {filename}\n\n"

        f"{analysis}"

    )

# ============================================================

# PART 4 CHECK

# ============================================================

print("GENIOSA PART 4/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 5/10

# GEMINI AI ENGINE / BUSINESS CONTEXT / CHAT INTELLIGENCE

# ============================================================

# ============================================================

# GEMINI API REQUEST

# ============================================================

def gemini_generate(

    prompt,

    temperature=0.3,

    max_output_tokens=4000,

    model=None

):

    """

    Sends a text request to Google Gemini.

    Returns:

        str -> AI response

        None -> if request failed

    """

    if not GEMINI_API_KEY:

        logger.error(

            "GEMINI_API_KEY is missing"

        )

        return None

    if not prompt:

        return None

    selected_model = (

        model or GEMINI_MODEL

    ).strip()

    payload = {

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

    url = gemini_url(

        selected_model

    )

    try:

        response = requests.post(

            url,

            params={

                "key": GEMINI_API_KEY

            },

            json=payload,

            timeout=90

        )

    except requests.RequestException as exc:

        logger.error(

            "Gemini network error: %s",

            exc

        )

        return None

    except Exception:

        logger.exception(

            "Gemini request error"

        )

        return None

    # --------------------------------------------------------

    # HTTP ERROR

    # --------------------------------------------------------

    if response.status_code != 200:

        logger.error(

            "Gemini HTTP %s: %s",

            response.status_code,

            response.text[:2000]

        )

        return None

    # --------------------------------------------------------

    # JSON PARSE

    # --------------------------------------------------------

    try:

        data = response.json()

    except Exception:

        logger.error(

            "Gemini returned invalid JSON"

        )

        return None

    # --------------------------------------------------------

    # RESPONSE EXTRACTION

    # --------------------------------------------------------

    candidates = (

        data.get("candidates")

        or []

    )

    if not candidates:

        logger.warning(

            "Gemini response contains no candidates: %s",

            str(data)[:2000]

        )

        return None

    candidate = candidates[0] or {}

    content = (

        candidate.get("content")

        or {}

    )

    parts = (

        content.get("parts")

        or []

    )

    texts = []

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

            texts.append(

                str(text_value)

            )

    result = "\n".join(

        texts

    ).strip()

    if not result:

        logger.warning(

            "Gemini returned empty text"

        )

        return None

    return result

# ============================================================

# GEMINI ERROR MESSAGE

# ============================================================

def gemini_error_message():

    return (

        "⚠️ Geniosa-ს AI ძრავასთან დაკავშირება "

        "ამ მომენტში ვერ მოხერხდა.\n\n"

        "გთხოვთ რამდენიმე წამში ხელახლა სცადოთ."

    )

# ============================================================

# AI SYSTEM PROMPT

# ============================================================

def ai_system_prompt(

    chat_id

):

    """

    Builds Geniosa's complete business intelligence

    context.

    """

    business_context = build_business_context(

        chat_id

    )

    messages = get_messages(

        chat_id,

        limit=16

    )

    history_lines = []

    for message in messages:

        role = (

            message.get("role")

            or "user"

        )

        text_value = (

            message.get("text")

            or ""

        ).strip()

        if not text_value:

            continue

        if len(text_value) > 2500:

            text_value = (

                text_value[:2500]

                + "..."

            )

        if role == "assistant":

            speaker = "Geniosa"

        else:

            speaker = "User"

        history_lines.append(

            f"{speaker}: {text_value}"

        )

    history = (

        "\n".join(history_lines)

        if history_lines

        else "წინა საუბარი არ არის."

    )

    return f"""

შენ ხარ Geniosa 4.0 —

უნივერსალური ბიზნესისა და საინვესტიციო

ინტელექტუალური ასისტენტი.

შენი მიზანია დაეხმარო მომხმარებელს:

• ბიზნესის მართვაში

• სამშენებლო და დეველოპერულ პროექტებში

• ინვესტორების მოძიებასა და შეფასებაში

• საინვესტიციო შეთავაზებების მომზადებაში

• ფინანსურ მოდელირებაში

• პროექტების ეკონომიკის შეფასებაში

• CRM და გარიგებების მართვაში

• დოკუმენტების ანალიზში

• ბაზრის კვლევაში

• ბიზნეს სტრატეგიაში

• რისკების შეფასებაში

• პრეზენტაციებისა და ბიზნეს მასალების მომზადებაში

============================================================

ძირითადი წესები

============================================================

1. არასოდეს მოიგონო ფაქტი.

2. თუ კონკრეტული ინფორმაცია არ გაქვს,

   პირდაპირ თქვი, რომ ინფორმაცია არ გაქვს.

3. თუ მომხმარებელი გაძლევს ციფრებს,

   გამოიყენე ზუსტად ის ციფრები,

   მაგრამ საჭიროების შემთხვევაში შეამოწმე

   მათი მათემატიკური თანმიმდევრულობა.

4. ფინანსურ გამოთვლებში აჩვენე:

   • ფორმულა

   • ძირითადი დაშვება

   • შედეგი

5. თუ მონაცემები ურთიერთსაწინააღმდეგოა,

   არ დამალო წინააღმდეგობა.

   მიუთითე კონკრეტულად სად არის პრობლემა.

6. საინვესტიციო გადაწყვეტილების დროს

   გამოყავი:

   • დადებითი ფაქტორები

   • რისკები

   • სუსტი მხარეები

   • შესაძლებლობები

   • რეკომენდებული შემდეგი ნაბიჯები

7. მომხმარებელს უპასუხე იმ ენაზე,

   რომელზეც ის გესაუბრება.

8. ქართული პასუხები დაწერე ბუნებრივი,

   პროფესიონალური ქართულით.

9. რუსულ კითხვებზე უპასუხე რუსულად.

10. ინგლისურ კითხვებზე უპასუხე ინგლისურად.

11. მოკლე კითხვას მიეცი მოკლე პასუხი.

    რთულ ბიზნეს საკითხზე კი იმუშავე დეტალურად.

12. თუ მომხმარებელი ითხოვს ბიზნეს დოკუმენტს,

    გამოიყენე პროფესიონალური სტრუქტურა.

13. არ თქვა, რომ რაღაც გააკეთე,

    თუ რეალურად ეს ფუნქცია არ შესრულებულა.

14. არ მოითხოვო მომხმარებლისგან მონაცემების

    ხელახლა გამეორება, თუ ისინი უკვე არსებობს

    Geniosa-ს მეხსიერებაში ან პროექტებში.

============================================================

შენახული ბიზნეს კონტექსტი

============================================================

{business_context}

============================================================

ბოლო საუბრის ისტორია

============================================================

{history}

============================================================

END CONTEXT

============================================================

"""

# ============================================================

# USER PROMPT BUILDER

# ============================================================

def build_ai_prompt(

    chat_id,

    user_text

):

    system_context = ai_system_prompt(

        chat_id

    )

    return f"""

{system_context}

============================================================

CURRENT USER REQUEST

============================================================

{user_text}

============================================================

TASK

============================================================

უპასუხე მომხმარებლის მიმდინარე მოთხოვნას.

თუ საჭიროა გამოთვლა:

ჯერ გამოთვალე მონაცემები,

შემდეგ ახსენი შედეგი.

თუ საჭიროა ბიზნეს გადაწყვეტილება:

მიუთითე რეკომენდაცია და მისი მიზეზი.

თუ ინფორმაცია არასაკმარისია:

დაუსვი მხოლოდ ის დამატებითი კითხვა,

რომელიც რეალურად აუცილებელია.

პასუხი იყოს პრაქტიკული და გამოსაყენებელი.

"""

# ============================================================

# AI CHAT

# ============================================================

def ai_chat(

    chat_id,

    user_text

):

    if not user_text:

        return (

            "გთხოვთ დაწეროთ თქვენი მოთხოვნა."

        )

    prompt = build_ai_prompt(

        chat_id,

        user_text

    )

    result = gemini_generate(

        prompt,

        temperature=0.35,

        max_output_tokens=5000

    )

    if not result:

        return gemini_error_message()

    return result

# ============================================================

# BUSINESS MEMORY DETECTION

# ============================================================

MEMORY_KEYWORDS = [

    "დაიმახსოვრე",

    "შეინახე",

    "დაიმახსოვრეთ",

    "remember",

    "save this",

    "keep this",

    "запомни",

    "сохрани"

]

def looks_like_memory_request(

    text

):

    if not text:

        return False

    lowered = (

        str(text)

        .strip()

        .lower()

    )

    return any(

        keyword in lowered

        for keyword in MEMORY_KEYWORDS

    )

def clean_memory_text(

    text

):

    """

    Removes common memory-command prefixes.

    """

    if not text:

        return ""

    result = str(

        text

    ).strip()

    prefixes = [

        "დაიმახსოვრე",

        "შეინახე",

        "დაიმახსოვრეთ",

        "remember",

        "save this",

        "keep this",

        "запомни",

        "сохрани"

    ]

    changed = True

    while changed:

        changed = False

        lowered = result.lower()

        for prefix in prefixes:

            if lowered.startswith(

                prefix.lower()

            ):

                result = result[

                    len(prefix):

                ].strip()

                changed = True

                break

    return result

# ============================================================

# SAVE MEMORY FROM NATURAL LANGUAGE

# ============================================================

def save_memory_from_message(

    chat_id,

    text

):

    memory_text = clean_memory_text(

        text

    )

    if not memory_text:

        return None

    category = "general"

    lowered = (

        memory_text.lower()

    )

    if any(

        word in lowered

        for word in (

            "project",

            "პროექტ",

            "проект"

        )

    ):

        category = "project"

    elif any(

        word in lowered

        for word in (

            "investor",

            "ინვესტორ",

            "инвестор"

        )

    ):

        category = "investor"

    elif any(

        word in lowered

        for word in (

            "company",

            "კომპანი",

            "компани"

        )

    ):

        category = "company"

    elif any(

        word in lowered

        for word in (

            "financial",

            "finance",

            "ფინანს",

            "финанс"

        )

    ):

        category = "financial"

    return save_memory(

        chat_id=chat_id,

        memory=memory_text,

        category=category,

        importance=8

    )

# ============================================================

# MEMORY SUMMARY

# ============================================================

def memory_summary(

    chat_id

):

    memories = get_memories(

        chat_id,

        limit=50

    )

    if not memories:

        return (

            "🧠 Geniosa-ს მეხსიერებაში "

            "შენახული ინფორმაცია ჯერ არ არის."

        )

    lines = [

        f"🧠 შენახული მეხსიერება: {len(memories)}",

        ""

    ]

    for memory in memories:

        memory_id = memory.get(

            "id"

        )

        category = (

            memory.get("category")

            or "general"

        )

        memory_text = (

            memory.get("memory")

            or ""

        )

        lines.append(

            f"#{memory_id} "

            f"[{category}] "

            f"{memory_text}"

        )

    return "\n".join(

        lines

    )

# ============================================================

# DELETE MEMORY

# ============================================================

def delete_memory(

    chat_id,

    memory_id

):

    conn = None

    cur = None

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(

            """

            DELETE FROM business_memory

            WHERE chat_id = %s

            AND id = %s

            """,

            (

                chat_id,

                memory_id

            )

        )

        deleted = cur.rowcount

        conn.commit()

        return deleted

    except Exception:

        if conn:

            conn.rollback()

        logger.exception(

            "delete_memory error"

        )

        return 0

    finally:

        if cur:

            cur.close()

        if conn:

            conn.close()

# ============================================================

# AI RESPONSE WITH AUTOMATIC MEMORY COMMAND

# ============================================================

def process_ai_text(

    chat_id,

    user_text

):

    """

    Main natural-language AI processor.

    """

    if not user_text:

        return (

            "გთხოვთ დაწეროთ ტექსტი."

        )

    # --------------------------------------------------------

    # Explicit memory request

    # --------------------------------------------------------

    if looks_like_memory_request(

        user_text

    ):

        memory = save_memory_from_message(

            chat_id,

            user_text

        )

        if memory:

            return (

                "🧠 ინფორმაცია შევინახე "

                "Geniosa-ს ბიზნეს-მეხსიერებაში.\n\n"

                f"{memory.get('memory')}"

            )

        return (

            "⚠️ ინფორმაციის შენახვა ვერ მოხერხდა."

        )

    # --------------------------------------------------------

    # Normal AI conversation

    # --------------------------------------------------------

    save_message(

        chat_id,

        "user",

        user_text

    )

    response = ai_chat(

        chat_id,

        user_text

    )

    save_message(

        chat_id,

        "assistant",

        response

    )

    return response

# ============================================================

# AI PROJECT EXTRACTION

# ============================================================

def extract_project_data_with_ai(

    chat_id,

    text

):

    """

    Converts natural language project description

    into structured JSON.

    This is used later by project commands.

    """

    prompt = f"""

შენ ხარ ბიზნეს მონაცემების სტრუქტურირების AI.

მომხმარებელმა აღწერა პროექტი:

{text}

მომხმარებლის არსებული პროექტების კონტექსტი:

{build_project_context(chat_id)}

დააბრუნე მხოლოდ JSON ობიექტი.

გამოიყენე შემდეგი ველები:

{{

  "name": null,

  "industry": null,

  "location": null,

  "description": null,

  "land_area": null,

  "saleable_area": null,

  "construction_area": null,

  "total_area": null,

  "revenue": null,

  "total_cost": null,

  "operating_cost": null,

  "net_profit": null,

  "investor_capital": null,

  "investor_profit": null,

  "investor_share": null

}}

თუ მონაცემი არ არის,

დააყენე null.

არ მოიგონო მონაცემები.

არ დაამატო სხვა ველები.

"""

    result = gemini_generate(

        prompt,

        temperature=0.1,

        max_output_tokens=2000

    )

    if not result:

        return None

    # --------------------------------------------------------

    # Clean possible Markdown JSON

    # --------------------------------------------------------

    cleaned = result.strip()

    if cleaned.startswith(

        "```"

    ):

        cleaned = re.sub(

            r"^```(?:json)?\s*",

            "",

            cleaned,

            flags=re.IGNORECASE

        )

        cleaned = re.sub(

            r"\s*```$",

            "",

            cleaned

        )

    # --------------------------------------------------------

    # Parse JSON

    # --------------------------------------------------------

    try:

        data = json.loads(

            cleaned

        )

        if isinstance(

            data,

            dict

        ):

            return data

    except Exception:

        logger.warning(

            "AI project JSON parse failed: %s",

            cleaned[:2000]

        )

    # --------------------------------------------------------

    # Try to extract JSON object

    # --------------------------------------------------------

    match = re.search(

        r"\{.*\}",

        cleaned,

        flags=re.DOTALL

    )

    if match:

        try:

            data = json.loads(

                match.group(0)

            )

            if isinstance(

                data,

                dict

            ):

                return data

        except Exception:

            pass

    return None

# ============================================================

# PART 5 CHECK

# ============================================================

print("GENIOSA PART 5/10 LOADED")# ============================================================

# GENIOSA 4.0 — PART 6/10

# TELEGRAM FILE DOWNLOAD / IMAGE PROCESSING / GEMINI VISION

# ============================================================

# ============================================================

# TELEGRAM FILE DOWNLOAD

# ============================================================

def telegram_get_file(

    file_id

):

    """

    Gets Telegram file information.

    """

    if not file_id:

        return None

    try:

        response = requests.get(

            telegram_url("getFile"),

            params={

                "file_id": file_id

            },

            timeout=30

        )

        if response.status_code != 200:

            logger.error(

                "Telegram getFile HTTP %s: %s",

                response.status_code,

                response.text[:1000]

            )

            return None

        data = response.json()

        if not data.get("ok"):

            logger.error(

                "Telegram getFile failed: %s",

                str(data)[:1000]

            )

            return None

        return (

            data.get("result")

            or {}

        )

    except Exception:

        logger.exception(

            "telegram_get_file error"

        )

        return None

def download_telegram_file(

    file_id,

    filename=None

):

    """

    Downloads a Telegram file into /tmp/geniosa.

    """

    file_info = telegram_get_file(

        file_id

    )

    if not file_info:

        return None

    file_path = file_info.get(

        "file_path"

    )

    if not file_path:

        return None

    if not filename:

        filename = Path(

            file_path

        ).name

    # --------------------------------------------------------

    # Sanitize filename

    # --------------------------------------------------------

    safe_name = re.sub(

        r"[^A-Za-z0-9._-]+",

        "_",

        str(filename)

    )

    if not safe_name:

        safe_name = (

            f"telegram_{int(time.time())}"

        )

    local_path = (

        DOWNLOAD_DIR

        / f"{int(time.time() * 1000)}_{safe_name}"

    )

    download_url = (

        f"https://api.telegram.org/file/bot"

        f"{TELEGRAM_BOT_TOKEN}/"

        f"{file_path}"

    )

    try:

        response = requests.get(

            download_url,

            timeout=90

        )

        if response.status_code != 200:

            logger.error(

                "Telegram file download HTTP %s",

                response.status_code

            )

            return None

        local_path.write_bytes(

            response.content

        )

        return local_path

    except Exception:

        logger.exception(

            "download_telegram_file error"

        )

        return None

# ============================================================

# IMAGE HELPERS

# ============================================================

SUPPORTED_IMAGE_TYPES = {

    ".jpg": "image/jpeg",

    ".jpeg": "image/jpeg",

    ".png": "image/png",

    ".webp": "image/webp"

}

def image_mime_type(

    file_path

):

    suffix = (

        Path(file_path)

        .suffix

        .lower()

    )

    return SUPPORTED_IMAGE_TYPES.get(

        suffix

    )

def image_to_base64(

    file_path

):

    try:

        raw = Path(

            file_path

        ).read_bytes()

        return base64.b64encode(

            raw

        ).decode(

            "utf-8"

        )

    except Exception:

        logger.exception(

            "image_to_base64 error"

        )

        return None

# ============================================================

# GEMINI VISION

# ============================================================

def gemini_analyze_image(

    prompt,

    file_path,

    temperature=0.25,

    max_output_tokens=4000

):

    """

    Sends an image + text prompt to Gemini.

    """

    if not GEMINI_API_KEY:

        return None

    if not file_path:

        return None

    mime_type = image_mime_type(

        file_path

    )

    if not mime_type:

        logger.error(

            "Unsupported image type: %s",

            file_path

        )

        return None

    encoded = image_to_base64(

        file_path

    )

    if not encoded:

        return None

    payload = {

        "contents": [

            {

                "role": "user",

                "parts": [

                    {

                        "text": str(

                            prompt

                        )

                    },

                    {

                        "inline_data": {

                            "mime_type": mime_type,

                            "data": encoded

                        }

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

            gemini_url(),

            params={

                "key": GEMINI_API_KEY

            },

            json=payload,

            timeout=120

        )

    except requests.RequestException as exc:

        logger.error(

            "Gemini Vision network error: %s",

            exc

        )

        return None

    except Exception:

        logger.exception(

            "Gemini Vision request error"

        )

        return None

    if response.status_code != 200:

        logger.error(

            "Gemini Vision HTTP %s: %s",

            response.status_code,

            response.text[:2000]

        )

        return None

    try:

        data = response.json()

    except Exception:

        logger.error(

            "Gemini Vision invalid JSON"

        )

        return None

    candidates = (

        data.get("candidates")

        or []

    )

    if not candidates:

        logger.warning(

            "Gemini Vision returned no candidates"

        )

        return None

    content = (

        candidates[0].get(

            "content"

        )

        or {}

    )

    parts = (

        content.get("parts")

        or []

    )

    texts = []

    for part in parts:

        if not isinstance(

            part,

            dict

        ):

            continue

        value = part.get(

            "text"

        )

        if value:

            texts.append(

                str(value)

            )

    result = "\n".join(

        texts

    ).strip()

    return result or None

# ============================================================

# IMAGE ANALYSIS PROMPT

# ============================================================

def build_image_analysis_prompt(

    chat_id,

    caption=""

):

    project_context = build_project_context(

        chat_id

    )

    memory_context = build_memory_context(

        chat_id

    )

    caption_text = (

        caption

        if caption

        else "მომხმარებელმა აღწერა არ დაურთო."

    )

    return f"""

შენ ხარ Geniosa 4.0 —

ბიზნესისა და საინვესტიციო ანალიზის AI ასისტენტი.

მომხმარებელმა გამოგიგზავნა სურათი.

მომხმარებლის ტექსტი/აღწერა:

{caption_text}

არსებული პროექტების კონტექსტი:

{project_context}

ბიზნეს-მეხსიერება:

{memory_context}

============================================================

სურათის ანალიზის წესები

============================================================

გაანალიზე სურათი მაქსიმალურად პრაქტიკულად.

თუ სურათი ეხება:

• სამშენებლო პროექტს

• არქიტექტურას

• ინტერიერს

• ექსტერიერს

• უძრავ ქონებას

• საინვესტიციო პროექტს

• გეგმას

• ცხრილს

• დიაგრამას

• დოკუმენტს

• პროდუქტის დიზაინს

მიუთითე შესაბამისი ინფორმაცია.

აუცილებლად განასხვავე:

1. რა ჩანს პირდაპირ სურათზე

2. რა შეიძლება იყოს სავარაუდო

3. რა ვერ დგინდება სურათიდან

არ წარმოადგინო ვარაუდი ფაქტად.

თუ სურათზე არის ტექსტი ან ციფრები,

მოიყვანე მხოლოდ ის ინფორმაცია,

რომლის წაკითხვაც შესაძლებელია.

თუ რაიმე ნაწილი ბუნდოვანია,

მიუთითე რომ მონაცემი ვერ იკითხება.

თუ ეს სამშენებლო/უძრავი ქონების სურათია,

შეაფასე:

• არქიტექტურული გადაწყვეტა

• ვიზუალური ხარისხი

• ფუნქციონალური შესაძლებლობები

• კომერციული მიმზიდველობა

• პოტენციური პრობლემები

• რა შეიძლება გაუმჯობესდეს

თუ ფინანსური ცხრილია,

გამოყავი:

• შემოსავალი

• ხარჯი

• მოგება

• პროცენტები

• მნიშვნელოვანი შეუსაბამობები

პასუხი დაწერე ქართულად,

პროფესიონალური ბიზნეს-სტილით.

"""

# ============================================================

# PROCESS IMAGE

# ============================================================

def process_image_file(

    chat_id,

    file_path,

    filename=None,

    caption=""

):

    """

    Complete image-analysis pipeline.

    """

    if not file_path:

        return (

            "სურათის მიღება ვერ მოხერხდა."

        )

    if not filename:

        filename = Path(

            file_path

        ).name

    mime_type = image_mime_type(

        file_path

    )

    if not mime_type:

        return (

            "⚠️ ეს სურათის ფორმატი "

            "ამჟამად არ არის მხარდაჭერილი.\n\n"

            "გამოიყენეთ JPG, PNG ან WEBP."

        )

    prompt = build_image_analysis_prompt(

        chat_id,

        caption

    )

    result = gemini_analyze_image(

        prompt=prompt,

        file_path=file_path,

        temperature=0.25,

        max_output_tokens=4000

    )

    if not result:

        return gemini_error_message()

    return (

        f"🖼️ სურათის ანალიზი\n"

        f"ფაილი: {filename}\n\n"

        f"{result}"

    )

# ============================================================

# PHOTO UPDATE PROCESSING

# ============================================================

def process_photo_update(

    chat_id,

    message

):

    """

    Handles Telegram photo messages.

    """

    photos = (

        message.get("photo")

        or []

    )

    if not photos:

        return (

            "სურათი ვერ მოიძებნა."

        )

    # Telegram usually sends several sizes.

    # The last one is normally the largest.

    photo = photos[-1]

    file_id = photo.get(

        "file_id"

    )

    if not file_id:

        return (

            "სურათის file_id ვერ მოიძებნა."

        )

    caption = (

        message.get("caption")

        or ""

    ).strip()

    local_path = download_telegram_file(

        file_id,

        filename=f"photo_{int(time.time())}.jpg"

    )

    if not local_path:

        return (

            "⚠️ სურათის ჩამოტვირთვა ვერ მოხერხდა."

        )

    try:

        return process_image_file(

            chat_id=chat_id,

            file_path=local_path,

            filename=local_path.name,

            caption=caption

        )

    finally:

        try:

            local_path.unlink(

                missing_ok=True

            )

        except Exception:

            pass

# ============================================================

# DOCUMENT MESSAGE PROCESSING

# ============================================================

def process_document_update(

    chat_id,

    message

):

    """

    Handles Telegram document messages.

    """

    document = (

        message.get("document")

        or {}

    )

    file_id = document.get(

        "file_id"

    )

    filename = (

        document.get("file_name")

        or f"document_{int(time.time())}"

    )

    if not file_id:

        return (

            "დოკუმენტის file_id ვერ მოიძებნა."

        )

    # --------------------------------------------------------

    # Check supported type before download

    # --------------------------------------------------------

    file_type = detect_file_type(

        filename

    )

    if file_type == "unknown":

        suffix = (

            Path(filename)

            .suffix

            .lower()

        )

        if suffix in (

            ".doc",

            ".xls"

        ):

            return (

                "⚠️ ძველი Office ფორმატები "

                "(.doc / .xls) პირდაპირ არ იკითხება.\n\n"

                "გთხოვთ შეინახოთ ფაილი "

                "DOCX ან XLSX ფორმატში "

                "და ხელახლა ატვირთოთ."

            )

        return (

            f"⚠️ ფაილის ფორმატი "

            f"{suffix or 'უცნობი'} "

            "არ არის მხარდაჭერილი."

        )

    local_path = download_telegram_file(

        file_id,

        filename=filename

    )

    if not local_path:

        return (

            "⚠️ დოკუმენტის ჩამოტვირთვა "

            "ვერ მოხერხდა."

        )

    caption = (

        message.get("caption")

        or ""

    ).strip()

    # --------------------------------------------------------

    # Optional project ID from caption

    #

    # Example:

    # /project 12

    # --------------------------------------------------------

    project_id = None

    match = re.search(

        r"(?:/project|project)\s*#?\s*(\d+)",

        caption,

        flags=re.IGNORECASE

    )

    if match:

        try:

            project_id = int(

                match.group(1)

            )

        except Exception:

            project_id = None

    try:

        result = process_document_file(

            chat_id=chat_id,

            file_path=local_path,

            filename=filename,

            project_id=project_id

        )

        if not result.get("success"):

            return result.get(

                "message",

                "დოკუმენტის დამუშავება ვერ მოხერხდა."

            )

        analysis = (

            result.get("analysis")

            or "AI ანალიზი ვერ დაბრუნდა."

        )

        return (

            f"📄 დოკუმენტი დამუშავდა\n"

            f"ფაილი: {filename}\n"

            f"ტიპი: {file_type}\n"

            f"Document ID: {result.get('document_id')}\n\n"

            f"{analysis}"

        )

    finally:

        try:

            local_path.unlink(

                missing_ok=True

            )

        except Exception:

            pass

# ============================================================

# GENERIC FILE MESSAGE PROCESSOR

# ============================================================

def process_media_message(

    chat_id,

    message

):

    """

    Determines whether the Telegram message

    contains a photo or a document.

    """

    if message.get("photo"):

        return process_photo_update(

            chat_id,

            message

        )

    if message.get("document"):

        return process_document_update(

            chat_id,

            message

        )

    return (

        "ფაილის ტიპი ვერ განისაზღვრა."

    )

# ============================================================

# PART 6 CHECK

# ============================================================

print("GENIOSA PART 6/10 LOADED")
            
# =========================

# PART 7/10 — COMMANDS & MANAGEMENT

# =========================

def format_project(project):

    if not project:

        return "პროექტი ვერ მოიძებნა."

    lines = [

        f"🏗️ პროექტი #{project.get('id')}",

        f"სახელი: {project.get('name') or '—'}",

    ]

    if project.get("location"):

        lines.append(f"📍 მდებარეობა: {project.get('location')}")

    if project.get("industry"):

        lines.append(

            f"🏢 მიმართულება: {industry_name(project.get('industry'))}"

        )

    if project.get("status"):

        lines.append(f"📊 სტატუსი: {project.get('status')}")

    if project.get("description"):

        lines.append(

            f"\n📝 აღწერა:\n{project.get('description')}"

        )

    numeric_fields = [

        ("land_area", "მიწის ფართობი", "მ²"),

        ("build_area", "სამშენებლო ფართობი", "მ²"),

        ("saleable_area", "გასაყიდი ფართობი", "მ²"),

        ("construction_cost", "სამშენებლო ღირებულება", "$"),

        ("land_cost", "მიწის ღირებულება", "$"),

        ("total_cost", "სრული ღირებულება", "$"),

        ("expected_revenue", "მოსალოდნელი შემოსავალი", "$"),

        ("expected_profit", "მოსალოდნელი მოგება", "$"),

        ("net_profit", "წმინდა მოგება", "$"),

    ]

    for field, label, unit in numeric_fields:

        value = project.get(field)

        if value is None:

            continue

        if unit == "$":

            formatted = format_money(value)

        else:

            formatted = format_number(value)

        lines.append(f"• {label}: {formatted} {unit}")

    if project.get("notes"):

        lines.append(

            f"\n📌 დამატებითი ინფორმაცია:\n{project.get('notes')}"

        )

    return "\n".join(lines)

def format_project_list(projects):

    if not projects:

        return "📭 პროექტები ჯერ არ გაქვს დამატებული."

    lines = ["🏗️ შენი პროექტები:\n"]

    for project in projects:

        project_id = project.get("id")

        name = project.get("name") or "უსახელო პროექტი"

        location = project.get("location") or "—"

        status = project.get("status") or "აქტიური"

        lines.append(

            f"#{project_id} — {name}\n"

            f"   📍 {location}\n"

            f"   📊 {status}"

        )

    return "\n".join(lines)

def get_document(chat_id, document_id):

    conn = None

    try:

        conn = db()

        with conn.cursor() as cur:

            cur.execute(

                """

                SELECT *

                FROM documents

                WHERE id = %s AND chat_id = %s

                LIMIT 1

                """,

                (document_id, chat_id),

            )

            row = cur.fetchone()

        return row

    except Exception as e:

        logging.exception("get_document error: %s", e)

        return None

    finally:

        if conn:

            conn.close()

def format_document(document):

    if not document:

        return "დოკუმენტი ვერ მოიძებნა."

    lines = [

        f"📄 დოკუმენტი #{document.get('id')}",

        f"ფაილი: {document.get('filename') or '—'}",

        f"ტიპი: {document.get('file_type') or '—'}",

    ]

    if document.get("project_id"):

        lines.append(

            f"🏗️ პროექტი: #{document.get('project_id')}"

        )

    if document.get("analysis"):

        lines.append(

            f"\n🤖 AI ანალიზი:\n{document.get('analysis')}"

        )

    if document.get("created_at"):

        lines.append(

            f"\n📅 დამატებულია: {document.get('created_at')}"

        )

    return "\n".join(lines)

def command_help():

    return (

        "🤖 GENIOSA — ბრძანებები\n\n"

        "ძირითადი:\n"

        "/start — დაწყება\n"

        "/help — დახმარება\n"

        "/status — სისტემის სტატუსი\n\n"

        "🏗️ პროექტები:\n"

        "/projects — ყველა პროექტი\n"

        "/project — მთავარი პროექტი\n"

        "/project ID — კონკრეტული პროექტი\n"

        "/newproject ტექსტი — ახალი პროექტის შექმნა\n"

        "/delete_project ID — პროექტის წაშლა\n\n"

        "👥 ინვესტორები:\n"

        "/investors — ინვესტორების სია\n"

        "/investor ID — კონკრეტული ინვესტორი\n"

        "/newinvestor ტექსტი — ინვესტორის დამატება\n\n"

        "🤝 გარიგებები:\n"

        "/deals — გარიგებების სია\n"

        "/deal ID — კონკრეტული გარიგება\n\n"

        "📄 დოკუმენტები:\n"

        "/documents — დოკუმენტების სია\n"

        "/document ID — კონკრეტული დოკუმენტი\n\n"

        "💰 ფინანსები:\n"

        "/financial — მთავარი პროექტის ფინანსები\n"

        "/financial ID — კონკრეტული პროექტის ფინანსები\n\n"

        "🧠 მეხსიერება:\n"

        "/memory — შენახული ინფორმაცია\n"

        "/forget ID — მეხსიერებიდან ინფორმაციის წაშლა\n\n"

        "ასევე შეგიძლია უბრალოდ მომწერო ჩვეულებრივი ტექსტით "

        "რა გჭირდება — Geniosa გამოიყენებს AI-ს."

    )

def parse_command(text):

    text = normalize_text(text)

    if not text.startswith("/"):

        return None, ""

    parts = text.split(maxsplit=1)

    command = parts[0].lower()

    # /start@GeniosaBot → /start

    if "@" in command:

        command = command.split("@", 1)[0]

    args = ""

    if len(parts) > 1:

        args = parts[1].strip()

    return command, args

def parse_int_argument(args):

    if not args:

        return None

    match = re.search(r"\d+", args)

    if not match:

        return None

    try:

        return int(match.group(0))

    except Exception:

        return None

def create_project_from_text(chat_id, text):

    if not text:

        return (

            "მაგალითი:\n"

            "/newproject ქუთაისში სასტუმროს პროექტი, "

            "ნიკეას ქუჩაზე, 3070 მ² მიწაზე."

        )

    try:

        data = extract_project_data_with_ai(chat_id, text)

        if not isinstance(data, dict):

            return "პროექტის მონაცემების ამოცნობა ვერ მოხერხდა."

        name = (

            data.get("name")

            or data.get("project_name")

            or data.get("title")

        )

        if not name:

            return (

                "პროექტის სახელი ვერ ამოვიცანი.\n\n"

                "გთხოვ, მიუთითე პროექტის სახელი.\n"

                "მაგალითად:\n"

                "/newproject NIKKEA 12 Kutaisi"

            )

        project = create_project(

            chat_id=chat_id,

            name=name,

            description=data.get("description"),

            location=data.get("location"),

            industry=data.get("industry"),

            status=data.get("status") or "active",

            land_area=to_float(data.get("land_area")),

            build_area=to_float(data.get("build_area")),

            saleable_area=to_float(data.get("saleable_area")),

            land_cost=to_float(data.get("land_cost")),

            construction_cost=to_float(

                data.get("construction_cost")

            ),

            total_cost=to_float(data.get("total_cost")),

            expected_revenue=to_float(

                data.get("expected_revenue")

            ),

            expected_profit=to_float(

                data.get("expected_profit")

            ),

            net_profit=to_float(data.get("net_profit")),

            notes=data.get("notes"),

        )

        if not project:

            return "❌ პროექტის შექმნა ვერ მოხერხდა."

        return (

            "✅ პროექტი წარმატებით შეიქმნა.\n\n"

            + format_project(project)

        )

    except Exception as e:

        logging.exception(

            "create_project_from_text error: %s",

            e

        )

        return (

            "❌ პროექტის შექმნისას დაფიქსირდა შეცდომა.\n"

            "გთხოვ, ისევ სცადო."

        )

def extract_investor_data_with_ai(chat_id, text):

    prompt = f"""

მომხმარებელს უნდა ინვესტორის დამატება CRM-ში.

მომხმარებლის ტექსტი:

{text}

დააბრუნე მხოლოდ JSON ობიექტი შემდეგი ველებით:

{{

  "name": "",

  "company": "",

  "country": "",

  "contact": "",

  "investment_capacity": null,

  "preferred_sector": "",

  "status": "new",

  "notes": ""

}}

წესები:

- არ მოიგონო ინფორმაცია.

- თუ ინფორმაცია არ არის, დატოვე ცარიელი.

- investment_capacity უნდა იყოს მხოლოდ რიცხვი ან null.

- მხოლოდ JSON დააბრუნე.

"""

    raw = gemini_generate(prompt)

    if not raw:

        return None

    try:

        cleaned = raw.strip()

        if cleaned.startswith("```"):

            cleaned = re.sub(

                r"^```(?:json)?",

                "",

                cleaned,

                flags=re.IGNORECASE

            )

            cleaned = re.sub(

                r"```$",

                "",

                cleaned

            ).strip()

        match = re.search(

            r"\{.*\}",

            cleaned,

            flags=re.DOTALL

        )

        if not match:

            return None

        return json.loads(match.group(0))

    except Exception:

        logging.exception(

            "Investor JSON parsing error"

        )

        return None

def create_investor_from_text(chat_id, text):

    if not text:

        return (

            "მაგალითი:\n"

            "/newinvestor ABC Development, "

            "Dubai, real estate investor, "

            "capacity $20M"

        )

    data = extract_investor_data_with_ai(

        chat_id,

        text

    )

    if not data:

        return "ინვესტორის მონაცემების ამოცნობა ვერ მოხერხდა."

    name = data.get("name")

    if not name:

        return (

            "ინვესტორის სახელი ვერ ამოვიცანი.\n"

            "გთხოვ, მიუთითე სახელი ან კომპანიის დასახელება."

        )

    try:

        investor = create_investor(

            chat_id=chat_id,

            name=name,

            company=data.get("company"),

            country=data.get("country"),

            contact=data.get("contact"),

            investment_capacity=to_float(

                data.get("investment_capacity")

            ),

            preferred_sector=data.get("preferred_sector"),

            status=data.get("status") or "new",

            notes=data.get("notes"),

        )

        if not investor:

            return "❌ ინვესტორის დამატება ვერ მოხერხდა."

        return (

            "✅ ინვესტორი დაემატა CRM-ში.\n\n"

            + format_investor(investor)

        )

    except Exception as e:

        logging.exception(

            "create_investor_from_text error: %s",

            e

        )

        return "❌ ინვესტორის დამატებისას მოხდა შეცდომა."

def handle_command(chat_id, text):

    command, args = parse_command(text)

    if not command:

        return None

    # =========================

    # START

    # =========================

    if command == "/start":

        return (

            "👋 მოგესალმები Geniosa-ში.\n\n"

            "მე ვარ შენი AI ბიზნეს ასისტენტი.\n"

            "შემიძლია დაგეხმარო პროექტებში, ინვესტორებში, "

            "გარიგებებში, ფინანსურ ანალიზში და დოკუმენტებში.\n\n"

            "დაიწყე /help ბრძანებით."

        )

    # =========================

    # HELP

    # =========================

    if command in ("/help", "/commands"):

        return command_help()

    # =========================

    # STATUS

    # =========================

    if command == "/status":

        try:

            conn = db()

            with conn.cursor() as cur:

                cur.execute("SELECT 1")

                cur.fetchone()

            conn.close()

            return (

                "🟢 GENIOSA STATUS\n\n"

                "Telegram: ✅\n"

                "Database: ✅\n"

                f"Gemini: {'✅' if GEMINI_API_KEY else '❌'}\n"

                "Application: ✅"

            )

        except Exception as e:

            logging.exception(

                "Status check error: %s",

                e

            )

            return (

                "🟡 GENIOSA STATUS\n\n"

                "Telegram: ✅\n"

                "Database: ❌\n"

                "Application: ⚠️"

            )

    # =========================

    # PROJECTS

    # =========================

    if command == "/projects":

        projects = get_projects(chat_id)

        return format_project_list(projects)

    if command == "/project":

        project_id = parse_int_argument(args)

        if project_id:

            project = get_project(

                chat_id,

                project_id

            )

        else:

            project = get_primary_project(chat_id)

        if not project:

            return (

                "📭 პროექტი ვერ მოიძებნა.\n\n"

                "შეგიძლია შექმნა:\n"

                "/newproject პროექტის აღწერა"

            )

        return format_project(project)

    if command == "/newproject":

        return create_project_from_text(

            chat_id,

            args

        )

    if command == "/delete_project":

        project_id = parse_int_argument(args)

        if not project_id:

            return (

                "გამოიყენე:\n"

                "/delete_project ID"

            )

        project = get_project(

            chat_id,

            project_id

        )

        if not project:

            return "ასეთი პროექტი ვერ მოიძებნა."

        success = delete_project(

            chat_id,

            project_id

        )

        if success:

            return (

                f"🗑️ პროექტი #{project_id} წაიშალა."

            )

        return "პროექტის წაშლა ვერ მოხერხდა."

    # =========================

    # INVESTORS

    # =========================

    if command == "/investors":

        investors = get_investors(chat_id)

        if not investors:

            return (

                "📭 ინვესტორები ჯერ არ არის დამატებული.\n\n"

                "შეგიძლია დაამატო:\n"

                "/newinvestor ინვესტორის ინფორმაცია"

            )

        return investors_summary(investors)

    if command == "/investor":

        investor_id = parse_int_argument(args)

        if not investor_id:

            return (

                "გამოიყენე:\n"

                "/investor ID"

            )

        investor = get_investor(

            chat_id,

            investor_id

        )

        if not investor:

            return "ინვესტორი ვერ მოიძებნა."

        return format_investor(investor)

    if command == "/newinvestor":

        return create_investor_from_text(

            chat_id,

            args

        )

    # =========================

    # DEALS

    # =========================

    if command == "/deals":

        deals = get_deals(chat_id)

        if not deals:

            return "📭 გარიგებები ჯერ არ არის დამატებული."

        return deals_summary(deals)

    if command == "/deal":

        deal_id = parse_int_argument(args)

        if not deal_id:

            return (

                "გამოიყენე:\n"

                "/deal ID"

            )

        deal = get_deal(

            chat_id,

            deal_id

        )

        if not deal:

            return "გარიგება ვერ მოიძებნა."

        return format_deal(deal)

    # =========================

    # DOCUMENTS

    # =========================

    if command == "/documents":

        documents = get_documents(chat_id)

        return documents_summary(documents)

    if command == "/document":

        document_id = parse_int_argument(args)

        if not document_id:

            return (

                "გამოიყენე:\n"

                "/document ID"

            )

        document = get_document(

            chat_id,

            document_id

        )

        if not document:

            return "დოკუმენტი ვერ მოიძებნა."

        return format_document(document)

    # =========================

    # FINANCIAL

    # =========================

    if command == "/financial":

        project_id = parse_int_argument(args)

        if project_id:

            project = get_project(

                chat_id,

                project_id

            )

        else:

            project = get_primary_project(chat_id)

        if not project:

            return "ფინანსური ანალიზისთვის პროექტი ვერ მოიძებნა."

        try:

            snapshot = build_project_financial_snapshot(

                project

            )

            return format_financial_report(

                snapshot

            )

        except Exception as e:

            logging.exception(

                "Financial command error: %s",

                e

            )

            return (

                "ფინანსური ანალიზის შექმნა ვერ მოხერხდა."

            )

    # =========================

    # MEMORY

    # =========================

    if command == "/memory":

        return memory_summary(chat_id)

    if command == "/forget":

        memory_id = parse_int_argument(args)

        if not memory_id:

            return (

                "გამოიყენე:\n"

                "/forget ID"

            )

        try:

            deleted = delete_memory(

                chat_id,

                memory_id

            )

            if deleted:

                return (

                    f"🗑️ მეხსიერების ჩანაწერი "

                    f"#{memory_id} წაიშალა."

                )

            return "ასეთი მეხსიერების ჩანაწერი ვერ მოიძებნა."

        except Exception:

            logging.exception(

                "Memory delete error"

            )

            return "მეხსიერების წაშლა ვერ მოხერხდა."

    # =========================

    # UNKNOWN COMMAND

    # =========================

    return (

        "❓ ასეთი ბრძანება არ ვიცი.\n\n"

        "იხილე ყველა ხელმისაწვდომი ბრძანება:\n"

        "/help"

    )

print("GENIOSA PART 7/10 LOADED")# =========================

# PART 8/10 — MESSAGE ROUTER

# =========================

def normalize_text(text):

    if text is None:

        return ""

    return str(text).strip()

def is_command(text):

    text = normalize_text(text)

    return text.startswith("/")

def looks_like_project_request(text):

    t = normalize_text(text).lower()

    keywords = [

        "პროექტი",

        "პროექტის",

        "project",

        "უძრავი ქონება",

        "მშენებლობა",

        "დეველოპმენტი",

        "developer",

        "construction",

        "development",

    ]

    return any(k in t for k in keywords)

def looks_like_investor_request(text):

    t = normalize_text(text).lower()

    keywords = [

        "ინვესტორი",

        "ინვესტორები",

        "ინვესტიცია",

        "investor",

        "investors",

        "investment",

        "fund",

        "ფონდი",

    ]

    return any(k in t for k in keywords)

def looks_like_financial_request(text):

    t = normalize_text(text).lower()

    keywords = [

        "ფინანს",

        "მოგება",

        "შემოსავალი",

        "ხარჯი",

        "ბიუჯეტი",

        "profit",

        "revenue",

        "cost",

        "financial",

        "roi",

        "cash flow",

        "break even",

        "payback",

    ]

    return any(k in t for k in keywords)

def looks_like_deal_request(text):

    t = normalize_text(text).lower()

    keywords = [

        "გარიგება",

        "deal",

        "crm",

        "შეთანხმება",

        "ინვესტორის შეთავაზება",

        "კონტრაქტი",

        "ხელშეკრულება",

    ]

    return any(k in t for k in keywords)

def looks_like_document_request(text):

    t = normalize_text(text).lower()

    keywords = [

        "დოკუმენტი",

        "ფაილი",

        "pdf",

        "excel",

        "xlsx",

        "docx",

        "pptx",

        "ფაილები",

    ]

    return any(k in t for k in keywords)

def build_router_context(chat_id):

    parts = []

    try:

        project_context = build_project_context(chat_id)

        if project_context:

            parts.append(project_context)

    except Exception:

        logging.exception(

            "Could not build project context"

        )

    try:

        memory_context = build_memory_context(chat_id)

        if memory_context:

            parts.append(memory_context)

    except Exception:

        logging.exception(

            "Could not build memory context"

        )

    return "\n\n".join(parts)

def safe_ai_reply(chat_id, text):

    try:

        reply = ai_chat(

            chat_id,

            text

        )

        if reply:

            return reply

        return (

            "ამ მომენტში AI პასუხის მიღება ვერ მოხერხდა.\n"

            "გთხოვ, რამდენიმე წამში ისევ სცადო."

        )

    except Exception as e:

        logging.exception(

            "AI reply error: %s",

            e

        )

        return (

            "დაფიქსირდა დროებითი AI შეცდომა.\n"

            "გთხოვ, ისევ სცადო."

        )

def handle_text_message(chat_id, text):

    text = normalize_text(text)

    if not text:

        return (

            "მომწერე ტექსტი ან გამომიგზავნე "

            "ფაილი/სურათი."

        )

    # =========================

    # MEMORY

    # =========================

    if looks_like_memory_request(text):

        try:

            saved = save_memory_from_message(

                chat_id,

                text

            )

            if saved:

                return (

                    "✅ ინფორმაცია შევინახე "

                    "მეხსიერებაში.\n\n"

                    f"{saved}"

                )

        except Exception:

            logging.exception(

                "Memory save error"

            )

        return (

            "მეხსიერებაში შენახვა ვერ მოხერხდა."

        )

    # =========================

    # FINANCIAL

    # =========================

    if looks_like_financial_request(text):

        try:

            project = get_primary_project(

                chat_id

            )

            if project:

                snapshot = (

                    build_project_financial_snapshot(

                        project

                    )

                )

                report = format_financial_report(

                    snapshot

                )

                prompt = (

                    "მომხმარებელი სვამს ფინანსურ "

                    "კითხვას პროექტთან დაკავშირებით.\n\n"

                    "პროექტის ფინანსური მონაცემები:\n"

                    f"{report}\n\n"

                    "მომხმარებლის კითხვა:\n"

                    f"{text}\n\n"

                    "უპასუხე ქართულად. "

                    "გამოიყენე მხოლოდ მოცემული მონაცემები. "

                    "თუ რაიმე მონაცემი არ არის, "

                    "პირდაპირ თქვი რომ მონაცემი არ გვაქვს. "

                    "არ მოიგონო ციფრები."

                )

                answer = safe_ai_reply(

                    chat_id,

                    prompt

                )

                if answer:

                    return answer

        except Exception:

            logging.exception(

                "Financial routing error"

            )

    # =========================

    # PROJECT

    # =========================

    if looks_like_project_request(text):

        try:

            project = get_primary_project(

                chat_id

            )

            if project:

                context = build_project_context(

                    chat_id

                )

                prompt = (

                    "უპასუხე მომხმარებლის კითხვას "

                    "მისი პროექტის კონტექსტის გამოყენებით.\n\n"

                    f"{context}\n\n"

                    f"კითხვა:\n{text}\n\n"

                    "უპასუხე ქართულად, კონკრეტულად "

                    "და პრაქტიკულად. "

                    "არ მოიგონო ისეთი ფაქტები, "

                    "რომლებიც კონტექსტში არ არის."

                )

                answer = safe_ai_reply(

                    chat_id,

                    prompt

                )

                if answer:

                    return answer

        except Exception:

            logging.exception(

                "Project routing error"

            )

    # =========================

    # INVESTOR

    # =========================

    if looks_like_investor_request(text):

        try:

            investors = get_investors(

                chat_id

            )

            investor_text = investors_summary(

                investors

            )

            prompt = (

                "მომხმარებელი საუბრობს "

                "ინვესტორებთან დაკავშირებით.\n\n"

                f"შენახული ინვესტორები:\n"

                f"{investor_text}\n\n"

                f"მომხმარებლის მოთხოვნა:\n{text}\n\n"

                "უპასუხე ქართულად. "

                "თუ მონაცემთა ბაზაში ინფორმაცია "

                "არ არის, პირდაპირ აღნიშნე ეს. "

                "არ მოიგონო ინვესტორის მონაცემები."

            )

            answer = safe_ai_reply(

                chat_id,

                prompt

            )

            if answer:

                return answer

        except Exception:

            logging.exception(

                "Investor routing error"

            )

    # =========================

    # DEAL / CRM

    # =========================

    if looks_like_deal_request(text):

        try:

            deals = get_deals(

                chat_id

            )

            deal_text = deals_summary(

                deals

            )

            prompt = (

                "მომხმარებელი ეკითხება "

                "საინვესტიციო გარიგებებს ან CRM-ს.\n\n"

                f"შენახული გარიგებები:\n"

                f"{deal_text}\n\n"

                f"მოთხოვნა:\n{text}\n\n"

                "უპასუხე ქართულად და პრაქტიკულად. "

                "არ მოიგონო მონაცემები."

            )

            answer = safe_ai_reply(

                chat_id,

                prompt

            )

            if answer:

                return answer

        except Exception:

            logging.exception(

                "Deal routing error"

            )

    # =========================

    # DOCUMENTS

    # =========================

    if looks_like_document_request(text):

        try:

            docs = get_documents(

                chat_id

            )

            if docs:

                lines = [

                    "📄 შენახული დოკუმენტები:\n"

                ]

                for doc in docs[:20]:

                    doc_id = doc.get("id")

                    filename = (

                        doc.get("filename")

                        or "უცნობი ფაილი"

                    )

                    file_type = (

                        doc.get("file_type")

                        or ""

                    )

                    lines.append(

                        f"#{doc_id} — "

                        f"{filename} {file_type}"

                    )

                return "\n".join(lines)

        except Exception:

            logging.exception(

                "Document routing error"

            )

    # =========================

    # GENERAL AI

    # =========================

    return safe_ai_reply(

        chat_id,

        text

    )

def process_text_update(update):

    if not isinstance(update, dict):

        return None

    message = update.get(

        "message"

    ) or {}

    chat = message.get(

        "chat"

    ) or {}

    chat_id = chat.get(

        "id"

    )

    if not chat_id:

        return None

    if not user_allowed(chat_id):

        logging.warning(

            "Unauthorized user attempted access: %s",

            chat_id

        )

        return None

    text = message.get(

        "text"

    )

    if not text:

        return None

    text = normalize_text(

        text

    )

    # Save incoming message

    try:

        save_message(

            chat_id=chat_id,

            role="user",

            content=text,

            message_type="text",

        )

    except Exception:

        logging.exception(

            "Could not save incoming message"

        )

    # =========================

    # TELEGRAM COMMAND

    # =========================

    if is_command(text):

        try:

            result = handle_command(

                chat_id,

                text

            )

            if result:

                try:

                    save_message(

                        chat_id=chat_id,

                        role="assistant",

                        content=result,

                        message_type="command",

                    )

                except Exception:

                    logging.exception(

                        "Could not save command response"

                    )

                send_long_message(

                    chat_id,

                    result

                )

            return result

        except Exception as e:

            logging.exception(

                "Command processing error: %s",

                e

            )

            error_text = (

                "დაფიქსირდა ბრძანების "

                "დამუშავების შეცდომა.\n"

                "გთხოვ, ისევ სცადო."

            )

            send_message(

                chat_id,

                error_text

            )

            return error_text

    # =========================

    # NORMAL TEXT

    # =========================

    try:

        result = handle_text_message(

            chat_id,

            text

        )

        if not result:

            result = (

                "პასუხის მომზადება ვერ მოხერხდა."

            )

        try:

            save_message(

                chat_id=chat_id,

                role="assistant",

                content=result,

                message_type="text",

            )

        except Exception:

            logging.exception(

                "Could not save AI response"

            )

        send_long_message(

            chat_id,

            result

        )

        return result

    except Exception as e:

        logging.exception(

            "Text processing error: %s",

            e

        )

        error_text = (

            "დაფიქსირდა დროებითი შეცდომა.\n"

            "გთხოვ, ისევ სცადო."

        )

        send_message(

            chat_id,

            error_text

        )

        return error_text

print("GENIOSA PART 8/10 LOADED")# =========================

# PART 9/10 — TELEGRAM UPDATE ENGINE

# =========================

def process_update(update):

    """

    მთავარი Telegram update router.

    ამუშავებს:

    - ტექსტს

    - ბრძანებებს

    - ფოტოებს

    - დოკუმენტებს

    """

    if not isinstance(update, dict):

        return None

    try:

        # =========================

        # CALLBACK QUERY

        # =========================

        callback_query = update.get("callback_query")

        if callback_query:

            callback_message = (

                callback_query.get("message")

                or {}

            )

            callback_chat = (

                callback_message.get("chat")

                or {}

            )

            callback_chat_id = callback_chat.get(

                "id"

            )

            callback_data = (

                callback_query.get("data")

                or ""

            )

            if callback_chat_id:

                try:

                    telegram_url_data = {

                        "callback_query_id":

                            callback_query.get("id")

                    }

                    requests.post(

                        telegram_url(

                            "answerCallbackQuery"

                        ),

                        json=telegram_url_data,

                        timeout=10,

                    )

                except Exception:

                    pass

                if callback_data:

                    return process_text_update(

                        {

                            "message": {

                                "chat": {

                                    "id": callback_chat_id

                                },

                                "from": (

                                    callback_message.get(

                                        "from"

                                    )

                                    or {}

                                ),

                                "text": callback_data,

                            }

                        }

                    )

            return None

        # =========================

        # TEXT MESSAGE

        # =========================

        message = update.get(

            "message"

        )

        if not message:

            return None

        chat = message.get(

            "chat"

        ) or {}

        chat_id = chat.get(

            "id"

        )

        if not chat_id:

            return None

        if not user_allowed(chat_id):

            logging.warning(

                "Blocked Telegram user: %s",

                chat_id

            )

            return None

        # =========================

        # PHOTO

        # =========================

        if message.get("photo"):

            return process_photo_update(

                update

            )

        # =========================

        # DOCUMENT

        # =========================

        if message.get("document"):

            return process_document_update(

                update

            )

        # =========================

        # TEXT

        # =========================

        if message.get("text"):

            return process_text_update(

                update

            )

        # =========================

        # OTHER MESSAGE TYPES

        # =========================

        if message.get("voice"):

            send_message(

                chat_id,

                "🎤 Voice შეტყობინებების მხარდაჭერა "

                "ამ ვერსიაში ჯერ არ არის ჩართული."

            )

            return None

        if message.get("video"):

            send_message(

                chat_id,

                "🎥 ვიდეო ფაილების დამუშავება "

                "ამ ვერსიაში ჯერ არ არის ჩართული."

            )

            return None

        if message.get("audio"):

            send_message(

                chat_id,

                "🎵 აუდიო ფაილების დამუშავება "

                "ამ ვერსიაში ჯერ არ არის ჩართული."

            )

            return None

        return None

    except Exception as e:

        logging.exception(

            "process_update error: %s",

            e

        )

        try:

            chat = (

                update.get("message", {})

                .get("chat", {})

            )

            chat_id = chat.get("id")

            if chat_id:

                send_message(

                    chat_id,

                    "⚠️ შეტყობინების დამუშავებისას "

                    "დაფიქსირდა შეცდომა."

                )

        except Exception:

            pass

        return None

def telegram_get_updates(

    offset=None,

    timeout=30

):

    """

    იღებს ახალ Telegram updates-ს.

    """

    params = {

        "timeout": timeout,

        "allowed_updates": json.dumps(

            [

                "message",

                "callback_query",

            ]

        ),

    }

    if offset is not None:

        params["offset"] = offset

    response = requests.get(

        telegram_url(

            "getUpdates"

        ),

        params=params,

        timeout=timeout + 10,

    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):

        raise RuntimeError(

            data.get(

                "description",

                "Telegram API error"

            )

        )

    return data.get(

        "result",

        []

    )

def telegram_delete_webhook():

    """

    Webhook-ის წაშლა საჭიროა,

    თუ ბოტს polling რეჟიმში ვუშვებთ.

    """

    try:

        response = requests.post(

            telegram_url(

                "deleteWebhook"

            ),

            json={

                "drop_pending_updates": False

            },

            timeout=15,

        )

        data = response.json()

        if data.get("ok"):

            logging.info(

                "Telegram webhook deleted."

            )

            return True

        logging.warning(

            "Could not delete Telegram webhook: %s",

            data

        )

    except Exception as e:

        logging.exception(

            "deleteWebhook error: %s",

            e

        )

    return False

def telegram_bot_info():

    """

    ამოწმებს Telegram Bot Token-ს.

    """

    try:

        response = requests.get(

            telegram_url(

                "getMe"

            ),

            timeout=15,

        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):

            return None

        return data.get(

            "result"

        )

    except Exception as e:

        logging.exception(

            "getMe error: %s",

            e

        )

        return None

def polling_loop():

    """

    ძირითადი Telegram polling loop.

    Render-ზე ერთი polling thread მუშაობს.

    """

    global TELEGRAM_OFFSET

    logging.info(

        "Geniosa Telegram polling started."

    )

    telegram_delete_webhook()

    bot = telegram_bot_info()

    if bot:

        logging.info(

            "Telegram bot connected: @%s",

            bot.get("username")

        )

    else:

        logging.error(

            "Telegram bot authentication failed."

        )

        return

    consecutive_errors = 0

    while not POLLING_STOP.is_set():

        try:

            updates = telegram_get_updates(

                offset=TELEGRAM_OFFSET,

                timeout=30,

            )

            consecutive_errors = 0

            for update in updates:

                if POLLING_STOP.is_set():

                    break

                update_id = update.get(

                    "update_id"

                )

                if update_id is not None:

                    TELEGRAM_OFFSET = (

                        update_id + 1

                    )

                try:

                    process_update(

                        update

                    )

                except Exception:

                    logging.exception(

                        "Single update processing error"

                    )

        except requests.exceptions.Timeout:

            # Long polling timeout is normal.

            continue

        except requests.exceptions.RequestException as e:

            consecutive_errors += 1

            logging.error(

                "Telegram network error "

                "(%s): %s",

                consecutive_errors,

                e,

            )

            sleep_time = min(

                30,

                2 * consecutive_errors

            )

            time.sleep(

                sleep_time

            )

        except Exception as e:

            consecutive_errors += 1

            logging.exception(

                "Polling loop error: %s",

                e

            )

            time.sleep(

                min(

                    30,

                    2 * consecutive_errors

                )

            )

    logging.info(

        "Geniosa Telegram polling stopped."

    )

def start_polling():

    """

    იწყებს Telegram polling-ს მხოლოდ ერთხელ.

    """

    global POLLING_THREAD

    if POLLING_THREAD is not None:

        if POLLING_THREAD.is_alive():

            logging.info(

                "Polling thread already running."

            )

            return

    POLLING_STOP.clear()

    POLLING_THREAD = threading.Thread(

        target=polling_loop,

        name="geniosa-telegram-polling",

        daemon=True,

    )

    POLLING_THREAD.start()

    logging.info(

        "Polling thread started."

    )

def stop_polling():

    """

    აჩერებს polling thread-ს.

    """

    global POLLING_THREAD

    POLLING_STOP.set()

    if POLLING_THREAD is not None:

        try:

            POLLING_THREAD.join(

                timeout=5

            )

        except Exception:

            pass

    POLLING_THREAD = None

    logging.info(

        "Polling stopped."

    )

def telegram_send_startup_message():

    """

    სურვილის შემთხვევაში აგზავნის startup შეტყობინებას

    მხოლოდ მაშინ, თუ OWNER_ID განსაზღვრულია.

    """

    if not OWNER_ID:

        return False

    try:

        send_message(

            OWNER_ID,

            (

                "🟢 Geniosa ჩაირთო.\n\n"

                "Telegram bot მუშაობს.\n"

                "გამოიყენე /help ბრძანება."

            )

        )

        return True

    except Exception:

        logging.exception(

            "Startup Telegram message failed."

        )

        return False

print("GENIOSA PART 9/10 LOADED")# =========================

# PART 10/10 — FINAL STARTUP

# =========================

from contextlib import asynccontextmanager

@asynccontextmanager

async def lifespan(app: FastAPI):

    """

    FastAPI application lifecycle.

    """

    logging.info(

        "===================================="

    )

    logging.info(

        "GENIOSA STARTUP"

    )

    logging.info(

        "===================================="

    )

    # =========================

    # DATABASE

    # =========================

    try:

        init_db()

        logging.info(

            "Database initialization: OK"

        )

    except Exception as e:

        logging.exception(

            "Database initialization failed: %s",

            e

        )

    # =========================

    # TELEGRAM

    # =========================

    try:

        bot = telegram_bot_info()

        if bot:

            logging.info(

                "Telegram connection: OK"

            )

            logging.info(

                "Bot username: @%s",

                bot.get("username")

            )

        else:

            logging.error(

                "Telegram connection: FAILED"

            )

    except Exception as e:

        logging.exception(

            "Telegram startup check failed: %s",

            e

        )

    # =========================

    # START POLLING

    # =========================

    try:

        start_polling()

        logging.info(

            "Telegram polling: STARTED"

        )

    except Exception as e:

        logging.exception(

            "Could not start Telegram polling: %s",

            e

        )

    # =========================

    # STARTUP MESSAGE

    # =========================

    try:

        # მცირე დაყოვნება, რომ polling thread

        # და Telegram connection სტაბილურად გაეშვას.

        time.sleep(1)

        telegram_send_startup_message()

    except Exception:

        logging.exception(

            "Startup message error"

        )

    logging.info(

        "===================================="

    )

    logging.info(

        "GENIOSA IS READY"

    )

    logging.info(

        "===================================="

    )

    # Application მუშაობს

    yield

    # =========================

    # SHUTDOWN

    # =========================

    logging.info(

        "===================================="

    )

    logging.info(

        "GENIOSA SHUTDOWN"

    )

    logging.info(

        "===================================="

    )

    try:

        stop_polling()

    except Exception:

        logging.exception(

            "Polling shutdown error"

        )

# =========================

# FASTAPI LIFESPAN

# =========================

app.router.lifespan_context = lifespan

# =========================

# FINAL HEALTH ENDPOINTS

# =========================

@app.get("/health")

def final_health():

    return {

        "status": "ok",

        "service": "geniosa",

        "telegram": bool(TELEGRAM_BOT_TOKEN),

        "gemini": bool(GEMINI_API_KEY),

        "database": bool(DATABASE_URL),

    }

@app.get("/telegram")

def telegram_status():

    bot = telegram_bot_info()

    if not bot:

        return {

            "status": "error",

            "telegram": False,

        }

    return {

        "status": "ok",

        "telegram": True,

        "bot_id": bot.get("id"),

        "username": bot.get("username"),

        "first_name": bot.get("first_name"),

    }

@app.get("/db")

def database_status():

    conn = None

    try:

        conn = db()

        with conn.cursor() as cur:

            cur.execute(

                "SELECT NOW() AS current_time"

            )

            row = cur.fetchone()

        return {

            "status": "ok",

            "database": True,

            "time": str(

                row.get("current_time")

                if row

                else ""

            ),

        }

    except Exception as e:

        logging.exception(

            "Database health check failed: %s",

            e

        )

        return {

            "status": "error",

            "database": False,

            "error": str(e),

        }

    finally:

        if conn:

            conn.close()

# =========================

# ROOT INFORMATION

# =========================

@app.get("/")

def final_root():

    return {

        "name": "Geniosa",

        "version": "4.0",

        "status": "running",

        "message": "Geniosa AI Business Assistant",

    }

# =========================

# STARTUP MARKER

# =========================

print("====================================")

print("GENIOSA 4.0 — ALL PARTS LOADED")

print("GENIOSA IS READY")

print("====================================")
