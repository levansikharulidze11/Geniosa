# ============================================================
# GENIOSA 5.2
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + FastAPI
#
# IMPORTANT:
# - Existing PostgreSQL data is preserved.
# - NO DROP TABLE.
# - NO TRUNCATE.
# - Legacy database schemas are supported.
# - facts table does NOT require an id column.
# - Automatic migrations are performed safely.
# - Telegram polling uses PostgreSQL advisory lock.
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

APP_VERSION = "5.2"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

OWNER_ID_RAW = os.getenv("GENIOSA_OWNER_ID", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

PORT = int(os.getenv("PORT", "10000"))

POLL_TIMEOUT = 30
POLL_RETRY_DELAY = 5

POLLING_LOCK_KEY = 735052

app = FastAPI(title="GENIOSA", version=APP_VERSION)

db_pool = None
polling_thread = None
polling_stop = threading.Event()
polling_running = False
startup_complete = False


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).isoformat()
    print(f"[GENIOSA {now}] {message}", flush=True)


# ============================================================
# BASIC HELPERS
# ============================================================

def get_owner_id():
    if not OWNER_ID_RAW:
        return None

    try:
        return int(OWNER_ID_RAW)
    except Exception:
        return None


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


# ============================================================
# DATABASE CONNECTION
# ============================================================

def init_db_pool():
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    db_pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=DATABASE_URL
    )

    log("DATABASE CONNECTION POOL READY")


def get_db():
    if db_pool is None:
        raise RuntimeError("Database pool is not initialized")

    return db_pool.getconn()


def release_db(conn):
    if db_pool is not None and conn is not None:
        db_pool.putconn(conn)


def db_execute(
    query,
    params=None,
    fetch=False,
    fetchone=False,
    commit=True
):
    conn = None
    cur = None

    try:
        conn = get_db()

        if fetch or fetchone:
            cur = conn.cursor(cursor_factory=RealDictCursor)
        else:
            cur = conn.cursor()

        cur.execute(query, params or ())

        result = None

        if fetch:
            result = cur.fetchall()

        elif fetchone:
            result = cur.fetchone()

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

        release_db(conn)


# ============================================================
# DATABASE SCHEMA HELPERS
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


