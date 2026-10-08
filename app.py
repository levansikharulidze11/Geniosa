# ============================================================
# GENIOSA 5.1
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + FastAPI
#
# IMPORTANT:
# - Existing PostgreSQL data is preserved.
# - No DROP TABLE.
# - No TRUNCATE.
# - Automatic migrations.
# - Safe legacy-schema handling.
# - PostgreSQL connection pool.
# - PostgreSQL advisory lock for Telegram polling.
# - Persistent facts / memories / projects / tasks / decisions.
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
from psycopg2 import sql
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI


# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "5.1"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GENIOSA_OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

PORT = int(os.getenv("PORT", "10000"))

TELEGRAM_TIMEOUT = 35
TELEGRAM_RETRY_DELAY = 5

DB_POOL_MIN = 1
DB_POOL_MAX = 5

POLLING_LOCK_KEY = 735050


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).isoformat()
    print(f"[GENIOSA {now}] {message}", flush=True)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(title="GENIOSA", version=APP_VERSION)


# ============================================================
# GLOBAL STATE
# ============================================================

db_pool = None

polling_thread = None
polling_stop_event = threading.Event()

polling_lock_connection = None

telegram_offset = 0

startup_completed = False


# ============================================================
# BASIC HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def safe_key(value, fallback="item"):
    value = clean_text(value).lower()

    value = re.sub(
        r"[^a-z0-9ა-ჰ_]+",
        "_",
        value
    )

    value = re.sub(
        r"_+",
        "_",
        value
    ).strip("_")

    return value[:180] or fallback


def json_text(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False
        )
    except Exception:
        return str(value)


# ============================================================
# DATABASE CONNECTION
# ============================================================

def init_db_pool():
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    if db_pool is not None:
        return

    db_pool = ThreadedConnectionPool(
        DB_POOL_MIN,
        DB_POOL_MAX,
        DATABASE_URL
    )

    log("DATABASE CONNECTION POOL READY")


def get_db_connection():
    if db_pool is None:
        raise RuntimeError("Database pool is not initialized")

    return db_pool.getconn()


def release_db_connection(conn):
    if db_pool is not None and conn is not None:
        try:
            db_pool.putconn(conn)
        except Exception:
            pass


# ============================================================
# DATABASE EXECUTOR
# ============================================================

def db_execute(
    query,
    params=None,
    fetch=False,
    fetchone=False,
    commit=False
):
    """
    Safe database executor.

    IMPORTANT FIX:
    psycopg2 cursor must receive cursor_factory=RealDictCursor,
    NOT RealDictCursor as a positional argument.
    """

    conn = None
    cur = None

    try:
        conn = get_db_connection()

        if fetch or fetchone:
            cur = conn.cursor(
                cursor_factory=RealDictCursor
            )
        else:
            cur = conn.cursor()

        cur.execute(query, params or ())

        result = None

        if fetchone:
            result = cur.fetchone()

        elif fetch:
            result = cur.fetchall()

        if commit:
            conn.commit()

        return result

    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass

        raise

    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass

        release_db_connection(conn)


# ============================================================
# DATABASE INTROSPECTION
# ============================================================

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
        fetchone=True
    )

    return bool(row and row["exists"])


def get_columns(table_name):
    rows = db_execute(
        """
        SELECT
            column_name,
            data_type,
            is_nullable,
            column_default,
            character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
        fetch=True
    )

    return {
        row["column_name"]: row
        for row in (rows or [])
    }


def column_exists(table_name, column_name):
    columns = get_columns(table_name)
    return column_name in columns


def add_column(table_name, column_name, column_definition):
    if not table_exists(table_name):
        return

    if column_exists(table_name, column_name):
        return

    query = sql.SQL(
        "ALTER TABLE {} ADD COLUMN {} {}"
    ).format(
        sql.Identifier(table_name),
        sql.Identifier(column_name),
        sql.SQL(column_definition)
    )

    db_execute(
        query.as_string(
            psycopg2.connect(DATABASE_URL)
        ),
        commit=True
    )

    log(
        f"ADDED COLUMN {table_name}.{column_name}"
    )


# ============================================================
# SAFER ADD COLUMN
# ============================================================

def safe_add_column(
    table_name,
    column_name,
    column_definition
):
    """
    Same purpose as add_column but avoids creating a second
    temporary connection just for SQL quoting.
    """

    if not table_exists(table_name):
        return False

    if column_exists(table_name, column_name):
        return False

    conn = None
    cur = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        query = sql.SQL(
            "ALTER TABLE {} ADD COLUMN {} {}"
        ).format(
            sql.Identifier(table_name),
            sql.Identifier(column_name),
            sql.SQL(column_definition)
        )

        cur.execute(query)
        conn.commit()

        log(
            f"ADDED COLUMN {table_name}.{column_name}"
        )

        return True

    except Exception:
        if conn:
            conn.rollback()

        raise

    finally:
        if cur:
            cur.close()

        release_db_connection(conn)


# ============================================================
# TABLE CREATION
# ============================================================

def create_table_if_missing(table_name, create_sql):
    if table_exists(table_name):
        return

    db_execute(
        create_sql,
        commit=True
    )

    log(
        f"CREATED TABLE {table_name}"
    )


# ============================================================
# DATABASE MIGRATION
# ============================================================

def migrate_database():

    # --------------------------------------------------------
    # messages
    # --------------------------------------------------------

    create_table_if_missing(
        "messages",
        """
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            role TEXT,
            message TEXT,
            text TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    safe_add_column(
        "messages",
        "chat_id",
        "BIGINT"
    )

    safe_add_column(
        "messages",
        "role",
        "TEXT"
    )

    safe_add_column(
        "messages",
        "message",
        "TEXT"
    )

    safe_add_column(
        "messages",
        "text",
        "TEXT"
    )

    safe_add_column(
        "messages",
        "created_at",
        "TIMESTAMPTZ DEFAULT NOW()"
    )

    # Copy legacy text -> message
    try:
        if column_exists("messages", "text") and \
           column_exists("messages", "message"):

            db_execute(
                """
                UPDATE messages
                SET message = text
                WHERE message IS NULL
                  AND text IS NOT NULL
                """,
                commit=True
            )
    except Exception as e:
        log(
            f"messages text->message migration warning: {e!r}"
        )

    # Copy message -> text
    try:
        if column_exists("messages", "text") and \
           column_exists("messages", "message"):

            db_execute(
                """
                UPDATE messages
                SET text = message
                WHERE text IS NULL
                  AND message IS NOT NULL
                """,
                commit=True
            )
    except Exception as e:
        log(
            f"messages message->text migration warning: {e!r}"
        )

    # --------------------------------------------------------
    # memories
    # --------------------------------------------------------

    create_table_if_missing(
        "memories",
        """
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            memory TEXT,
            memory_type TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 5,
            source TEXT DEFAULT 'user',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    safe_add_column(
        "memories",
        "chat_id",
        "BIGINT"
    )

    safe_add_column(
        "memories",
        "content",
        "TEXT"
    )

    safe_add_column(
        "memories",
        "memory",
        "TEXT"
    )

    safe_add_column(
        "memories",
        "memory_type",
        "TEXT DEFAULT 'general'"
    )

    safe_add_column(
        "memories",
        "importance",
        "INTEGER DEFAULT 5"
    )

    safe_add_column(
        "memories",
        "source",
        "TEXT DEFAULT 'user'"
    )

    safe_add_column(
        "memories",
        "created_at",
        "TIMESTAMPTZ DEFAULT NOW()"
    )

    safe_add_column(
        "memories",
        "updated_at",
        "TIMESTAMPTZ DEFAULT NOW()"
    )

    try:
        db_execute(
            """
            UPDATE memories
            SET content = memory
            WHERE content IS NULL
              AND memory IS NOT NULL
            """,
            commit=True
        )
    except Exception as e:
        log(
            f"memories memory->content warning: {e!r}"
        )

    try:
        db_execute(
            """
            UPDATE memories
            SET memory = content
            WHERE memory IS NULL
              AND content IS NOT NULL
            """,
            commit=True
        )
    except Exception as e:
        log(
            f"memories content->memory warning: {e!r}"
        )

    # --------------------------------------------------------
    # memory_events
    # --------------------------------------------------------

    create_table_if_missing(
        "memory_events",
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            memory_id BIGINT,
            action TEXT,
            content TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    safe_add_column(
        "memory_events",
        "chat_id",
        "BIGINT"
    )

    safe_add_column(
        "memory_events",
        "memory_id",
        "BIGINT"
    )

    safe_add_column(
        "memory_events",
        "action",
        "TEXT"
    )

    safe_add_column(
        "memory_events",
        "content",
        "TEXT"
    )

    safe_add_column(
        "memory_events",
        "created_at",
        "TIMESTAMPTZ DEFAULT NOW()"
    )

    # --------------------------------------------------------
    # facts
    # --------------------------------------------------------

    create_table_if_missing(
        "facts",
        """
        CREATE TABLE IF NOT EXISTS facts (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            fact_key TEXT,
            value TEXT,
            fact TEXT,
            content TEXT,
            source TEXT DEFAULT 'user',
            status TEXT DEFAULT 'CONFIRMED',
            category TEXT DEFAULT 'general',
            memory_type TEXT DEFAULT 'fact',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    safe_add_column(
        "facts",
        "chat_id",
        "BIGINT"
    )

    safe_add_column(
        "facts",
        "fact_key",
        "TEXT"
    )

    safe_add_column(
        "facts",
        "value",
        "TEXT"
    )

    safe_add_column(
        "facts",
        "fact",
        "TEXT"
    )

    safe_add_column(
        "facts",
        "content",
        "TEXT"
    )

    safe_add_column(
        "facts",
        "source",
        "TEXT DEFAULT 'user'"
    )

    safe_add_column(
        "facts",
        "status",
        "TEXT DEFAULT 'CONFIRMED'"
    )

    safe_add_column(
        "facts",
        "category",
        "TEXT DEFAULT 'general'"
    )

    safe_add_column(
        "facts",
        "memory_type",
        "TEXT DEFAULT 'fact'"
    )

    safe_add_column(
        "facts",
        "created_at",
        "TIMESTAMPTZ DEFAULT NOW()"
    )

    safe_add_column(
        "facts",
        "updated_at",
        "TIMESTAMPTZ DEFAULT NOW()"
    )

    # --------------------------------------------------------
    # Legacy facts migration
    # --------------------------------------------------------

    try:
        db_execute(
            """
            UPDATE facts
            SET content = COALESCE(
                content,
                fact,
                value
            )
            WHERE content IS NULL
              AND (
                  fact IS NOT NULL
                  OR value IS NOT NULL
              )
            """,
            commit=True
        )
    except Exception as e:
        log(
            f"facts legacy->content warning: {e!r}"
        )

    try:
        db_execute(
            """
            UPDATE facts
            SET fact = content
            WHERE fact IS NULL
              AND content IS NOT NULL
            """,
            commit=True
        )
    except Exception as e:
        log(
            f"facts content->fact warning: {e!r}"
        )

    try:
        db_execute(
            """
            UPDATE facts
            SET value = content
            WHERE value IS NULL
              AND content IS NOT NULL
            """,
            commit=True
        )
    except Exception as e:
        log(
            f"facts content->value warning: {e!r}"
        )

    # --------------------------------------------------------
    # CRITICAL LEGACY FACT_KEY FIX
    # --------------------------------------------------------

    try:
        if column_exists("facts", "fact_key"):

            # First use existing fact/value/content.
            db_execute(
                """
                UPDATE facts
                SET fact_key =
                    COALESCE(
                        NULLIF(fact_key, ''),
                        'legacy_fact_' || id::TEXT
                    )
                WHERE fact_key IS NULL
                   OR fact_key = ''
                """,
                commit=True
            )

            log(
                "FACT_KEY LEGACY MIGRATION COMPLETE"
            )

    except Exception as e:
        log(
            f"FACT_KEY MIGRATION WARNING: {e!r}"
        )

    # --------------------------------------------------------
    # Normalize facts
    # --------------------------------------------------------

    try:
        db_execute(
            """
            UPDATE facts
            SET status = 'CONFIRMED'
            WHERE status IS NULL
               OR TRIM(status) = ''
            """,
            commit=True
        )
    except Exception as e:
        log(
            f"facts status normalization warning: {e!r}"
        )

    # --------------------------------------------------------
    # fact_history
    # --------------------------------------------------------

    create_table_if_missing(
        "fact_history",
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id BIGSERIAL PRIMARY KEY,
            fact_id BIGINT,
            chat_id BIGINT,
            old_content TEXT,
            new_content TEXT,
            action TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # decisions
    # --------------------------------------------------------

    create_table_if_missing(
        "decisions",
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            content TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # tasks
    # --------------------------------------------------------

    create_table_if_missing(
        "tasks",
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            description TEXT,
            status TEXT DEFAULT 'OPEN',
            due_date TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # projects
    # --------------------------------------------------------

    create_table_if_missing(
        "projects",
        """
        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            name TEXT,
            description TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # research
    # --------------------------------------------------------

    create_table_if_missing(
        "research",
        """
        CREATE TABLE IF NOT EXISTS research (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            topic TEXT,
            content TEXT,
            source TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # financial_models
    # --------------------------------------------------------

    create_table_if_missing(
        "financial_models",
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            project_name TEXT,
            model_data TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # bot_projects
    # --------------------------------------------------------

    create_table_if_missing(
        "bot_projects",
        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            name TEXT,
            description TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # bot_files
    # --------------------------------------------------------

    create_table_if_missing(
        "bot_files",
        """
        CREATE TABLE IF NOT EXISTS bot_files (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            filename TEXT,
            filepath TEXT,
            description TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # --------------------------------------------------------
    # seed metadata
    # --------------------------------------------------------

    create_table_if_missing(
        "system_seed_meta",
        """
        CREATE TABLE IF NOT EXISTS system_seed_meta (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            seed_version TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(chat_id, seed_version)
        )
        """
    )

    # --------------------------------------------------------
    # indexes
    # --------------------------------------------------------

    index_statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_messages_chat_id
        ON messages(chat_id)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_messages_created_at
        ON messages(created_at)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_memories_chat_id
        ON memories(chat_id)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_facts_chat_id
        ON facts(chat_id)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_facts_status
        ON facts(status)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_tasks_chat_id
        ON tasks(chat_id)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_projects_chat_id
        ON projects(chat_id)
        """
    ]

    for statement in index_statements:
        try:
            db_execute(
                statement,
                commit=True
            )
        except Exception as e:
            log(
                f"INDEX WARNING: {e!r}"
            )

    log(
        "DATABASE INITIALIZED AND MIGRATED SUCCESSFULLY"
    )


# ============================================================
# SYSTEM FACTS
# ============================================================

SYSTEM_FACTS = [

    (
        "company_name",
        "SAMTISI CONSTRUCTION LLC"
    ),

    (
        "company_id",
        "406391202"
    ),

    (
        "company_founded",
        "December 5, 2022"
    ),

    (
        "company_location",
        "Tbilisi, Georgia"
    ),

    (
        "company_type",
        "Construction & Development Company"
    ),

    (
        "company_profile",
        "Georgian construction and development company focused on residential, commercial and infrastructure projects."
    ),

    (
        "nikkea12_location",
        "Kutaisi, Nikkea 12, Georgia"
    ),

    (
        "nikkea12_land_area",
        "3,070 m²"
    ),

    (
        "nikkea12_saleable_area",
        "10,854 m²"
    ),

    (
        "nikkea12_hotel_rooms_area",
        "7,212 m²"
    ),

    (
        "nikkea12_monolith_area",
        "21,500 m²"
    ),

    (
        "nikkea12_architecture_area",
        "16,000 m²"
    ),

    (
        "nikkea12_first_floor_commercial",
        "836 m²"
    ),

    (
        "nikkea12_second_floor_commercial",
        "1,006 m²"
    ),

    (
        "nikkea12_rooms",
        "Floors 3–14, total 7,212 m²"
    ),

    (
        "nikkea12_concept",
        "Hotel-type apartments, casino, shopping center, restaurant and top-floor lounge bar."
    ),

    (
        "nikkea12_landowner_requirement",
        "1,800 m² apartments + 25 parking spaces + $300,000 cash."
    ),

    (
        "nikkea12_monolith_cost",
        "$170/m²"
    ),

    (
        "nikkea12_fitout_assumption",
        "Up to $450/m²"
    ),

    (
        "nikkea12_apartment_price",
        "$1,500–1,800/m²"
    ),

    (
        "nikkea12_commercial_price",
        "$2,500/m²"
    ),

    (
        "nikkea12_jv_share",
        "Investor 80% / SAMTISI operator 20%"
    ),

    (
        "nikkea12_investor_contribution",
        "35% of total project cost + $300,000"
    ),

    (
        "nikkea12_bank_financing",
        "Remaining financing expected from bank funding."
    ),

    (
        "nikkea12_bank_interest",
        "11.5–13.5% maximum assumption."
    ),

    (
        "nikkea12_property_company",
        "Separate property/service company proposed as 50/50 between investor and SAMTISI."
    ),

    (
        "nikkea12_rental_split",
        "30% property/service company / 70% apartment owner."
    ),

    (
        "nikkea12_daily_rental",
        "$150–300 per apartment per day target."
    ),

    (
        "nikkea12_casino",
        "1,006 m² casino area is intended to be retained rather than sold."
    ),

    (
        "nikkea12_casino_value",
        "Target valuation at least $2,000/m²."
    ),

    (
        "nikkea12_casino_rent",
        "Potential target rent at least $50/m²/month."
    ),

    (
        "samgori_location",
        "Giorgi Naderishvili Street, Samgori district, Tbilisi, Georgia"
    ),

    (
        "samgori_land_area",
        "7,390 m²"
    ),

    (
        "samgori_build_area",
        "46,131 m²"
    ),

    (
        "samgori_construction_volume",
        "41,874 m³"
    ),

    (
        "samgori_saleable_area",
        "30,540 m² excluding parking"
    ),

    (
        "samgori_parking",
        "6,516 m²"
    ),

    (
        "samgori_residential",
        "22,143 m²"
    ),

    (
        "samgori_summer_area",
        "5,040 m²"
    ),

    (
        "samgori_commercial",
        "1,647 m²"
    ),

    (
        "samgori_office",
        "1,710 m²"
    ),

    (
        "samgori_construction_cost",
        "Approximately $253/m² of saleable area."
    ),

    (
        "samgori_land_cost",
        "$7.75 million"
    ),

    (
        "samgori_construction_budget",
        "$15 million"
    ),

    (
        "samgori_total_capital",
        "Approximately $23.5 million."
    ),

    (
        "samgori_expected_net_profit",
        "Approximately $10 million."
    ),

    (
        "golden_lake_main_land",
        "46 hectares"
    ),

    (
        "golden_lake_total_area",
        "Lake and surrounding area approximately 70–80 hectares."
    ),

    (
        "golden_lake_land_budget",
        "$35–40 million"
    ),

    (
        "golden_lake_planned_construction",
        "Approximately 250,000 m²"
    ),

    (
        "golden_lake_hotel",
        "5-star 200-room hotel + aquapark + casino."
    ),

    (
        "golden_lake_second_hotel",
        "4-star hotel with approximately 100–120 rooms."
    ),

    (
        "golden_lake_arena",
        "Sports/concert arena for approximately 10,000 guests, around 20,000–30,000 m²."
    ),

    (
        "golden_lake_apartments",
        "Approximately 150,000 m² sale area."
    ),

    (
        "golden_lake_commercial",
        "Approximately 30,000 m² commercial area."
    ),

    (
        "golden_lake_apartment_fitout",
        "$1,000–1,200/m²"
    ),

    (
        "golden_lake_apartment_sale",
        "$2,500–3,000/m²"
    ),

    (
        "golden_lake_commercial_sale",
        "$3,500–5,000/m²"
    )
]


# ============================================================
# FACT STORAGE
# ============================================================

def create_fact(
    chat_id,
    fact_key,
    content,
    source="system",
    category="general",
    status="CONFIRMED"
):

    content = clean_text(content)

    if not content:
        return None

    fact_key = safe_key(
        fact_key,
        fallback="fact"
    )

    try:

        existing = db_execute(
            """
            SELECT id
            FROM facts
            WHERE chat_id = %s
              AND LOWER(
                    COALESCE(
                        content,
                        fact,
                        value,
                        ''
                    )
                  ) = LOWER(%s)
            ORDER BY id
            LIMIT 1
            """,
            (
                chat_id,
                content
            ),
            fetchone=True
        )

        if existing:

            db_execute(
                """
                UPDATE facts
                SET
                    fact_key = %s,
                    content = %s,
                    fact = %s,
                    value = %s,
                    source = %s,
                    status = %s,
                    category = %s,
                    memory_type = 'fact',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    fact_key,
                    content,
                    content,
                    content,
                    source,
                    status,
                    category,
                    existing["id"]
                ),
                commit=True
            )

            return existing["id"]

        # ----------------------------------------------------
        # Dynamic insert based on columns.
        #
        # This protects against old legacy schemas.
        # ----------------------------------------------------

        columns = get_columns("facts")

        data = {
            "chat_id": chat_id,
            "fact_key": fact_key,
            "value": content,
            "fact": content,
            "content": content,
            "source": source,
            "status": status,
            "category": category,
            "memory_type": "fact"
        }

        insert_columns = []
        insert_values = []

        for key, value in data.items():

            if key in columns:
                insert_columns.append(
                    sql.Identifier(key)
                )
                insert_values.append(value)

        if "fact_key" in columns and \
           "fact_key" not in [
               str(x) for x in insert_columns
           ]:
            raise RuntimeError(
                "facts.fact_key could not be populated"
            )

        # Use parameterized SQL safely.
        placeholders = sql.SQL(", ").join(
            [sql.Placeholder() for _ in insert_values]
        )

        query = sql.SQL(
            """
            INSERT INTO {}
            ({})
            VALUES ({})
            RETURNING id
            """
        ).format(
            sql.Identifier("facts"),
            sql.SQL(", ").join(insert_columns),
            placeholders
        )

        conn = None
        cur = None

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute(
                query,
                insert_values
            )

            row = cur.fetchone()
            conn.commit()

            return row[0] if row else None

        finally:
            if cur:
                cur.close()

            release_db_connection(conn)

    except Exception as e:

        log(
            f"CREATE_FACT ERROR key={fact_key}: {e!r}"
        )

        return None


def create_memory(
    chat_id,
    content,
    memory_type="general",
    importance=5,
    source="user"
):

    content = clean_text(content)

    if not content:
        return None

    try:

        existing = db_execute(
            """
            SELECT id
            FROM memories
            WHERE chat_id = %s
              AND LOWER(
                    COALESCE(content, memory, '')
                  ) = LOWER(%s)
            ORDER BY id
            LIMIT 1
            """,
            (
                chat_id,
                content
            ),
            fetchone=True
        )

        if existing:

            db_execute(
                """
                UPDATE memories
                SET
                    content = %s,
                    memory = %s,
                    memory_type = %s,
                    importance = %s,
                    source = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    content,
                    content,
                    memory_type,
                    importance,
                    source,
                    existing["id"]
                ),
                commit=True
            )

            return existing["id"]

        columns = get_columns("memories")

        data = {
            "chat_id": chat_id,
            "content": content,
            "memory": content,
            "memory_type": memory_type,
            "importance": importance,
            "source": source
        }

        insert_columns = []
        insert_values = []

        for key, value in data.items():

            if key in columns:
                insert_columns.append(
                    sql.Identifier(key)
                )
                insert_values.append(value)

        placeholders = sql.SQL(", ").join(
            [sql.Placeholder() for _ in insert_values]
        )

        query = sql.SQL(
            """
            INSERT INTO {}
            ({})
            VALUES ({})
            RETURNING id
            """
        ).format(
            sql.Identifier("memories"),
            sql.SQL(", ").join(insert_columns),
            placeholders
        )

        conn = None
        cur = None

        try:
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute(
                query,
                insert_values
            )

            row = cur.fetchone()
            conn.commit()

            memory_id = row[0] if row else None

        finally:
            if cur:
                cur.close()

            release_db_connection(conn)

        if memory_id:

            try:
                db_execute(
                    """
                    INSERT INTO memory_events
                    (chat_id, memory_id, action, content)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        chat_id,
                        memory_id,
                        "CREATE",
                        content
                    ),
                    commit=True
                )
            except Exception as e:
                log(
                    f"MEMORY EVENT WARNING: {e!r}"
                )

        return memory_id

    except Exception as e:

        log(
            f"CREATE_MEMORY ERROR: {e!r}"
        )

        return None


# ============================================================
# GET FACTS / MEMORIES
# ============================================================

def get_facts(chat_id, limit=60):

    try:

        rows = db_execute(
            """
            SELECT
                id,
                fact_key,
                COALESCE(
                    NULLIF(content, ''),
                    NULLIF(fact, ''),
                    value
                ) AS content,
                source,
                status,
                category
            FROM facts
            WHERE chat_id = %s
            ORDER BY
                CASE
                    WHEN status = 'CONFIRMED'
                    THEN 0
                    ELSE 1
                END,
                id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        )

        return rows or []

    except Exception as e:

        log(
            f"GET_FACTS ERROR: {e!r}"
        )

        return []


def get_memories(chat_id, limit=25):

    try:

        rows = db_execute(
            """
            SELECT
                id,
                COALESCE(
                    NULLIF(content, ''),
                    memory
                ) AS content,
                memory_type,
                importance,
                source
            FROM memories
            WHERE chat_id = %s
            ORDER BY
                importance DESC,
                id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        )

        return rows or []

    except Exception as e:

        log(
            f"GET_MEMORIES ERROR: {e!r}"
        )

        return []


def get_recent_messages(chat_id, limit=12):

    try:

        rows = db_execute(
            """
            SELECT
                role,
                COALESCE(
                    NULLIF(message, ''),
                    text
                ) AS content,
                created_at
            FROM messages
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        )

        rows = rows or []

        rows.reverse()

        return rows

    except Exception as e:

        log(
            f"GET_MESSAGES ERROR: {e!r}"
        )

        return []


# ============================================================
# SAVE MESSAGE
# ============================================================

def save_message(
    chat_id,
    role,
    message
):

    message = clean_text(message)

    if not message:
        return None

    try:

        columns = get_columns("messages")

        data = {
            "chat_id": chat_id,
            "role": role,
            "message": message,
            "text": message
        }

        insert_columns = []
        insert_values = []

        for key, value in data.items():

            if key in columns:
                insert_columns.append(
                    sql.Identifier(key)
                )
                insert_values.append(value)

        placeholders = sql.SQL(", ").join(
            [sql.Placeholder() for _ in insert_values]
        )

        query = sql.SQL(
            """
            INSERT INTO {}
            ({})
            VALUES ({})
            RETURNING id
            """
        ).format(
            sql.Identifier("messages"),
            sql.SQL(", ").join(insert_columns),
            placeholders
        )

        conn = None
        cur = None

        try:

            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute(
                query,
                insert_values
            )

            row = cur.fetchone()

            conn.commit()

            return row[0] if row else None

        finally:

            if cur:
                cur.close()

            release_db_connection(conn)

    except Exception as e:

        log(
            f"SAVE_MESSAGE ERROR: {e!r}"
        )

        return None


# ============================================================
# SYSTEM SEED
# ============================================================

SEED_VERSION = "5.1-v1"


def seed_system_facts(chat_id):

    if not chat_id:
        return

    try:

        existing = db_execute(
            """
            SELECT id
            FROM system_seed_meta
            WHERE chat_id = %s
              AND seed_version = %s
            LIMIT 1
            """,
            (
                chat_id,
                SEED_VERSION
            ),
            fetchone=True
        )

        if existing:

            log(
                f"SEED ALREADY COMPLETE chat={chat_id}"
            )

            return

        log(
            f"SEED START chat={chat_id}"
        )

        success = 0

        for fact_key, content in SYSTEM_FACTS:

            result = create_fact(
                chat_id=chat_id,
                fact_key=fact_key,
                content=content,
                source="system",
                category="company/project",
                status="CONFIRMED"
            )

            if result:
                success += 1

        db_execute(
            """
            INSERT INTO system_seed_meta
            (chat_id, seed_version)
            VALUES (%s, %s)
            ON CONFLICT (chat_id, seed_version)
            DO NOTHING
            """,
            (
                chat_id,
                SEED_VERSION
            ),
            commit=True
        )

        log(
            f"SEED COMPLETE chat={chat_id} facts={success}/{len(SYSTEM_FACTS)}"
        )

    except Exception as e:

        log(
            f"SEED ERROR chat={chat_id}: {e!r}"
        )

        traceback.print_exc()


# ============================================================
# MEMORY CAPTURE
# ============================================================

MEMORY_PHRASES = [
    "დაიმახსოვრე",
    "შეინახე მეხსიერებაში",
    "დაიმახსოვრე ეს",
    "remember this",
    "remember that",
    "save this",
    "save this to memory",
    "memorize this"
]


def is_memory_request(text):

    lowered = clean_text(text).lower()

    return any(
        phrase in lowered
        for phrase in MEMORY_PHRASES
    )


def extract_memory_text(text):

    original = clean_text(text)

    lowered = original.lower()

    for phrase in MEMORY_PHRASES:

        index = lowered.find(
            phrase.lower()
        )

        if index >= 0:

            result = original[
                index + len(phrase):
            ].strip()

            result = re.sub(
                r"^[\s:,\-–—]+",
                "",
                result
            )

            return result

    return ""


# ============================================================
# DIRECT FACT CAPTURE
# ============================================================

def capture_direct_fact(chat_id, text):

    text = clean_text(text)

    patterns = [

        (
            r"(?:ჩემი კომპანია არის|my company is)\s+(.+)",
            "company_name"
        ),

        (
            r"(?:კომპანიის სახელი არის)\s+(.+)",
            "company_name"
        ),

        (
            r"(?:კომპანიის ID არის|company id is)\s+(.+)",
            "company_id"
        ),

        (
            r"(?:ეს არის ახალი პროექტი|new project is)\s+(.+)",
            "new_project"
        )
    ]

    for pattern, key in patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:

            value = clean_text(
                match.group(1)
            )

            if value:

                return create_fact(
                    chat_id,
                    key,
                    value,
                    source="user",
                    category="user"
                )

    return None


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_url(method):

    return (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/"
        + method
    )


def telegram_request(
    method,
    payload=None,
    timeout=40
):

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    response = requests.post(
        telegram_url(method),
        json=payload or {},
        timeout=timeout
    )

    if response.status_code >= 400:

        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    data = response.json()

    if not data.get("ok"):

        raise RuntimeError(
            f"Telegram API error: {data}"
        )

    return data


def clear_telegram_webhook():

    try:

        telegram_request(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            },
            timeout=20
        )

        log(
            "Telegram webhook cleared"
        )

    except Exception as e:

        log(
            f"Telegram webhook warning: {e!r}"
        )


def send_message(chat_id, text):

    text = clean_text(text)

    if not text:
        return False

    # Telegram max message length is around 4096 chars.
    chunks = [
        text[i:i + 3900]
        for i in range(
            0,
            len(text),
            3900
        )
    ]

    try:

        for chunk in chunks:

            telegram_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk
                },
                timeout=30
            )

        return True

    except Exception as e:

        log(
            f"SEND_MESSAGE ERROR chat={chat_id}: {e!r}"
        )

        return False


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def format_facts(chat_id):

    facts = get_facts(
        chat_id,
        limit=100
    )

    if not facts:

        return (
            "GENIOSA-ს მეხსიერებაში ამ ჩატისთვის "
            "დადასტურებული ფაქტები ჯერ არ მოიძებნა."
        )

    lines = [
        "📌 GENIOSA — დადასტურებული ფაქტები",
        ""
    ]

    for index, fact in enumerate(
        facts,
        start=1
    ):

        content = clean_text(
            fact.get("content")
        )

        if not content:
            continue

        lines.append(
            f"{index}. {content}"
        )

    return "\n".join(lines)


def format_memories(chat_id):

    memories = get_memories(
        chat_id,
        limit=50
    )

    if not memories:

        return (
            "GENIOSA-ს მეხსიერებაში "
            "ცალკე შენახული ჩანაწერები არ არის."
        )

    lines = [
        "🧠 GENIOSA — მეხსიერება",
        ""
    ]

    for index, memory in enumerate(
        memories,
        start=1
    ):

        content = clean_text(
            memory.get("content")
        )

        if content:

            lines.append(
                f"{index}. {content}"
            )

    return "\n".join(lines)


def format_projects(chat_id):

    try:

        rows = db_execute(
            """
            SELECT
                name,
                description,
                status
            FROM projects
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT 30
            """,
            (chat_id,),
            fetch=True
        )

    except Exception as e:

        log(
            f"PROJECTS ERROR: {e!r}"
        )

        rows = []

    if not rows:

        return (
            "ამ ჩატისთვის ცალკე პროექტები "
            "ჯერ არ არის შენახული."
        )

    lines = [
        "🏗 GENIOSA — პროექტები",
        ""
    ]

    for row in rows:

        name = clean_text(
            row.get("name")
        )

        description = clean_text(
            row.get("description")
        )

        status = clean_text(
            row.get("status")
        )

        lines.append(
            f"• {name}"
        )

        if description:
            lines.append(
                f"  {description}"
            )

        if status:
            lines.append(
                f"  სტატუსი: {status}"
            )

    return "\n".join(lines)


def format_tasks(chat_id):

    try:

        rows = db_execute(
            """
            SELECT
                title,
                description,
                status,
                due_date
            FROM tasks
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT 30
            """,
            (chat_id,),
            fetch=True
        )

    except Exception as e:

        log(
            f"TASKS ERROR: {e!r}"
        )

        rows = []

    if not rows:

        return (
            "ამ ჩატისთვის შენახული დავალებები არ არის."
        )

    lines = [
        "📋 GENIOSA — დავალებები",
        ""
    ]

    for row in rows:

        title = clean_text(
            row.get("title")
        )

        description = clean_text(
            row.get("description")
        )

        status = clean_text(
            row.get("status")
        )

        lines.append(
            f"• {title}"
        )

        if description:
            lines.append(
                f"  {description}"
            )

        if status:
            lines.append(
                f"  სტატუსი: {status}"
            )

    return "\n".join(lines)


def health_text():

    return (
        "🟢 GENIOSA 5.1\n\n"
        f"Version: {APP_VERSION}\n"
        f"Telegram: {'OK' if TELEGRAM_BOT_TOKEN else 'MISSING'}\n"
        f"Gemini: {'OK' if GEMINI_API_KEY else 'MISSING'}\n"
        f"Database: {'OK' if DATABASE_URL else 'MISSING'}\n"
        f"DB Pool: {'READY' if db_pool else 'NOT READY'}\n"
        f"Polling: {'RUNNING' if polling_thread and polling_thread.is_alive() else 'STOPPED'}"
    )


def handle_command(
    chat_id,
    text
):

    command = text.strip().split()[0].lower()

    if command.startswith("/start"):

        return (
            "გამარჯობა. მე ვარ GENIOSA 5.1 — "
            "შენი პირადი ბიზნეს-მრჩეველი და ასისტენტი.\n\n"
            "ჩემი ძირითადი ფუნქციებია:\n"
            "• ბიზნესისა და პროექტების ანალიზი\n"
            "• ფინანსური მოდელები\n"
            "• ინვესტორების ძიებისა და შეფასების მხარდაჭერა\n"
            "• პროექტების, ფაქტებისა და მეხსიერების შენახვა\n"
            "• კვლევა და გადაწყვეტილებების მხარდაჭერა\n\n"
            "გამოიყენე /help დახმარებისთვის."
        )

    if command.startswith("/help"):

        return (
            "GENIOSA 5.1 ბრძანებები:\n\n"
            "/health — სისტემის მდგომარეობა\n"
            "/facts — შენახული დადასტურებული ფაქტები\n"
            "/memory — შენახული მეხსიერება\n"
            "/projects — პროექტები\n"
            "/tasks — დავალებები\n"
            "/summary — მოკლე შეჯამება\n\n"
            "მეხსიერებაში ჩანაწერის შესანახად შეგიძლია დაწერო:\n"
            "„დაიმახსოვრე, რომ ...“"
        )

    if command.startswith("/health"):
        return health_text()

    if command.startswith("/facts"):
        return format_facts(chat_id)

    if command.startswith("/memory"):
        return format_memories(chat_id)

    if command.startswith("/projects"):
        return format_projects(chat_id)

    if command.startswith("/tasks"):
        return format_tasks(chat_id)

    if command.startswith("/summary"):

        facts = get_facts(
            chat_id,
            limit=20
        )

        memories = get_memories(
            chat_id,
            limit=10
        )

        messages = get_recent_messages(
            chat_id,
            limit=5
        )

        return (
            "📊 GENIOSA — მდგომარეობის შეჯამება\n\n"
            f"დადასტურებული ფაქტები: {len(facts)}\n"
            f"მეხსიერება: {len(memories)}\n"
            f"ბოლო შეტყობინებები: {len(messages)}"
        )

    return None


# ============================================================
# GEMINI
# ============================================================

def build_system_instruction():

    return """
You are GENIOSA, a personal business advisor and project-development assistant.

Your job is to help the user with:
- business strategy
- construction and development projects
- financial and economic analysis
- investor relations
- project feasibility
- market research
- planning
- decision support
- project management
- document and data analysis

CORE RULES:

1. Never invent facts.
2. Clearly distinguish:
   - confirmed facts
   - assumptions
   - estimates
   - scenarios
   - unknown information
3. If information is missing, say that it is missing.
4. Never present an estimate as a confirmed fact.
5. For legal, tax, regulatory or financial matters, explain when professional verification is required.
6. Do not fabricate sources, companies, investors, prices, laws or statistics.
7. Use the user's stored facts when relevant.
8. Do not overwrite confirmed facts merely because a new assumption appears.
9. Be practical and decision-oriented.
10. When useful, give numbers, formulas, scenarios and risks.
11. If a calculation depends on missing data, state exactly which data is needed.
12. The user expects continuity across conversations.
13. Respond in the language used by the user unless another language is requested.
14. Do not mention internal database implementation unless the user asks about it.
15. The user's company is SAMTISI CONSTRUCTION LLC.
"""


def build_gemini_prompt(
    chat_id,
    user_message
):

    facts = get_facts(
        chat_id,
        limit=60
    )

    memories = get_memories(
        chat_id,
        limit=25
    )

    recent = get_recent_messages(
        chat_id,
        limit=12
    )

    fact_lines = []

    for fact in facts:

        content = clean_text(
            fact.get("content")
        )

        if content:
            fact_lines.append(
                f"- {content}"
            )

    memory_lines = []

    for memory in memories:

        content = clean_text(
            memory.get("content")
        )

        if content:
            memory_lines.append(
                f"- {content}"
            )

    history_lines = []

    for item in recent:

        role = clean_text(
            item.get("role")
        )

        content = clean_text(
            item.get("content")
        )

        if content:

            history_lines.append(
                f"{role}: {content}"
            )

    prompt = (
        build_system_instruction()
        + "\n\n"
        + "CONFIRMED FACTS:\n"
        + (
            "\n".join(fact_lines)
            if fact_lines
            else "No stored confirmed facts."
        )
        + "\n\n"
        + "STORED MEMORIES:\n"
        + (
            "\n".join(memory_lines)
            if memory_lines
            else "No stored memories."
        )
        + "\n\n"
        + "RECENT CONVERSATION:\n"
        + (
            "\n".join(history_lines)
            if history_lines
            else "No recent conversation."
        )
        + "\n\n"
        + "CURRENT USER MESSAGE:\n"
        + user_message
        + "\n\n"
        + "Answer the user directly and use the stored information when relevant."
    )

    return prompt


def gemini_generate(
    chat_id,
    user_message
):

    if not GEMINI_API_KEY:

        return (
            "Gemini API key არ არის დაყენებული. "
            "შეამოწმე GEMINI_API_KEY Render-ის Environment-ში."
        )

    prompt = build_gemini_prompt(
        chat_id,
        user_message
    )

    # First use configured model, then safe fallbacks.
    models = []

    for model in [
        GEMINI_MODEL,
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash"
    ]:

        model = clean_text(model)

        if model and model not in models:
            models.append(model)

    last_error = None

    for model in models:

        url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/"
            + model
            + ":generateContent"
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
                "maxOutputTokens": 4096
            }
        }

        try:

            response = requests.post(
                url,
                params={
                    "key": GEMINI_API_KEY
                },
                json=payload,
                timeout=90
            )

            if response.status_code == 200:

                data = response.json()

                candidates = data.get(
                    "candidates",
                    []
                )

                if not candidates:

                    raise RuntimeError(
                        "Gemini returned no candidates"
                    )

                parts = (
                    candidates[0]
                    .get("content", {})
                    .get("parts", [])
                )

                answer_parts = []

                for part in parts:

                    text = part.get("text")

                    if text:
                        answer_parts.append(
                            text
                        )

                answer = "\n".join(
                    answer_parts
                ).strip()

                if answer:

                    if model != GEMINI_MODEL:

                        log(
                            f"GEMINI FALLBACK MODEL USED: {model}"
                        )

                    return answer

                raise RuntimeError(
                    "Gemini returned empty answer"
                )

            body = response.text[:1200]

            last_error = (
                f"HTTP {response.status_code}: {body}"
            )

            log(
                f"GEMINI MODEL {model} FAILED: {last_error}"
            )

            # Try next model.
            continue

        except Exception as e:

            last_error = repr(e)

            log(
                f"GEMINI MODEL {model} ERROR: {e!r}"
            )

            continue

    return (
        "ამ ეტაპზე Gemini-სგან პასუხის მიღება ვერ მოხერხდა.\n\n"
        f"ტექნიკური მიზეზი: {last_error}"
    )


# ============================================================
# UPDATE PROCESSING
# ============================================================

def process_update(update):

    if not isinstance(update, dict):
        return

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat") or {}

    chat_id = chat.get("id")

    if not chat_id:
        return

    user = message.get("from") or {}

    user_text = message.get("text")

    if not user_text:
        return

    user_text = clean_text(
        user_text
    )

    if not user_text:
        return

    # --------------------------------------------------------
    # Always make sure system facts exist.
    # --------------------------------------------------------

    seed_system_facts(
        chat_id
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    if user_text.startswith("/"):

        save_message(
            chat_id,
            "user",
            user_text
        )

        result = handle_command(
            chat_id,
            user_text
        )

        if result:

            save_message(
                chat_id,
                "assistant",
                result
            )

            send_message(
                chat_id,
                result
            )

            return

    # --------------------------------------------------------
    # Normal message
    # --------------------------------------------------------

    save_message(
        chat_id,
        "user",
        user_text
    )

    # --------------------------------------------------------
    # Explicit memory request
    # --------------------------------------------------------

    if is_memory_request(
        user_text
    ):

        memory_text = extract_memory_text(
            user_text
        )

        if memory_text:

            memory_id = create_memory(
                chat_id,
                memory_text,
                memory_type="explicit",
                importance=10,
                source="user"
            )

            if memory_id:

                reply = (
                    "🧠 დავიმახსოვრე.\n\n"
                    + memory_text
                )

                save_message(
                    chat_id,
                    "assistant",
                    reply
                )

                send_message(
                    chat_id,
                    reply
                )

                return

    # --------------------------------------------------------
    # Direct fact capture
    # --------------------------------------------------------

    try:

        capture_direct_fact(
            chat_id,
            user_text
        )

    except Exception as e:

        log(
            f"DIRECT FACT WARNING: {e!r}"
        )

    # --------------------------------------------------------
    # Gemini
    # --------------------------------------------------------

    answer = gemini_generate(
        chat_id,
        user_text
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
# TELEGRAM POLLING
# ============================================================

def acquire_polling_lock():

    global polling_lock_connection

    if not DATABASE_URL:
        return True

    try:

        conn = get_db_connection()

        cur = conn.cursor()

        cur.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (POLLING_LOCK_KEY,)
        )

        row = cur.fetchone()

        acquired = bool(
            row and row[0]
        )

        cur.close()

        if acquired:

            polling_lock_connection = conn

            log(
                "POSTGRES POLLING LOCK ACQUIRED"
            )

            return True

        release_db_connection(
            conn
        )

        log(
            "POSTGRES POLLING LOCK NOT AVAILABLE"
        )

        return False

    except Exception as e:

        log(
            f"POLLING LOCK ERROR: {e!r}"
        )

        if conn:
            release_db_connection(
                conn
            )

        return False


def release_polling_lock():

    global polling_lock_connection

    if polling_lock_connection is None:
        return

    try:

        cur = polling_lock_connection.cursor()

        cur.execute(
            "SELECT pg_advisory_unlock(%s)",
            (POLLING_LOCK_KEY,)
        )

        polling_lock_connection.commit()

        cur.close()

    except Exception as e:

        log(
            f"POLLING LOCK RELEASE ERROR: {e!r}"
        )

    finally:

        release_db_connection(
            polling_lock_connection
        )

        polling_lock_connection = None

        log(
            "POSTGRES POLLING LOCK RELEASED"
        )


def get_updates():

    global telegram_offset

    return telegram_request(
        "getUpdates",
        {
            "offset": telegram_offset,
            "timeout": TELEGRAM_TIMEOUT,
            "allowed_updates": [
                "message"
            ]
        },
        timeout=TELEGRAM_TIMEOUT + 10
    )


def polling_loop():

    global telegram_offset

    log(
        "Telegram polling thread started"
    )

    # --------------------------------------------------------
    # Only one process may poll.
    # --------------------------------------------------------

    while not polling_stop_event.is_set():

        if not acquire_polling_lock():

            time.sleep(
                TELEGRAM_RETRY_DELAY
            )

            continue

        try:

            clear_telegram_webhook()

            while not polling_stop_event.is_set():

                try:

                    data = get_updates()

                    updates = data.get(
                        "result",
                        []
                    )

                    for update in updates:

                        update_id = update.get(
                            "update_id"
                        )

                        if update_id is not None:

                            telegram_offset = (
                                update_id + 1
                            )

                        try:

                            process_update(
                                update
                            )

                        except Exception as e:

                            log(
                                f"UPDATE PROCESS ERROR: {e!r}"
                            )

                            traceback.print_exc()

                except RuntimeError as e:

                    error_text = str(e)

                    # Telegram 409 means another process
                    # currently owns getUpdates.
                    if "409" in error_text or \
                       "Conflict" in error_text:

                        log(
                            "TELEGRAM 409 CONFLICT — "
                            "releasing polling lock and retrying"
                        )

                        break

                    log(
                        f"POLLING REQUEST ERROR: {e!r}"
                    )

                    time.sleep(
                        TELEGRAM_RETRY_DELAY
                    )

                except requests.RequestException as e:

                    log(
                        f"TELEGRAM NETWORK ERROR: {e!r}"
                    )

                    time.sleep(
                        TELEGRAM_RETRY_DELAY
                    )

                except Exception as e:

                    log(
                        f"POLLING LOOP ERROR: {e!r}"
                    )

                    traceback.print_exc()

                    time.sleep(
                        TELEGRAM_RETRY_DELAY
                    )

        finally:

            release_polling_lock()

            time.sleep(
                TELEGRAM_RETRY_DELAY
            )


def start_polling_thread():

    global polling_thread

    if polling_thread is not None:
        return

    polling_stop_event.clear()

    polling_thread = threading.Thread(
        target=polling_loop,
        name="geniosa-telegram-polling",
        daemon=True
    )

    polling_thread.start()


# ============================================================
# KNOWN CHAT IDS
# ============================================================

def get_known_chat_ids():

    try:

        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM messages
            WHERE chat_id IS NOT NULL
            ORDER BY chat_id
            """,
            fetch=True
        )

        return [
            row["chat_id"]
            for row in (rows or [])
            if row.get("chat_id") is not None
        ]

    except Exception as e:

        log(
            f"KNOWN CHAT IDS ERROR: {e!r}"
        )

        return []


# ============================================================
# STARTUP
# ============================================================

def startup_worker():

    global startup_completed

    log(
        f"GENIOSA {APP_VERSION} STARTUP"
    )

    log(
        "CONFIG: "
        + str({
            "telegram": bool(TELEGRAM_BOT_TOKEN),
            "gemini": bool(GEMINI_API_KEY),
            "database": bool(DATABASE_URL),
            "model": GEMINI_MODEL
        })
    )

    try:

        # ----------------------------------------------------
        # Database
        # ----------------------------------------------------

        if DATABASE_URL:

            init_db_pool()

            migrate_database()

            log(
                "DATABASE READY"
            )

            # ------------------------------------------------
            # Existing chats
            # ------------------------------------------------

            known_chat_ids = get_known_chat_ids()

            log(
                f"KNOWN CHAT IDS: {known_chat_ids}"
            )

            # ------------------------------------------------
            # Seed existing chats.
            # ------------------------------------------------

            for chat_id in known_chat_ids:

                try:

                    seed_system_facts(
                        chat_id
                    )

                except Exception as e:

                    log(
                        f"SEED EXISTING CHAT ERROR "
                        f"{chat_id}: {e!r}"
                    )

        else:

            log(
                "DATABASE DISABLED — DATABASE_URL missing"
            )

        # ----------------------------------------------------
        # Telegram
        # ----------------------------------------------------

        if TELEGRAM_BOT_TOKEN:

            clear_telegram_webhook()

            start_polling_thread()

        else:

            log(
                "TELEGRAM DISABLED — token missing"
            )

        startup_completed = True

        log(
            f"GENIOSA {APP_VERSION} STARTUP COMPLETE"
        )

    except Exception as e:

        log(
            f"STARTUP ERROR: {e!r}"
        )

        traceback.print_exc()

        # IMPORTANT:
        # Do not silently claim startup completed.
        startup_completed = False


# ============================================================
# FASTAPI EVENTS
# ============================================================

@app.on_event("startup")
def on_startup():

    thread = threading.Thread(
        target=startup_worker,
        name="geniosa-startup",
        daemon=True
    )

    thread.start()


@app.on_event("shutdown")
def on_shutdown():

    log(
        "GENIOSA SHUTDOWN"
    )

    polling_stop_event.set()

    release_polling_lock()

    global db_pool

    if db_pool is not None:

        try:

            db_pool.closeall()

        except Exception as e:

            log(
                f"DB POOL CLOSE ERROR: {e!r}"
            )

        db_pool = None


# ============================================================
# HTTP ROUTES
# ============================================================

@app.get("/")
def root():

    return {
        "service": "GENIOSA",
        "version": APP_VERSION,
        "status": "online",
        "startup_completed": startup_completed
    }


@app.get("/health")
def health():

    db_status = False

    if db_pool is not None:

        try:

            db_execute(
                "SELECT 1",
                fetchone=True
            )

            db_status = True

        except Exception:

            db_status = False

    return {
        "service": "GENIOSA",
        "version": APP_VERSION,
        "startup_completed": startup_completed,
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "database_configured": bool(
            DATABASE_URL
        ),
        "database_connected": db_status,
        "polling_running": bool(
            polling_thread
            and polling_thread.is_alive()
        )
    }


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False
    )