def column_exists(table_name, column_name):
    row = db_execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
        ) AS exists
        """,
        (table_name, column_name),
        fetchone=True
    )

    return bool(row and row["exists"])


def get_columns(table_name):
    rows = db_execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
        fetch=True
    )

    return {
        row["column_name"]
        for row in (rows or [])
    }


def add_column(table_name, column_name, definition):
    if column_exists(table_name, column_name):
        return False

    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        query = sql.SQL(
            "ALTER TABLE {} ADD COLUMN {} {}"
        ).format(
            sql.Identifier(table_name),
            sql.Identifier(column_name),
            sql.SQL(definition)
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
        release_db(conn)


# ============================================================
# GENERIC TABLE CREATION
# ============================================================

def create_tables():
    # --------------------------------------------------------
    # messages
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            chat_id BIGINT,
            role TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # memories
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            chat_id BIGINT,
            memory TEXT,
            memory_type TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 5,
            source TEXT DEFAULT 'conversation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # facts
    #
    # IMPORTANT:
    # No dependency on id.
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            chat_id BIGINT,
            fact_key TEXT,
            value TEXT,
            fact TEXT,
            source TEXT DEFAULT 'system',
            status TEXT DEFAULT 'confirmed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            content TEXT,
            memory_type TEXT DEFAULT 'CONFIRMED',
            category TEXT
        )
        """
    )

    # --------------------------------------------------------
    # decisions
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            chat_id BIGINT,
            decision TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # tasks
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            chat_id BIGINT,
            task TEXT,
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            due_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # projects
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            chat_id BIGINT,
            name TEXT,
            description TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # research
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS research (
            chat_id BIGINT,
            topic TEXT,
            result TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # financial_models
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            chat_id BIGINT,
            project TEXT,
            data JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # bot_projects
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            chat_id BIGINT,
            name TEXT,
            description TEXT,
            status TEXT DEFAULT 'active',
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
            chat_id BIGINT,
            filename TEXT,
            filepath TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # memory_events
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            chat_id BIGINT,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # fact_history
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            chat_id BIGINT,
            fact_key TEXT,
            old_value TEXT,
            new_value TEXT,
            action TEXT,
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
            chat_id BIGINT PRIMARY KEY,
            seed_version TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


# ============================================================
# LEGACY MIGRATION
# ============================================================

def migrate_database():
    create_tables()

    # --------------------------------------------------------
    # messages
    # --------------------------------------------------------

    add_column(
        "messages",
        "chat_id",
        "BIGINT"
    )

    add_column(
        "messages",
        "role",
        "TEXT"
    )

    add_column(
        "messages",
        "message",
        "TEXT"
    )

    add_column(
        "messages",
        "text",
        "TEXT"
    )

    add_column(
        "messages",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # Copy legacy message/text values.
    if column_exists("messages", "message") and column_exists(
        "messages", "text"
    ):
        db_execute(
            """
            UPDATE messages
            SET message = text
            WHERE message IS NULL
              AND text IS NOT NULL
            """
        )

        db_execute(
            """
            UPDATE messages
            SET text = message
            WHERE text IS NULL
              AND message IS NOT NULL
            """
        )

    # --------------------------------------------------------
    # memories
    # --------------------------------------------------------

    add_column(
        "memories",
        "chat_id",
        "BIGINT"
    )

    add_column(
        "memories",
        "memory",
        "TEXT"
    )

    add_column(
        "memories",
        "content",
        "TEXT"
    )

    add_column(
        "memories",
        "memory_type",
        "TEXT DEFAULT 'general'"
    )

    add_column(
        "memories",
        "importance",
        "INTEGER DEFAULT 5"
    )

    add_column(
        "memories",
        "source",
        "TEXT DEFAULT 'conversation'"
    )

    add_column(
        "memories",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    add_column(
        "memories",
        "updated_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    if column_exists("memories", "memory") and column_exists(
        "memories", "content"
    ):
        db_execute(
            """
            UPDATE memories
            SET content = memory
            WHERE content IS NULL
              AND memory IS NOT NULL
            """
        )

        db_execute(
            """
            UPDATE memories
            SET memory = content
            WHERE memory IS NULL
              AND content IS NOT NULL
            """
        )

    # --------------------------------------------------------
    # facts
    # --------------------------------------------------------

    add_column(
        "facts",
        "chat_id",
        "BIGINT"
    )

    add_column(
        "facts",
        "fact_key",
        "TEXT"
    )

    add_column(
        "facts",
        "value",
        "TEXT"
    )

    add_column(
        "facts",
        "fact",
        "TEXT"
    )

    add_column(
        "facts",
        "content",
        "TEXT"
    )

    add_column(
        "facts",
        "source",
        "TEXT DEFAULT 'system'"
    )

    add_column(
        "facts",
        "status",
        "TEXT DEFAULT 'confirmed'"
    )

    add_column(
        "facts",
        "memory_type",
        "TEXT DEFAULT 'CONFIRMED'"
    )

    add_column(
        "facts",
        "category",
        "TEXT"
    )

    add_column(
        "facts",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    add_column(
        "facts",
        "updated_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # Copy legacy fact columns.
    # --------------------------------------------------------

    if column_exists("facts", "fact") and column_exists(
        "facts", "content"
    ):
        db_execute(
            """
            UPDATE facts
            SET content = fact
            WHERE content IS NULL
              AND fact IS NOT NULL
            """
        )

    if column_exists("facts", "content") and column_exists(
        "facts", "fact"
    ):
        db_execute(
            """
            UPDATE facts
            SET fact = content
            WHERE fact IS NULL
              AND content IS NOT NULL
            """
        )

    # --------------------------------------------------------
    # Legacy fact_key migration.
    #
    # IMPORTANT:
    # There is intentionally NO reference to facts.id.
    #
    # We generate deterministic keys using row_number()
    # only inside the migration query.
    # --------------------------------------------------------

    try:
        db_execute(
            """
            WITH numbered AS (
                SELECT
                    ctid,
                    'legacy_fact_' ||
                    ROW_NUMBER() OVER (
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
        )

        log("FACT_KEY LEGACY MIGRATION COMPLETE")

    except Exception as exc:
        log(
            f"FACT_KEY MIGRATION WARNING: {repr(exc)}"
        )

    # --------------------------------------------------------
    # Fill missing fact text from value.
    # --------------------------------------------------------

    if column_exists("facts", "value") and column_exists(
        "facts", "content"
    ):
        db_execute(
            """
            UPDATE facts
            SET content = value
            WHERE content IS NULL
              AND value IS NOT NULL
            """
        )

    if column_exists("facts", "content") and column_exists(
        "facts", "value"
    ):
        db_execute(
            """
            UPDATE facts
            SET value = content
            WHERE value IS NULL
              AND content IS NOT NULL
            """
        )

    if column_exists("facts", "status"):
        db_execute(
            """
            UPDATE facts
            SET status = 'confirmed'
            WHERE status IS NULL
            """
        )

    if column_exists("facts", "memory_type"):
        db_execute(
            """
            UPDATE facts
            SET memory_type = 'CONFIRMED'
            WHERE memory_type IS NULL
            """
        )

    # --------------------------------------------------------
    # indexes
    # --------------------------------------------------------

    safe_index(
        "idx_messages_chat_created",
        "messages",
        ["chat_id", "created_at"]
    )

    safe_index(
        "idx_memories_chat",
        "memories",
        ["chat_id"]
    )

    safe_index(
        "idx_facts_chat",
        "facts",
        ["chat_id"]
    )

    safe_index(
        "idx_facts_key",
        "facts",
        ["chat_id", "fact_key"]
    )

    safe_index(
        "idx_tasks_chat",
        "tasks",
        ["chat_id"]
    )

    safe_index(
        "idx_projects_chat",
        "projects",
        ["chat_id"]
    )

    log(
        "DATABASE INITIALIZED AND MIGRATED SUCCESSFULLY"
    )


def safe_index(index_name, table_name, columns):
    try:
        cols = sql.SQL(", ").join(
            sql.Identifier(x)
            for x in columns
        )

        query = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {} ON {} ({})"
        ).format(
            sql.Identifier(index_name),
            sql.Identifier(table_name),
            cols
        )

        db_execute(query.as_string(get_db()))

    except Exception:
        # Indexes are helpful but must never stop GENIOSA.
        pass


# ============================================================
# SYSTEM FACTS
# ============================================================

SYSTEM_FACTS = [

    {
        "key": "company_name",
        "category": "company",
        "content": "კომპანიის სახელი: SAMTISI CONSTRUCTION LLC",
    },

    {
        "key": "company_id",
        "category": "company",
        "content": "კომპანიის საიდენტიფიკაციო ნომერი: 406391202",
    },

    {
        "key": "company_type",
        "category": "company",
        "content": "SAMTISI CONSTRUCTION LLC არის Construction & Development Company.",
    },

    {
        "key": "company_location",
        "category": "company",
        "content": "კომპანიის მთავარი ოფისი მდებარეობს თბილისში, საქართველოში.",
    },

    {
        "key": "company_foundation",
        "category": "company",
        "content": "SAMTISI CONSTRUCTION LLC დაარსდა 2022 წლის 5 დეკემბერს.",
    },

    {
        "key": "company_activity",
        "category": "company",
        "content": "კომპანიის საქმიანობა მოიცავს მშენებლობასა და დეველოპმენტს, მათ შორის საცხოვრებელ, კომერციულ და ინფრასტრუქტურულ პროექტებს.",
    },

    {
        "key": "company_goal",
        "category": "company",
        "content": "SAMTISI CONSTRUCTION-ის მიზანია საქართველოში წამყვან სამშენებლო და დეველოპერულ კომპანიად ჩამოყალიბება.",
    },

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    {
        "key": "nikkea12_location",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტი მდებარეობს ქუთაისში, ნიკეას ქუჩა 12-ში.",
    },

    {
        "key": "nikkea12_land",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტის მიწის ფართობია 3,070 მ².",
    },

    {
        "key": "nikkea12_saleable",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტის საერთო გასაყიდი ფართობია 10,854 მ².",
    },

    {
        "key": "nikkea12_rooms",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტში სასტუმროს ტიპის აპარტამენტების/ოთახების ფართობია 7,212 მ².",
    },

    {
        "key": "nikkea12_monolith",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტის მონოლითის მოცულობა/ფართობი გათვლებში არის 21,500 მ².",
    },

    {
        "key": "nikkea12_architecture",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტის არქიტექტურული სამუშაოების ფართობი გათვლებში არის 16,000 მ².",
    },

    {
        "key": "nikkea12_commercial_1",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტში პირველი სართულის კომერციული ფართობია 836 მ².",
    },

    {
        "key": "nikkea12_commercial_2",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12 პროექტში მეორე სართულის კომერციული ფართობია 1,006 მ².",
    },

    {
        "key": "nikkea12_concept",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის კონცეფცია მოიცავს სასტუმროს ტიპის აპარტამენტებს, კაზინოს, სავაჭრო ცენტრს, რესტორანს და ზედა სართულზე lounge bar-ს.",
    },

    {
        "key": "nikkea12_landowner",
        "category": "NIKKEA 12",
        "content": "მიწის მესაკუთრის მოთხოვნაა 1,800 მ² აპარტამენტები, 25 საპარკინგე ადგილი და 300,000 აშშ დოლარი ნაღდი თანხა.",
    },

    {
        "key": "nikkea12_monolith_cost",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის გათვლებში მონოლითის სამშენებლო ღირებულება არის ზუსტად 170 აშშ დოლარი/მ².",
    },

    {
        "key": "nikkea12_fitout",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის დარჩენილი ფართების fit-out-ის სამუშაოებისთვის გამოყენებული მაქსიმალური სამუშაო დაშვება არის 450 აშშ დოლარი/მ².",
    },

    {
        "key": "nikkea12_apartment_price",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის აპარტამენტების საწყის საინვესტიციო გათვლებში გამოყენებულია 1,500–1,800 აშშ დოლარი/მ².",
    },

    {
        "key": "nikkea12_commercial_price",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის კომერციული ფართების საწყის გათვლებში გამოყენებულია 2,500 აშშ დოლარი/მ².",
    },

    {
        "key": "nikkea12_jv",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის JV მოდელში ინვესტორის წილი არის 80%, ხოლო SAMTISI-ის ოპერატორის წილი 20%.",
    },

    {
        "key": "nikkea12_investor",
        "category": "NIKKEA 12",
        "content": "ინვესტორის მოთხოვნილი მონაწილეობა განისაზღვრა პროექტის საერთო ღირებულების 35%-ით და დამატებით 300,000 აშშ დოლარით.",
    },

    {
        "key": "nikkea12_bank",
        "category": "NIKKEA 12",
        "content": "დაფინანსების დარჩენილი ნაწილისთვის გათვალისწინებულია საბანკო დაფინანსება, რომლის საპროცენტო განაკვეთის სამუშაო დაშვებაა მაქსიმუმ დაახლოებით 11.5–13.5%.",
    },

    {
        "key": "nikkea12_casino",
        "category": "NIKKEA 12",
        "content": "NIKKEA 12-ის 1,006 მ² კაზინოს ფართობი არ უნდა გაიყიდოს პროექტის მიმდინარე სტრატეგიის მიხედვით.",
    },

    {
        "key": "nikkea12_casino_value",
        "category": "NIKKEA 12",
        "content": "კაზინოს 1,006 მ² ფართობის საინვესტიციო შეფასებისთვის გამოყენებული მინიმალური სამიზნე ღირებულებაა 2,000 აშშ დოლარი/მ².",
    },

    {
        "key": "nikkea12_casino_rent",
        "category": "NIKKEA 12",
        "content": "კაზინოს გაქირავების შესაძლებლობის სამუშაო დაშვებად განხილულია მინიმუმ 50 აშშ დოლარი/მ² თვეში.",
    },

    # --------------------------------------------------------
    # SAMGORI
    # --------------------------------------------------------

    {
        "key": "samgori_location",
        "category": "Samgori",
        "content": "Samgori პროექტი მდებარეობს თბილისში, სამგორის რაიონში, გიორგი ნადერიშვილის ქუჩაზე.",
    },

    {
        "key": "samgori_land",
        "category": "Samgori",
        "content": "Samgori პროექტის მიწის ფართობია 7,390 მ².",
    },

    {
        "key": "samgori_build_area",
        "category": "Samgori",
        "content": "Samgori პროექტის საერთო სამშენებლო ფართობია 46,131 მ².",
    },

    {
        "key": "samgori_volume",
        "category": "Samgori",
        "content": "Samgori პროექტის სამშენებლო მოცულობაა 41,874 მ³.",
    },

    {
        "key": "samgori_saleable",
        "category": "Samgori",
        "content": "Samgori პროექტის გასაყიდი ფართობი, პარკინგის გამოკლებით, არის 30,540 მ².",
    },

    {
        "key": "samgori_parking",
        "category": "Samgori",
        "content": "Samgori პროექტის პარკინგის ფართობია 6,516 მ².",
    },

    {
        "key": "samgori_land_cost",
        "category": "Samgori",
        "content": "Samgori პროექტში მიწის მესაკუთრის მოთხოვნაა დაახლოებით 7,750,000 აშშ დოლარი.",
    },

    {
        "key": "samgori_construction_cost",
        "category": "Samgori",
        "content": "Samgori პროექტისთვის გათვალისწინებული მშენებლობის საჭიროება არის დაახლოებით 15,000,000 აშშ დოლარი.",
    },

    {
        "key": "samgori_total",
        "category": "Samgori",
        "content": "Samgori პროექტის საერთო კაპიტალის სამუშაო გათვლა დაახლოებით 23,500,000 აშშ დოლარია.",
    },

    {
        "key": "samgori_profit",
        "category": "Samgori",
        "content": "Samgori პროექტის სამუშაო გათვლებში მოსალოდნელი წმინდა მოგება დაახლოებით 10,000,000 აშშ დოლარია.",
    },

    # --------------------------------------------------------
    # GOLDEN LAKE / OQRI
    # --------------------------------------------------------

    {
        "key": "golden_lake_land",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri პროექტის ძირითადი მიწის ფართობია დაახლოებით 46 ჰექტარი.",
    },

    {
        "key": "golden_lake_total_area",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri კონცეფციაში ტბისა და მიმდებარე ტერიტორიის საერთო მასშტაბი დაახლოებით 70–80 ჰექტარია.",
    },

    {
        "key": "golden_lake_land_cost",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri პროექტში მიწის ღირებულების სამუშაო დიაპაზონია დაახლოებით 35–40 მილიონი აშშ დოლარი.",
    },

    {
        "key": "golden_lake_construction",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri პროექტში დაგეგმილი სამშენებლო ფართობია დაახლოებით 250,000 მ².",
    },

    {
        "key": "golden_lake_hotel",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri კონცეფცია მოიცავს 5-ვარსკვლავიან 200-ნომრიან სასტუმროს, აკვაპარკს და კაზინოს.",
    },

    {
        "key": "golden_lake_four_star",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri კონცეფცია ასევე ითვალისწინებს 4-ვარსკვლავიან სასტუმროს დაახლოებით 100–120 ნომრით.",
    },

    {
        "key": "golden_lake_arena",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri კონცეფცია მოიცავს დაახლოებით 10,000 მაყურებელზე გათვლილ სპორტულ/ივენთ არენას.",
    },

    {
        "key": "golden_lake_apartments",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri კონცეფციაში ბინების გასაყიდი ფართობი დაახლოებით 150,000 მ² არის.",
    },

    {
        "key": "golden_lake_commercial",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri კონცეფციაში კომერციული ფართობი დაახლოებით 30,000 მ² არის.",
    },

    {
        "key": "golden_lake_apartment_price",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri-ის საწყის სამუშაო გათვლებში ბინების გაყიდვის ფასი არის დაახლოებით 2,500–3,000 აშშ დოლარი/მ².",
    },

    {
        "key": "golden_lake_commercial_price",
        "category": "Golden Lake",
        "content": "Golden Lake/Oqri-ის საწყის სამუშაო გათვლებში კომერციული ფართების ფასი არის დაახლოებით 3,500–5,000 აშშ დოლარი/მ².",
    },
]


# ============================================================
# FACT FUNCTIONS
# ============================================================

def create_fact(
    chat_id,
    fact_key,
    content,
    category="general",
    source="system",
    status="confirmed"
):
    """
    Legacy-compatible fact insert.

    Dynamically detects available columns.
    Does NOT assume an id column exists.
    """

    try:
        columns = get_columns("facts")

        data = {}

        if "chat_id" in columns:
            data["chat_id"] = chat_id

        if "fact_key" in columns:
            data["fact_key"] = fact_key

        if "value" in columns:
            data["value"] = content

        if "fact" in columns:
            data["fact"] = content

        if "content" in columns:
            data["content"] = content

        if "source" in columns:
            data["source"] = source

        if "status" in columns:
            data["status"] = status

        if "memory_type" in columns:
            data["memory_type"] = "CONFIRMED"

        if "category" in columns:
            data["category"] = category

        if not data:
            raise RuntimeError(
                "facts table has no usable columns"
            )

        col_names = list(data.keys())

        placeholders = ", ".join(
            ["%s"] * len(col_names)
        )

        query = sql.SQL(
            "INSERT INTO facts ({}) VALUES ({})"
        ).format(
            sql.SQL(", ").join(
                sql.Identifier(c)
                for c in col_names
            ),
            sql.SQL(placeholders)
        )

        db_execute(
            query.as_string(get_db()),
            tuple(data[c] for c in col_names)
        )

        return True

    except Exception as exc:
        log(
            f"CREATE_FACT ERROR key={fact_key}: {repr(exc)}"
        )
        return False


def fact_exists(chat_id, fact_key):
    columns = get_columns("facts")

    if "chat_id" not in columns:
        return False

    if "fact_key" not in columns:
        return False

    row = db_execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM facts
            WHERE chat_id = %s
              AND fact_key = %s
        ) AS exists
        """,
        (chat_id, fact_key),
        fetchone=True
    )

    return bool(row and row["exists"])


def seed_system_facts(chat_id):
    """
    Seeds system facts safely.

    IMPORTANT:
    - No SELECT id.
    - No UPDATE by id.
    - Uses chat_id + fact_key.
    """

    if not chat_id:
        return

    try:
        for item in SYSTEM_FACTS:

            key = item["key"]
            content = item["content"]
            category = item.get(
                "category",
                "general"
            )

            if fact_exists(chat_id, key):
                continue

            create_fact(
                chat_id=chat_id,
                fact_key=key,
                content=content,
                category=category,
                source="system",
                status="confirmed"
            )

        log(
            f"SEED COMPLETE chat={chat_id}"
        )

        db_execute(
            """
            INSERT INTO system_seed_meta
                (chat_id, seed_version, updated_at)
            VALUES
                (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (chat_id)
            DO UPDATE SET
                seed_version = EXCLUDED.seed_version,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                chat_id,
                "5.2-v1"
            )
        )

    except Exception as exc:
        log(
            f"SEED ERROR chat={chat_id}: {repr(exc)}"
        )
        traceback.print_exc()


# ============================================================
# MEMORY
# ============================================================

def create_memory(
    chat_id,
    content,
    memory_type="general",
    importance=5,
    source="conversation"
):
    try:
        db_execute(
            """
            INSERT INTO memories
                (
                    chat_id,
                    memory,
                    content,
                    memory_type,
                    importance,
                    source,
                    created_at,
                    updated_at
                )
            VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            """,
            (
                chat_id,
                content,
                content,
                memory_type,
                importance,
                source
            )
        )

        return True

    except Exception as exc:
        log(
            f"CREATE_MEMORY ERROR: {repr(exc)}"
        )
        return False


def get_memories(chat_id, limit=30):
    try:
        columns = get_columns("memories")

        if "content" in columns:
            text_column = "content"
        elif "memory" in columns:
            text_column = "memory"
        else:
            return []

        query = sql.SQL(
            """
            SELECT chat_id, {} AS content,
                   memory_type,
                   importance,
                   source,
                   created_at
            FROM memories
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """
        ).format(
            sql.Identifier(text_column)
        )

        return db_execute(
            query.as_string(get_db()),
            (chat_id, limit),
            fetch=True
        ) or []

    except Exception as exc:
        log(
            f"GET_MEMORIES ERROR: {repr(exc)}"
        )
        return []


# ============================================================
# FACT RETRIEVAL
# ============================================================

def get_facts(chat_id, limit=100):
    try:
        columns = get_columns("facts")

        if "content" in columns:
            content_column = "content"

        elif "fact" in columns:
            content_column = "fact"

        elif "value" in columns:
            content_column = "value"

        else:
            return []

        key_column = (
            "fact_key"
            if "fact_key" in columns
            else None
        )

        category_column = (
            "category"
            if "category" in columns
            else None
        )

        source_column = (
            "source"
            if "source" in columns
            else None
        )

        chat_column = (
            "chat_id"
            if "chat_id" in columns
            else None
        )

        if not chat_column:
            return []

        select_parts = [
            sql.SQL("{} AS content").format(
                sql.Identifier(content_column)
            )
        ]

        if key_column:
            select_parts.append(
                sql.SQL("{} AS fact_key").format(
                    sql.Identifier(key_column)
                )
            )

        else:
            select_parts.append(
                sql.SQL("NULL AS fact_key")
            )

        if category_column:
            select_parts.append(
                sql.SQL("{} AS category").format(
                    sql.Identifier(category_column)
                )
            )

        else:
            select_parts.append(
                sql.SQL("NULL AS category")
            )

        if source_column:
            select_parts.append(
                sql.SQL("{} AS source").format(
                    sql.Identifier(source_column)
                )
            )

        else:
            select_parts.append(
                sql.SQL("NULL AS source")
            )

        query = sql.SQL(
            """
            SELECT {}
            FROM facts
            WHERE {} = %s
            ORDER BY
                COALESCE(updated_at, created_at)
                DESC
            LIMIT %s
            """
        ).format(
            sql.SQL(", ").join(select_parts),
            sql.Identifier(chat_column)
        )

        return db_execute(
            query.as_string(get_db()),
            (chat_id, limit),
            fetch=True
        ) or []

    except Exception as exc:
        log(
            f"GET_FACTS ERROR: {repr(exc)}"
        )
        return []


# ============================================================
# CONVERSATION HISTORY
# ============================================================

def save_message(chat_id, role, message):
    message = clean_text(message)

    if not message:
        return False

    try:
        columns = get_columns("messages")

        data = {}

        if "chat_id" in columns:
            data["chat_id"] = chat_id

        if "role" in columns:
            data["role"] = role

        if "message" in columns:
            data["message"] = message

        if "text" in columns:
            data["text"] = message

        if not data:
            return False

        names = list(data.keys())

        query = sql.SQL(
            "INSERT INTO messages ({}) VALUES ({})"
        ).format(
            sql.SQL(", ").join(
                sql.Identifier(x)
                for x in names
            ),
            sql.SQL(", ").join(
                sql.Placeholder(x)
                for x in names
            )
        )

        db_execute(
            query.as_string(get_db()),
            data
        )

        return True

    except Exception as exc:
        log(
            f"SAVE_MESSAGE ERROR: {repr(exc)}"
        )
        return False


def get_history(chat_id, limit=20):
    try:
        columns = get_columns("messages")

        if "message" in columns:
            message_column = "message"
        elif "text" in columns:
            message_column = "text"
        else:
            return []

        query = sql.SQL(
            """
            SELECT role, {} AS message, created_at
            FROM messages
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """
        ).format(
            sql.Identifier(message_column)
        )

        rows = db_execute(
            query.as_string(get_db()),
            (chat_id, limit),
            fetch=True
        ) or []

        return list(reversed(rows))

    except Exception as exc:
        log(
            f"GET_HISTORY ERROR: {repr(exc)}"
        )
        return []


# ============================================================
# CONTEXT BUILDER
# ============================================================

def build_context(chat_id):
    facts = get_facts(
        chat_id,
        limit=100
    )

    memories = get_memories(
        chat_id,
        limit=30
    )

    history = get_history(
        chat_id,
        limit=20
    )

    fact_text = []

    for row in facts:
        content = clean_text(
            row.get("content")
        )

        if content:
            category = clean_text(
                row.get("category")
            )

            if category:
                fact_text.append(
                    f"[{category}] {content}"
                )
            else:
                fact_text.append(content)

    memory_text = []

    for row in memories:
        content = clean_text(
            row.get("content")
        )

        if content:
            memory_text.append(
                content
            )

    history_text = []

    for row in history:
        role = clean_text(
            row.get("role")
        )

        message = clean_text(
            row.get("message")
        )

        if message:
            history_text.append(
                f"{role}: {message}"
            )

    return {
        "facts": fact_text,
        "memories": memory_text,
        "history": history_text
    }


# ============================================================
# GENIOSA CONSTITUTION
# ============================================================

SYSTEM_PROMPT = """
შენ ხარ GENIOSA — მომხმარებლის პირადი ბიზნეს მრჩეველი,
ეკონომისტი, პროექტების ანალიტიკოსი, კვლევის ასისტენტი და
ბიზნეს-ოპერაციების დამხმარე.

მომხმარებელი არის GENIOSA-ს დამფუძნებელი/მფლობელი და
SAMTISI CONSTRUCTION LLC-ის ხელმძღვანელი.

მთავარი წესები:

1. არასოდეს მოიგონო ფაქტი.
2. დადასტურებული ფაქტი მკაფიოდ განასხვავე:
   - დადასტურებული ფაქტისგან
   - ვარაუდისგან
   - შეფასებისგან
   - პროგნოზისგან
   - ბაზრის შესამოწმებელი ინფორმაციისგან.
3. თუ ინფორმაცია არ იცი, პირდაპირ თქვი:
   „ეს ინფორმაცია ამ ეტაპზე დადასტურებული არ მაქვს.“
4. მომხმარებლის მიერ ადრე დადასტურებული ინფორმაცია
   გამოიყენე როგორც სამუშაო კონტექსტი.
5. პროექტის ფინანსურ ანალიზში ყოველთვის მიუთითე,
   რომელი ციფრი არის ფაქტი და რომელი არის დაშვება.
6. არ შეცვალო მომხმარებლის მიერ დადასტურებული მონაცემი
   თვითნებურად.
7. თუ ახალი ინფორმაცია ეწინააღმდეგება ძველ დადასტურებულ
   ფაქტს, მიუთითე წინააღმდეგობაზე და არ გადაწყვიტო
   თვითნებურად რომელი არის სწორი.
8. ბიზნეს, საინვესტიციო, ფინანსურ და სამართლებრივ საკითხებში
   იყავი ფრთხილი და მიუთითე საჭიროების შემთხვევაში
   პროფესიული შემოწმების აუცილებლობაზე.
9. პასუხები უნდა იყოს პრაქტიკული, სტრუქტურირებული და
   გადაწყვეტილების მიღებაში გამოსადეგი.
10. მომხმარებელს არ უთხრა, რომ რაღაც გახსოვს, თუ შესაბამისი
    ინფორმაცია რეალურად არ არის შენთვის მიწოდებულ კონტექსტში.

GENIOSA-ს ძირითადი მიმართულებებია:
- ბიზნეს სტრატეგია
- ინვესტორების მოძიება
- უძრავი ქონების დეველოპმენტი
- პროექტების ფინანსური მოდელები
- ROI / IRR / NPV / Cash Flow
- სამშენებლო ეკონომიკა
- ბაზრის კვლევა
- უცხოელი ინვესტორები
- პროექტების მართვა
- დოკუმენტების მომზადება
- რისკების შეფასება
- ბიზნეს კვლევა
- ტექნიკური და ოპერაციული დაგეგმვა.
"""


# ============================================================
# GEMINI
# ============================================================

def gemini_generate(prompt):
    if not GEMINI_API_KEY:
        return (
            "Gemini API Key არ არის კონფიგურირებული."
        )

    models = [
        GEMINI_MODEL,
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
    ]

    tried = set()

    for model in models:

        if not model or model in tried:
            continue

        tried.add(model)

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
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

            if response.status_code != 200:
                log(
                    f"GEMINI ERROR model={model} "
                    f"status={response.status_code} "
                    f"body={response.text[:500]}"
                )
                continue

            data = response.json()

            candidates = data.get(
                "candidates",
                []
            )

            if not candidates:
                continue

            parts = (
                candidates[0]
                .get("content", {})
                .get("parts", [])
            )

            text_parts = []

            for part in parts:
                value = part.get("text")

                if value:
                    text_parts.append(value)

            result = "\n".join(
                text_parts
            ).strip()

            if result:
                return result

        except Exception as exc:
            log(
                f"GEMINI REQUEST ERROR model={model}: "
                f"{repr(exc)}"
            )

    return (
        "ამ მომენტში Gemini-სგან პასუხის მიღება ვერ მოხერხდა. "
        "გთხოვ, ცოტა ხანში სცადო ხელახლა."
    )


def answer_user(chat_id, user_message):
    context = build_context(chat_id)

    facts = "\n".join(
        f"- {x}"
        for x in context["facts"]
    )

    memories = "\n".join(
        f"- {x}"
        for x in context["memories"]
    )

    history = "\n".join(
        context["history"]
    )

    prompt = f"""
{SYSTEM_PROMPT}

========================
CONFIRMED FACTS
========================

{facts if facts else "ამ ჩატში დადასტურებული ფაქტები ამ ეტაპზე არ მოიძებნა."}

========================
MEMORIES
========================

{memories if memories else "დამატებითი მეხსიერება არ მოიძებნა."}

========================
RECENT CONVERSATION
========================

{history if history else "წინა საუბარი არ მოიძებნა."}

========================
CURRENT USER MESSAGE
========================

{user_message}

========================
INSTRUCTION
========================

უპასუხე მომხმარებელს ქართულად, თუ მომხმარებელი სხვა ენას
არ იყენებს.

არ გამოიგონო მონაცემები.

თუ კითხვა ეხება კომპანიის ან პროექტის ფაქტებს,
გამოიყენე ზემოთ მოცემული CONFIRMED FACTS.

თუ მონაცემი არ არის კონტექსტში, თქვი რომ დადასტურებული
ინფორმაცია არ გაქვს.

ფინანსურ გამოთვლებში ციფრები მკაფიოდ აჩვენე.
"""

    return gemini_generate(
        prompt
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
    timeout=60
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

    if response.status_code != 200:
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


def clear_webhook():
    try:
        telegram_call(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            },
            timeout=30
        )

        log(
            "Telegram webhook cleared"
        )

    except Exception as exc:
        log(
            f"Telegram webhook error: {repr(exc)}"
        )


def send_message(
    chat_id,
    text,
    parse_mode=None
):
    text = clean_text(text)

    if not text:
        return False

    # Telegram limit protection.
    max_length = 4000

    chunks = [
        text[i:i + max_length]
        for i in range(
            0,
            len(text),
            max_length
        )
    ]

    for chunk in chunks:

        payload = {
            "chat_id": chat_id,
            "text": chunk
        }

        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            telegram_call(
                "sendMessage",
                payload,
                timeout=60
            )

        except Exception as exc:
            log(
                f"SEND MESSAGE ERROR chat={chat_id}: "
                f"{repr(exc)}"
            )
            return False

    return True


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def command_health(chat_id):
    send_message(
        chat_id,
        f"""🟢 GENIOSA {APP_VERSION}

Version: {APP_VERSION}
Telegram: {"OK" if TELEGRAM_BOT_TOKEN else "ERROR"}
Gemini: {"OK" if GEMINI_API_KEY else "ERROR"}
Database: {"OK" if db_pool else "ERROR"}
DB Pool: {"READY" if db_pool else "NOT READY"}
Polling: {"RUNNING" if polling_running else "STOPPED"}"""
    )


def command_facts(chat_id):
    facts = get_facts(
        chat_id,
        limit=100
    )

    if not facts:
        send_message(
            chat_id,
            "GENIOSA-ს მეხსიერებაში ამ ჩატისთვის "
            "დადასტურებული ფაქტები ჯერ არ მოიძებნა."
        )
        return

    lines = [
        "🧠 დადასტურებული ფაქტები:"
    ]

    for row in facts:

        content = clean_text(
            row.get("content")
        )

        if not content:
            continue

        category = clean_text(
            row.get("category")
        )

        if category:
            lines.append(
                f"\n• [{category}] {content}"
            )
        else:
            lines.append(
                f"\n• {content}"
            )

    send_message(
        chat_id,
        "\n".join(lines)
    )


def command_memory(chat_id):
    memories = get_memories(
        chat_id,
        limit=50
    )

    if not memories:
        send_message(
            chat_id,
            "GENIOSA-ს დამატებითი მეხსიერება ამ ჩატისთვის "
            "ჯერ არ მოიძებნა."
        )
        return

    lines = [
        "🧠 მეხსიერება:"
    ]

    for row in memories:
        content = clean_text(
            row.get("content")
        )

        if content:
            lines.append(
                f"\n• {content}"
            )

    send_message(
        chat_id,
        "\n".join(lines)
    )


def command_summary(chat_id):
    context = build_context(chat_id)

    facts = context["facts"]

    projects = [
        x
        for x in facts
        if any(
            keyword in x
            for keyword in [
                "NIKKEA",
                "Samgori",
                "Golden Lake",
                "SAMTISI"
            ]
        )
    ]

    text = (
        "📊 GENIOSA — მოკლე შეჯამება\n\n"
        f"დადასტურებული ფაქტები: {len(facts)}\n"
        f"ძირითადი პროექტებთან დაკავშირებული ჩანაწერები: "
        f"{len(projects)}"
    )

    send_message(
        chat_id,
        text
    )


def command_help(chat_id):
    send_message(
        chat_id,
        """🤖 GENIOSA

ძირითადი ბრძანებები:

/start — დაწყება
/help — დახმარება
/health — სისტემის სტატუსი
/facts — დადასტურებული ფაქტები
/memory — მეხსიერება
/summary — მოკლე შეჯამება

შეგიძლია უბრალოდ მომწერო კითხვა ჩვეულებრივად.

მაგალითად:
„რა გახსოვს NIKKEA 12-ზე?“
„გამიკეთე Samgori პროექტის ფინანსური ანალიზი“
„რა მონაცემები გვაქვს SAMTISI-ზე?“"""
    )


def command_start(chat_id):
    seed_system_facts(chat_id)

    send_message(
        chat_id,
        """გამარჯობა. მე ვარ GENIOSA — შენი პირადი ბიზნეს მრჩეველი.

ჩემი სამუშაოა დაგეხმარო:
• ბიზნესში
• ინვესტორების მოძიებაში
• დეველოპმენტ პროექტებში
• ფინანსურ ანალიზში
• პროექტების მართვაში
• კვლევასა და სტრატეგიაში.

ჩემი ძირითადი პრინციპია:
ფაქტი არ უნდა მოვიგონო."""
    )


# ============================================================
# TELEGRAM UPDATE HANDLER
# ============================================================

def handle_update(update):
    if not isinstance(update, dict):
        return

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    if chat_id is None:
        return

    text_value = message.get("text")

    if text_value is None:
        return

    user_message = clean_text(
        text_value
    )

    if not user_message:
        return

    log(
        f"USER MESSAGE chat={chat_id}: "
        f"{user_message[:500]}"
    )

    # Always make sure system facts exist.
    seed_system_facts(
        chat_id
    )

    save_message(
        chat_id,
        "user",
        user_message
    )

    command = user_message.split()[0].lower()

    if command == "/health":
        command_health(chat_id)
        return

    if command == "/facts":
        command_facts(chat_id)
        return

    if command == "/memory":
        command_memory(chat_id)
        return

    if command == "/summary":
        command_summary(chat_id)
        return

    if command == "/help":
        command_help(chat_id)
        return

    if command == "/start":
        command_start(chat_id)
        return

    # Normal AI conversation.
    answer = answer_user(
        chat_id,
        user_message
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

    log(
        f"ANSWER SENT chat={chat_id}"
    )


# ============================================================
# TELEGRAM POLLING LOCK
# ============================================================

def acquire_polling_lock():
    conn = None
    cur = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT pg_try_advisory_lock(%s)
            """,
            (POLLING_LOCK_KEY,)
        )

        row = cur.fetchone()

        acquired = bool(
            row and row[0]
        )

        if acquired:
            log(
                "POSTGRES POLLING LOCK ACQUIRED"
            )
        else:
            log(
                "POSTGRES POLLING LOCK NOT ACQUIRED"
            )

        return acquired, conn

    except Exception as exc:
        log(
            f"POLLING LOCK ERROR: {repr(exc)}"
        )

        if conn:
            release_db(conn)

        return False, None

    finally:
        if cur:
            cur.close()


def release_polling_lock(conn):
    if conn is None:
        return

    cur = None

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT pg_advisory_unlock(%s)
            """,
            (POLLING_LOCK_KEY,)
        )

        conn.commit()

        log(
            "POSTGRES POLLING LOCK RELEASED"
        )

    except Exception as exc:
        log(
            f"POLLING LOCK RELEASE ERROR: {repr(exc)}"
        )

    finally:
        if cur:
            cur.close()

        release_db(conn)


# ============================================================
# TELEGRAM POLLING LOOP
# ============================================================

def polling_loop():
    global polling_running

    lock_conn = None

    try:
        acquired, lock_conn = acquire_polling_lock()

        if not acquired:
            log(
                "ANOTHER GENIOSA INSTANCE OWNS TELEGRAM POLLING"
            )
            polling_running = False
            return

        clear_webhook()

        offset = None

        polling_running = True

        log(
            "Telegram polling thread started"
        )

        while not polling_stop.is_set():

            payload = {
                "timeout": POLL_TIMEOUT
            }

            if offset is not None:
                payload["offset"] = offset

            try:
                data = telegram_call(
                    "getUpdates",
                    payload,
                    timeout=POLL_TIMEOUT + 10
                )

                updates = data.get(
                    "result",
                    []
                )

                for update in updates:

                    update_id = update.get(
                        "update_id"
                    )

                    if update_id is not None:
                        offset = update_id + 1

                    try:
                        handle_update(
                            update
                        )

                    except Exception as exc:
                        log(
                            f"UPDATE HANDLER ERROR: "
                            f"{repr(exc)}"
                        )
                        traceback.print_exc()

            except Exception as exc:

                error_text = str(exc)

                if "409" in error_text:
                    log(
                        "TELEGRAM 409 CONFLICT — "
                        "another polling process may be active"
                    )

                    time.sleep(
                        POLL_RETRY_DELAY
                    )

                    continue

                log(
                    f"POLLING LOOP ERROR: "
                    f"{repr(exc)}"
                )

                time.sleep(
                    POLL_RETRY_DELAY
                )

    except Exception as exc:
        log(
            f"POLLING FATAL ERROR: {repr(exc)}"
        )
        traceback.print_exc()

    finally:
        polling_running = False

        if lock_conn is not None:
            release_polling_lock(
                lock_conn
            )


def start_polling():
    global polling_thread

    if not TELEGRAM_BOT_TOKEN:
        log(
            "TELEGRAM TOKEN MISSING — POLLING NOT STARTED"
        )
        return

    polling_stop.clear()

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
    ids = set()

    try:
        if table_exists("messages"):
            rows = db_execute(
                """
                SELECT DISTINCT chat_id
                FROM messages
                WHERE chat_id IS NOT NULL
                """,
                fetch=True
            ) or []

            for row in rows:
                value = row.get("chat_id")

                if value is not None:
                    ids.add(
                        int(value)
                    )

    except Exception as exc:
        log(
            f"KNOWN CHAT IDS ERROR: {repr(exc)}"
        )

    try:
        if table_exists("facts"):
            rows = db_execute(
                """
                SELECT DISTINCT chat_id
                FROM facts
                WHERE chat_id IS NOT NULL
                """,
                fetch=True
            ) or []

            for row in rows:
                value = row.get("chat_id")

                if value is not None:
                    ids.add(
                        int(value)
                    )

    except Exception as exc:
        log(
            f"KNOWN FACT CHAT IDS ERROR: {repr(exc)}"
        )

    owner_id = get_owner_id()

    if owner_id is not None:
        ids.add(
            owner_id
        )

    return sorted(ids)


# ============================================================
# FASTAPI
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
    return {
        "app": "GENIOSA",
        "version": APP_VERSION,
        "telegram": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini": bool(
            GEMINI_API_KEY
        ),
        "database": bool(
            DATABASE_URL
        ),
        "db_pool": (
            "READY"
            if db_pool
            else "NOT READY"
        ),
        "polling": (
            "RUNNING"
            if polling_running
            else "STOPPED"
        )
    }


# ============================================================
# STARTUP
# ============================================================

def startup_worker():
    global startup_complete

    log(
        f"GENIOSA {APP_VERSION} STARTUP"
    )

    log(
        "CONFIG: "
        + str(
            {
                "telegram": bool(
                    TELEGRAM_BOT_TOKEN
                ),
                "gemini": bool(
                    GEMINI_API_KEY
                ),
                "database": bool(
                    DATABASE_URL
                ),
                "model": GEMINI_MODEL
            }
        )
    )

    try:

        if DATABASE_URL:

            init_db_pool()

            migrate_database()

            log(
                "DATABASE READY"
            )

            known_ids = get_known_chat_ids()

            log(
                f"KNOWN CHAT IDS: {known_ids}"
            )

            # Seed only known chats.
            #
            # IMPORTANT:
            # Even if one seed fails, Telegram polling
            # must still start.
            for chat_id in known_ids:

                try:
                    seed_system_facts(
                        chat_id
                    )

                except Exception as exc:
                    log(
                        f"SEED STARTUP ERROR "
                        f"chat={chat_id}: "
                        f"{repr(exc)}"
                    )

        else:
            log(
                "DATABASE NOT CONFIGURED"
            )

        start_polling()

        startup_complete = True

        log(
            f"GENIOSA {APP_VERSION} STARTUP COMPLETE"
        )

    except Exception as exc:

        log(
            f"STARTUP ERROR: {repr(exc)}"
        )

        traceback.print_exc()

        # Do not crash the web server.
        #
        # If possible, Telegram can still be started
        # only when the required components are available.
        try:
            if TELEGRAM_BOT_TOKEN:
                start_polling()
        except Exception:
            pass


# ============================================================
# SHUTDOWN
# ============================================================

def shutdown_worker():
    global polling_running

    log(
        f"GENIOSA {APP_VERSION} SHUTDOWN"
    )

    polling_stop.set()

    polling_running = False

    global db_pool

    if db_pool is not None:
        try:
            db_pool.closeall()
        except Exception as exc:
            log(
                f"DB POOL CLOSE ERROR: {repr(exc)}"
            )

        db_pool = None


# ============================================================
# FASTAPI EVENTS
# ============================================================

@app.on_event("startup")
def on_startup():
    worker = threading.Thread(
        target=startup_worker,
        name="geniosa-startup",
        daemon=True
    )

    worker.start()


@app.on_event("shutdown")
def on_shutdown():
    shutdown_worker()


# ============================================================
# LOCAL ENTRYPOINT
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False
    )
