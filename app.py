# ============================================================
# GENIOSA 4.9
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL
#
# MEMORY OPTIMIZED
# BACKWARD COMPATIBLE DATABASE
#
# IMPORTANT:
# - Existing PostgreSQL data is preserved.
# - Existing tables are migrated automatically.
# - No DELETE / DROP of user data.
# - System facts are seeded only when needed.
# - Gemini fact extraction is NOT performed on every message.
# - Context is bounded for better speed.
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
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI


# ============================================================
# CONFIGURATION
# ============================================================

APP_VERSION = "4.9"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

OWNER_ID = os.getenv(
    "GENIOSA_OWNER_ID",
    ""
).strip()

TELEGRAM_TIMEOUT = 30
TELEGRAM_SEND_TIMEOUT = 30
GEMINI_TIMEOUT = 45

POLL_SLEEP = 1.5

MAX_TELEGRAM_MESSAGE = 3900
MAX_CONTEXT_MESSAGES = 10
MAX_CONTEXT_FACTS = 35
MAX_CONTEXT_MEMORIES = 25
MAX_PENDING_FACTS = 10
MAX_CONFLICT_FACTS = 10

SYSTEM_SEED_VERSION = "4.9-v1"


# ============================================================
# GLOBAL STATE
# ============================================================

app = FastAPI(title="GENIOSA", version=APP_VERSION)

db_pool = None

database_ready = False
polling_started = False
startup_completed = False

polling_lock = threading.Lock()

last_update_id = None
last_poll_error = None

telegram_ok = False
gemini_ok = False


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).isoformat()
    print(f"[GENIOSA {now}] {message}", flush=True)


# ============================================================
# DATABASE POOL
# ============================================================

def init_db_pool():
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    if db_pool is not None:
        return

    db_pool = pool.SimpleConnectionPool(
        1,
        5,
        DATABASE_URL,
        connect_timeout=15
    )

    log("DATABASE CONNECTION POOL READY")


def get_connection():
    if db_pool is None:
        init_db_pool()

    return db_pool.getconn()


def release_connection(conn):
    if db_pool is not None and conn is not None:
        db_pool.putconn(conn)


def close_db_pool():
    global db_pool

    if db_pool is not None:
        try:
            db_pool.closeall()
        except Exception:
            pass

        db_pool = None


# ============================================================
# DATABASE HELPERS
# ============================================================

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
        conn = get_connection()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute(query, params or ())

        rows = None

        if fetchone:
            rows = cur.fetchone()

        elif fetch:
            rows = cur.fetchall()

        if commit:
            conn.commit()

        return rows

    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass

        raise e

    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass

        release_connection(conn)


# ============================================================
# DATABASE TRANSACTION
# ============================================================

def db_transaction(callback):
    conn = None
    cur = None

    try:
        conn = get_connection()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        result = callback(cur)

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

        release_connection(conn)


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


def ensure_column(
    table_name,
    column_name,
    column_definition
):
    columns = get_columns(table_name)

    if column_name in columns:
        return

    # Table/column names here are internal constants,
    # not user input.
    db_execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN {column_name} {column_definition}
        """
    )

    log(
        f"ADDED COLUMN {table_name}.{column_name}"
    )


def create_index_safe(index_name, sql):
    try:
        db_execute(sql)
    except Exception as e:
        log(
            f"INDEX WARNING {index_name}: {repr(e)}"
        )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    global database_ready

    init_db_pool()

    # --------------------------------------------------------
    # MESSAGES
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            role TEXT,
            message TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    ensure_column(
        "messages",
        "chat_id",
        "BIGINT"
    )

    ensure_column(
        "messages",
        "role",
        "TEXT"
    )

    ensure_column(
        "messages",
        "message",
        "TEXT"
    )

    ensure_column(
        "messages",
        "text",
        "TEXT"
    )

    ensure_column(
        "messages",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # Old schema compatibility:
    # if old records have text but message is NULL,
    # copy text into message.
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
    # MEMORIES
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            memory_type TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 5,
            source TEXT DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    ensure_column(
        "memories",
        "chat_id",
        "BIGINT"
    )

    # IMPORTANT:
    # Older versions may have used "memory" instead of "content".
    memory_columns = get_columns("memories")

    if "content" not in memory_columns:

        if "memory" in memory_columns:
            ensure_column(
                "memories",
                "content",
                "TEXT"
            )

            db_execute(
                """
                UPDATE memories
                SET content = memory
                WHERE content IS NULL
                  AND memory IS NOT NULL
                """
            )

        else:
            ensure_column(
                "memories",
                "content",
                "TEXT"
            )

    ensure_column(
        "memories",
        "memory_type",
        "TEXT DEFAULT 'general'"
    )

    ensure_column(
        "memories",
        "importance",
        "INTEGER DEFAULT 5"
    )

    ensure_column(
        "memories",
        "source",
        "TEXT DEFAULT 'user'"
    )

    ensure_column(
        "memories",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    ensure_column(
        "memories",
        "updated_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # MEMORY EVENTS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    ensure_column(
        "memory_events",
        "chat_id",
        "BIGINT"
    )

    ensure_column(
        "memory_events",
        "event_type",
        "TEXT"
    )

    ensure_column(
        "memory_events",
        "content",
        "TEXT"
    )

    ensure_column(
        "memory_events",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # FACTS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            status TEXT DEFAULT 'CONFIRMED',
            source TEXT DEFAULT 'user',
            category TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    fact_columns = get_columns("facts")

    ensure_column(
        "facts",
        "chat_id",
        "BIGINT"
    )

    # Very important backward compatibility.
    # Older versions may have used "fact".
    fact_columns = get_columns("facts")

    if "content" not in fact_columns:

        if "fact" in fact_columns:
            ensure_column(
                "facts",
                "content",
                "TEXT"
            )

            db_execute(
                """
                UPDATE facts
                SET content = fact
                WHERE content IS NULL
                  AND fact IS NOT NULL
                """
            )

        else:
            ensure_column(
                "facts",
                "content",
                "TEXT"
            )

    ensure_column(
        "facts",
        "status",
        "TEXT DEFAULT 'CONFIRMED'"
    )

    ensure_column(
        "facts",
        "source",
        "TEXT DEFAULT 'user'"
    )

    ensure_column(
        "facts",
        "category",
        "TEXT DEFAULT 'general'"
    )

    ensure_column(
        "facts",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    ensure_column(
        "facts",
        "updated_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # Normalize old/null statuses.
    db_execute(
        """
        UPDATE facts
        SET status = 'CONFIRMED'
        WHERE status IS NULL
           OR TRIM(status) = ''
        """
    )

    db_execute(
        """
        UPDATE facts
        SET status = UPPER(status)
        WHERE status IS NOT NULL
        """
    )

    db_execute(
        """
        UPDATE facts
        SET status = 'CONFIRMED'
        WHERE status NOT IN (
            'CONFIRMED',
            'PENDING',
            'CONFLICT',
            'REJECTED'
        )
        """
    )

    # --------------------------------------------------------
    # FACT HISTORY
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id BIGSERIAL PRIMARY KEY,
            fact_id BIGINT,
            old_status TEXT,
            new_status TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # DECISIONS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            decision TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # TASKS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            title TEXT,
            description TEXT,
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            due_date TIMESTAMP NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # PROJECTS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id BIGSERIAL PRIMARY KEY,
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
    # RESEARCH
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS research (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            topic TEXT,
            findings TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # FINANCIAL MODELS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            project_name TEXT,
            model_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # BOT PROJECTS
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            id BIGSERIAL PRIMARY KEY,
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
    # BOT FILES
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS bot_files (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            filename TEXT,
            filepath TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # SYSTEM SEED META
    # --------------------------------------------------------

    db_execute(
        """
        CREATE TABLE IF NOT EXISTS system_seed_meta (
            chat_id BIGINT PRIMARY KEY,
            seed_version TEXT NOT NULL,
            seeded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # INDEXES
    # --------------------------------------------------------

    create_index_safe(
        "idx_messages_chat_created",
        """
        CREATE INDEX IF NOT EXISTS idx_messages_chat_created
        ON messages(chat_id, created_at DESC)
        """
    )

    create_index_safe(
        "idx_memories_chat_importance",
        """
        CREATE INDEX IF NOT EXISTS idx_memories_chat_importance
        ON memories(chat_id, importance DESC, id DESC)
        """
    )

    create_index_safe(
        "idx_facts_chat_status",
        """
        CREATE INDEX IF NOT EXISTS idx_facts_chat_status
        ON facts(chat_id, status)
        """
    )

    create_index_safe(
        "idx_tasks_chat_status",
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_chat_status
        ON tasks(chat_id, status)
        """
    )

    create_index_safe(
        "idx_projects_chat_status",
        """
        CREATE INDEX IF NOT EXISTS idx_projects_chat_status
        ON projects(chat_id, status)
        """
    )

    database_ready = True

    log(
        "DATABASE INITIALIZED AND MIGRATED SUCCESSFULLY"
    )


# ============================================================
# SYSTEM FACTS
# ============================================================

SYSTEM_FACTS = [

    # --------------------------------------------------------
    # COMPANY
    # --------------------------------------------------------

    {
        "content": "კომპანიის სახელი: SAMTISI CONSTRUCTION LLC.",
        "category": "company",
        "importance": 10
    },

    {
        "content": "SAMTISI CONSTRUCTION LLC-ის კომპანიის ID: 406391202.",
        "category": "company",
        "importance": 10
    },

    {
        "content": "SAMTISI CONSTRUCTION LLC დაარსებულია 2022 წლის 5 დეკემბერს.",
        "category": "company",
        "importance": 8
    },

    {
        "content": "SAMTISI CONSTRUCTION LLC-ის სათაო ოფისი მდებარეობს თბილისში, საქართველოში.",
        "category": "company",
        "importance": 8
    },

    {
        "content": "SAMTISI CONSTRUCTION არის Construction & Development Company.",
        "category": "company",
        "importance": 9
    },

    {
        "content": "SAMTISI-ის საქმიანობა მოიცავს საცხოვრებელ, კომერციულ და ინფრასტრუქტურულ სამშენებლო და დეველოპერულ პროექტებს.",
        "category": "company",
        "importance": 8
    },

    {
        "content": "SAMTISI-ის გუნდი მართავს დაგეგმვას, საინჟინრო კოორდინაციას, მშენებლობას, დასრულებას, ტექნიკურ მენეჯმენტს და ხარისხის კონტროლს.",
        "category": "company",
        "importance": 8
    },

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    {
        "content": "NIKKEA 12 პროექტი მდებარეობს ქუთაისში, ნიკეას ქუჩა 12-ში.",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის მიწის ფართობია 3,070 მ².",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის საერთო გასაყიდი ფართობია 10,854 მ².",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის სასტუმროს ტიპის აპარტამენტებისა და ოთახების ფართობია 7,212 მ².",
        "category": "NIKKEA 12",
        "importance": 9
    },

    {
        "content": "NIKKEA 12-ის მონოლითური სამუშაოების მოცულობა არის დაახლოებით 21,500 მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის არქიტექტურული სამუშაოების მოცულობა არის დაახლოებით 16,000 მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის პირველ სართულზე კომერციული ფართობია 836 მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის მეორე სართულზე კომერციული ფართობია 1,006 მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის მე-3-დან მე-14 სართულებამდე განთავსებულია დაახლოებით 7,212 მ² სასტუმროს ტიპის აპარტამენტები/ოთახები.",
        "category": "NIKKEA 12",
        "importance": 9
    },

    {
        "content": "NIKKEA 12-ის კონცეფცია მოიცავს სასტუმროს ტიპის აპარტამენტებს, კაზინოს, სავაჭრო ცენტრს, რესტორანს და ზედა სართულზე lounge bar-ს.",
        "category": "NIKKEA 12",
        "importance": 9
    },

    {
        "content": "NIKKEA 12-ის მიწის მესაკუთრის მოთხოვნაა 1,800 მ² აპარტამენტები, 25 საპარკინგე ადგილი და 300,000 აშშ დოლარი ნაღდი თანხა.",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის მონოლითური მშენებლობის ღირებულებად განსაზღვრულია ზუსტად 170 აშშ დოლარი/მ².",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის დარჩენილი სამუშაოების fit-out-ის სამუშაო ვარაუდია მაქსიმუმ 450 აშშ დოლარი/მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის აპარტამენტების საწყისი გასაყიდი ფასის დიაპაზონია მინიმუმ 1,500–1,800 აშშ დოლარი/მ².",
        "category": "NIKKEA 12",
        "importance": 9
    },

    {
        "content": "NIKKEA 12-ის კომერციული ფართების საწყისი გასაყიდი ფასი განიხილებოდა დაახლოებით 2,500 აშშ დოლარი/მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის განვითარების JV მოდელში ინვესტორის წილი არის 80%, ხოლო SAMTISI-ის ოპერატორის წილი 20%.",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის შემოთავაზებულ მოდელში ინვესტორი აფინანსებს პროექტის მთლიანი ღირებულების დაახლოებით 35%-ს პლუს 300,000 აშშ დოლარს, ხოლო დარჩენილი ნაწილი ფინანსდება ბანკით.",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის ბანკის დაფინანსების სავარაუდო საპროცენტო განაკვეთის ზედა ზღვარი განიხილებოდა დაახლოებით 11.5–13.5%.",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის მოდელში ინვესტორის კაპიტალი უნდა დაბრუნდეს პროექტის მოგებიდან, რის შემდეგაც ინვესტორი იღებს წმინდა მოგების 80%-ს.",
        "category": "NIKKEA 12",
        "importance": 9
    },

    {
        "content": "NIKKEA 12-ის ქონების/სერვისის ცალკე კომპანიაში SAMTISI-სა და ინვესტორის წილები განიხილებოდა 50/50.",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის გაქირავების მოდელში განიხილებოდა გაქირავებიდან მიღებული შემოსავლის 30% ქონების/სერვისის კომპანიას და 70% მესაკუთრეს.",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის აპარტამენტების სავარაუდო დღიური გაქირავების ფასი განიხილებოდა 150–300 აშშ დოლარის ფარგლებში.",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის 1,006 მ² კაზინოს ფართობი არ უნდა გაიყიდოს და უნდა დარჩეს პროექტის შემოსავლის მომტან აქტივად.",
        "category": "NIKKEA 12",
        "importance": 10
    },

    {
        "content": "NIKKEA 12-ის კაზინოს ფართობის სავარაუდო ღირებულება განიხილებოდა მინიმუმ 2,000 აშშ დოლარი/მ².",
        "category": "NIKKEA 12",
        "importance": 8
    },

    {
        "content": "NIKKEA 12-ის კაზინოს გაქირავების სავარაუდო მინიმალურ დონედ განიხილებოდა დაახლოებით 50 აშშ დოლარი/მ² თვეში.",
        "category": "NIKKEA 12",
        "importance": 8
    },

    # --------------------------------------------------------
    # SAMGORI
    # --------------------------------------------------------

    {
        "content": "Samgori პროექტი მდებარეობს თბილისში, სამგორის რაიონში, გიორგი ნადერიშვილის ქუჩაზე.",
        "category": "Samgori",
        "importance": 10
    },

    {
        "content": "Samgori პროექტის მიწის ფართობია 7,390 მ².",
        "category": "Samgori",
        "importance": 10
    },

    {
        "content": "Samgori პროექტის საერთო აშენებული ფართობია 46,131 მ².",
        "category": "Samgori",
        "importance": 9
    },

    {
        "content": "Samgori პროექტის სამშენებლო მოცულობა არის 41,874 მ³.",
        "category": "Samgori",
        "importance": 8
    },

    {
        "content": "Samgori პროექტის გასაყიდი ფართობი პარკინგის გარეშე არის 30,540 მ².",
        "category": "Samgori",
        "importance": 10
    },

    {
        "content": "Samgori პროექტის პარკინგის ფართობია 6,516 მ².",
        "category": "Samgori",
        "importance": 8
    },

    {
        "content": "Samgori პროექტში საცხოვრებელი ფართობია 22,143 მ².",
        "category": "Samgori",
        "importance": 8
    },

    {
        "content": "Samgori პროექტში summer area-ის ფართობია 5,040 მ².",
        "category": "Samgori",
        "importance": 7
    },

    {
        "content": "Samgori პროექტში კომერციული ფართობია 1,647 მ².",
        "category": "Samgori",
        "importance": 8
    },

    {
        "content": "Samgori პროექტში საოფისე ფართობია 1,710 მ².",
        "category": "Samgori",
        "importance": 8
    },

    {
        "content": "Samgori პროექტის მშენებლობის ღირებულება განიხილებოდა დაახლოებით 253 აშშ დოლარი/მ² გასაყიდ ფართობზე.",
        "category": "Samgori",
        "importance": 8
    },

    {
        "content": "Samgori პროექტის მიწის მესაკუთრის მოთხოვნაა დაახლოებით 7,750,000 აშშ დოლარი.",
        "category": "Samgori",
        "importance": 10
    },

    {
        "content": "Samgori პროექტის მშენებლობისთვის საჭირო კაპიტალი განიხილებოდა დაახლოებით 15,000,000 აშშ დოლარი.",
        "category": "Samgori",
        "importance": 10
    },

    {
        "content": "Samgori პროექტის საერთო კაპიტალის საჭიროება განიხილებოდა დაახლოებით 23,500,000 აშშ დოლარი.",
        "category": "Samgori",
        "importance": 10
    },

    {
        "content": "Samgori პროექტის მოსალოდნელი წმინდა მოგება დაახლოებით 10,000,000 აშშ დოლარია.",
        "category": "Samgori",
        "importance": 9
    },

    # --------------------------------------------------------
    # GOLDEN LAKE / OQRI
    # --------------------------------------------------------

    {
        "content": "Golden Lake / Oqri Lake პროექტის ძირითადი მიწის ფართობი არის დაახლოებით 46 ჰექტარი.",
        "category": "Golden Lake",
        "importance": 9
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტისთვის ტბისა და მიმდებარე ტერიტორიის საერთო კონცეფციური არეალი დაახლოებით 70–80 ჰექტარია.",
        "category": "Golden Lake",
        "importance": 8
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტის მიწის ღირებულება განიხილებოდა დაახლოებით 35–40 მილიონი აშშ დოლარი.",
        "category": "Golden Lake",
        "importance": 9
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში დაგეგმილი სამშენებლო ფართობი დაახლოებით 250,000 მ²-ია.",
        "category": "Golden Lake",
        "importance": 9
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტი მოიცავს 5-ვარსკვლავიან დაახლოებით 200-ოთახიან სასტუმროს, aquapark-ს და კაზინოს.",
        "category": "Golden Lake",
        "importance": 8
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში განიხილება 4-ვარსკვლავიანი სასტუმრო დაახლოებით 100–120 ოთახით.",
        "category": "Golden Lake",
        "importance": 7
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში დაგეგმილია დაახლოებით 10,000 მაყურებელზე გათვლილი სპორტული/საკონცერტო არენა.",
        "category": "Golden Lake",
        "importance": 8
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში არენის ფართობი განიხილებოდა დაახლოებით 20,000–30,000 მ².",
        "category": "Golden Lake",
        "importance": 7
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში აპარტამენტების გასაყიდი ფართობი დაახლოებით 150,000 მ²-ია.",
        "category": "Golden Lake",
        "importance": 9
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში კომერციული ფართობი დაახლოებით 30,000 მ²-ია.",
        "category": "Golden Lake",
        "importance": 8
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში აპარტამენტების fit-out ღირებულება განიხილებოდა დაახლოებით 1,000–1,200 აშშ დოლარი/მ².",
        "category": "Golden Lake",
        "importance": 7
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში აპარტამენტების გასაყიდი ფასი განიხილებოდა დაახლოებით 2,500–3,000 აშშ დოლარი/მ².",
        "category": "Golden Lake",
        "importance": 8
    },

    {
        "content": "Golden Lake / Oqri Lake პროექტში კომერციული ფართების გასაყიდი ფასი განიხილებოდა დაახლოებით 3,500–5,000 აშშ დოლარი/მ².",
        "category": "Golden Lake",
        "importance": 8
    }
]


# ============================================================
# SYSTEM FACT SEEDING
# ============================================================

def ensure_system_facts(chat_id):
    """
    Seed system facts only once per chat/version.

    This is one of the biggest performance improvements
    compared with GENIOSA 4.8.
    """

    if not database_ready:
        return

    try:
        row = db_execute(
            """
            SELECT seed_version
            FROM system_seed_meta
            WHERE chat_id = %s
            """,
            (chat_id,),
            fetchone=True
        )

        if row and row["seed_version"] == SYSTEM_SEED_VERSION:
            return

        def transaction(cur):

            for item in SYSTEM_FACTS:

                content = item["content"]
                category = item.get(
                    "category",
                    "general"
                )
                importance = item.get(
                    "importance",
                    5
                )

                # ------------------------------------------------
                # FACT
                # ------------------------------------------------

                cur.execute(
                    """
                    SELECT id
                    FROM facts
                    WHERE chat_id = %s
                      AND content IS NOT NULL
                      AND LOWER(TRIM(content))
                          = LOWER(TRIM(%s))
                    LIMIT 1
                    """,
                    (
                        chat_id,
                        content
                    )
                )

                existing_fact = cur.fetchone()

                if not existing_fact:

                    cur.execute(
                        """
                        INSERT INTO facts (
                            chat_id,
                            content,
                            status,
                            source,
                            category,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            %s,
                            %s,
                            'CONFIRMED',
                            'system',
                            %s,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """,
                        (
                            chat_id,
                            content,
                            category
                        )
                    )

                # ------------------------------------------------
                # MEMORY
                # ------------------------------------------------

                cur.execute(
                    """
                    SELECT id
                    FROM memories
                    WHERE chat_id = %s
                      AND content IS NOT NULL
                      AND LOWER(TRIM(content))
                          = LOWER(TRIM(%s))
                    LIMIT 1
                    """,
                    (
                        chat_id,
                        content
                    )
                )

                existing_memory = cur.fetchone()

                if not existing_memory:

                    cur.execute(
                        """
                        INSERT INTO memories (
                            chat_id,
                            content,
                            memory_type,
                            importance,
                            source,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            %s,
                            %s,
                            'system_fact',
                            %s,
                            'system',
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        """,
                        (
                            chat_id,
                            content,
                            importance
                        )
                    )

            # ----------------------------------------------------
            # SEED VERSION
            # ----------------------------------------------------

            cur.execute(
                """
                INSERT INTO system_seed_meta (
                    chat_id,
                    seed_version,
                    seeded_at
                )
                VALUES (
                    %s,
                    %s,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    seed_version = EXCLUDED.seed_version,
                    seeded_at = CURRENT_TIMESTAMP
                """,
                (
                    chat_id,
                    SYSTEM_SEED_VERSION
                )
            )

        db_transaction(transaction)

        log(
            f"SYSTEM FACTS SEEDED chat={chat_id}"
        )

    except Exception as e:
        log(
            f"SEED ERROR chat={chat_id}: {repr(e)}"
        )


# ============================================================
# MESSAGE STORAGE
# ============================================================

def save_message(
    chat_id,
    role,
    text
):
    if not text:
        return

    try:
        db_execute(
            """
            INSERT INTO messages (
                chat_id,
                role,
                message,
                text,
                created_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                CURRENT_TIMESTAMP
            )
            """,
            (
                chat_id,
                role,
                text,
                text
            )
        )

    except Exception as e:
        log(
            f"save_message ERROR: {repr(e)}"
        )


def get_recent_messages(
    chat_id,
    limit=MAX_CONTEXT_MESSAGES
):
    try:
        rows = db_execute(
            """
            SELECT
                role,
                COALESCE(message, text) AS message,
                created_at
            FROM messages
            WHERE chat_id = %s
              AND COALESCE(message, text) IS NOT NULL
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        )

        return list(reversed(rows or []))

    except Exception as e:
        log(
            f"get_recent_messages ERROR: {repr(e)}"
        )
        return []


# ============================================================
# MEMORY
# ============================================================

def save_memory(
    chat_id,
    content,
    memory_type="general",
    importance=7,
    source="user"
):
    if not content:
        return False

    content = content.strip()

    if not content:
        return False

    try:

        def transaction(cur):

            cur.execute(
                """
                SELECT id
                FROM memories
                WHERE chat_id = %s
                  AND content IS NOT NULL
                  AND LOWER(TRIM(content))
                      = LOWER(TRIM(%s))
                LIMIT 1
                """,
                (
                    chat_id,
                    content
                )
            )

            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE memories
                    SET importance = GREATEST(
                            COALESCE(importance, 0),
                            %s
                        ),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        importance,
                        existing["id"]
                    )
                )

                return False

            cur.execute(
                """
                INSERT INTO memories (
                    chat_id,
                    content,
                    memory_type,
                    importance,
                    source,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                RETURNING id
                """,
                (
                    chat_id,
                    content,
                    memory_type,
                    importance,
                    source
                )
            )

            inserted = cur.fetchone()

            if inserted:

                cur.execute(
                    """
                    INSERT INTO memory_events (
                        chat_id,
                        event_type,
                        content,
                        created_at
                    )
                    VALUES (
                        %s,
                        'memory_created',
                        %s,
                        CURRENT_TIMESTAMP
                    )
                    """,
                    (
                        chat_id,
                        content
                    )
                )

                return True

            return False

        return bool(
            db_transaction(transaction)
        )

    except Exception as e:
        log(
            f"save_memory ERROR: {repr(e)}"
        )
        return False


def get_memories(
    chat_id,
    limit=MAX_CONTEXT_MEMORIES
):
    try:
        rows = db_execute(
            """
            SELECT
                id,
                content,
                memory_type,
                importance,
                source,
                created_at
            FROM memories
            WHERE chat_id = %s
              AND content IS NOT NULL
              AND TRIM(content) <> ''
            ORDER BY
                importance DESC NULLS LAST,
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
            f"get_memories ERROR: {repr(e)}"
        )
        return []


# ============================================================
# FACTS
# ============================================================

VALID_FACT_STATUSES = {
    "CONFIRMED",
    "PENDING",
    "CONFLICT",
    "REJECTED"
}


def create_fact(
    chat_id,
    content,
    status="PENDING",
    source="user",
    category="general"
):
    if not content:
        return None

    content = content.strip()

    if not content:
        return None

    status = str(status).upper().strip()

    if status not in VALID_FACT_STATUSES:
        status = "PENDING"

    try:

        def transaction(cur):

            cur.execute(
                """
                SELECT
                    id,
                    status
                FROM facts
                WHERE chat_id = %s
                  AND content IS NOT NULL
                  AND LOWER(TRIM(content))
                      = LOWER(TRIM(%s))
                LIMIT 1
                """,
                (
                    chat_id,
                    content
                )
            )

            existing = cur.fetchone()

            if existing:

                if status == "CONFIRMED":

                    cur.execute(
                        """
                        UPDATE facts
                        SET status = 'CONFIRMED',
                            source = %s,
                            category = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            source,
                            category,
                            existing["id"]
                        )
                    )

                return existing["id"]

            cur.execute(
                """
                INSERT INTO facts (
                    chat_id,
                    content,
                    status,
                    source,
                    category,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                RETURNING id
                """,
                (
                    chat_id,
                    content,
                    status,
                    source,
                    category
                )
            )

            row = cur.fetchone()

            return row["id"] if row else None

        return db_transaction(transaction)

    except Exception as e:
        log(
            f"create_fact ERROR: {repr(e)}"
        )
        return None


def get_facts(
    chat_id,
    status="CONFIRMED",
    limit=MAX_CONTEXT_FACTS
):
    try:

        if status:
            rows = db_execute(
                """
                SELECT
                    id,
                    content,
                    status,
                    source,
                    category,
                    created_at,
                    updated_at
                FROM facts
                WHERE chat_id = %s
                  AND status = %s
                  AND content IS NOT NULL
                  AND TRIM(content) <> ''
                ORDER BY
                    CASE
                        WHEN source = 'system'
                        THEN 1
                        ELSE 2
                    END,
                    id DESC
                LIMIT %s
                """,
                (
                    chat_id,
                    status,
                    limit
                ),
                fetch=True
            )

        else:

            rows = db_execute(
                """
                SELECT
                    id,
                    content,
                    status,
                    source,
                    category,
                    created_at,
                    updated_at
                FROM facts
                WHERE chat_id = %s
                  AND content IS NOT NULL
                  AND TRIM(content) <> ''
                ORDER BY id DESC
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
            f"get_facts ERROR: {repr(e)}"
        )
        return []


# ============================================================
# FACT STATUS CONTROL
# ============================================================

def confirm_fact(
    chat_id,
    fact_id
):
    try:

        def transaction(cur):

            cur.execute(
                """
                SELECT
                    id,
                    status
                FROM facts
                WHERE id = %s
                  AND chat_id = %s
                FOR UPDATE
                """,
                (
                    fact_id,
                    chat_id
                )
            )

            row = cur.fetchone()

            if not row:
                return False

            old_status = row["status"]

            cur.execute(
                """
                UPDATE facts
                SET status = 'CONFIRMED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (fact_id,)
            )

            cur.execute(
                """
                INSERT INTO fact_history (
                    fact_id,
                    old_status,
                    new_status,
                    reason,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    'CONFIRMED',
                    'confirmed by user',
                    CURRENT_TIMESTAMP
                )
                """,
                (
                    fact_id,
                    old_status
                )
            )

            return True

        return db_transaction(transaction)

    except Exception as e:
        log(
            f"confirm_fact ERROR: {repr(e)}"
        )
        return False


def reject_fact(
    chat_id,
    fact_id
):
    try:

        def transaction(cur):

            cur.execute(
                """
                SELECT
                    id,
                    status
                FROM facts
                WHERE id = %s
                  AND chat_id = %s
                FOR UPDATE
                """,
                (
                    fact_id,
                    chat_id
                )
            )

            row = cur.fetchone()

            if not row:
                return False

            old_status = row["status"]

            cur.execute(
                """
                UPDATE facts
                SET status = 'REJECTED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (fact_id,)
            )

            cur.execute(
                """
                INSERT INTO fact_history (
                    fact_id,
                    old_status,
                    new_status,
                    reason,
                    created_at
                )
                VALUES (
                    %s,
                    %s,
                    'REJECTED',
                    'rejected by user',
                    CURRENT_TIMESTAMP
                )
                """,
                (
                    fact_id,
                    old_status
                )
            )

            return True

        return db_transaction(transaction)

    except Exception as e:
        log(
            f"reject_fact ERROR: {repr(e)}"
        )
        return False


# ============================================================
# MEMORY / FACT DETECTION
# ============================================================

MEMORY_WORDS = [
    "დაიმახსოვრე",
    "დამიმახსოვრე",
    "დაიმახსოვროს",
    "ჩაიწერე",
    "შეინახე",
    "დაიტოვე მეხსიერებაში",
    "გახსოვდეს",
    "remember",
    "memorize",
    "save this",
    "keep this in memory"
]


CONFIRM_WORDS = [
    "დაადასტურე",
    "ვადასტურებ",
    "ეს ფაქტია",
    "დადასტურებულია",
    "confirm",
    "confirmed"
]


def contains_memory_request(text):
    lower = text.lower()

    return any(
        phrase in lower
        for phrase in MEMORY_WORDS
    )


def contains_confirmation(text):
    lower = text.lower()

    return any(
        phrase in lower
        for phrase in CONFIRM_WORDS
    )


def is_confirmed_fact_request(text):
    return (
        contains_memory_request(text)
        or contains_confirmation(text)
    )


def capture_direct_company_fact(
    chat_id,
    text
):
    """
    Lightweight local detection.
    Does not call Gemini.
    """

    patterns = [
        r"(?:კომპანიის\s+სახელია|company\s+name\s+is)\s*[:\-]?\s*(.+)",
        r"(?:ჩვენი\s+კომპანიაა|our\s+company\s+is)\s*[:\-]?\s*(.+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = match.group(1).strip()

            if value:

                content = (
                    f"კომპანიის სახელი: {value}"
                )

                create_fact(
                    chat_id,
                    content,
                    status="CONFIRMED",
                    source="user",
                    category="company"
                )

                save_memory(
                    chat_id,
                    content,
                    memory_type="company",
                    importance=10,
                    source="user"
                )

                return True

    return False


def extract_simple_memory_text(text):
    """
    For explicit memory commands we normally store the
    user's statement itself.

    This avoids an additional Gemini call and makes memory
    much faster.
    """

    cleaned = text.strip()

    # Remove common memory prefixes.
    prefixes = [
        "დაიმახსოვრე:",
        "დაიმახსოვრე",
        "დამიმახსოვრე:",
        "დამიმახსოვრე",
        "ჩაიწერე:",
        "ჩაიწერე",
        "შეინახე:",
        "შეინახე",
        "remember:",
        "remember",
        "save this:",
        "save this"
    ]

    lower = cleaned.lower()

    for prefix in prefixes:

        if lower.startswith(prefix.lower()):

            result = cleaned[len(prefix):].strip(
                " :,-"
            )

            if result:
                return result

    return cleaned


def capture_memory(
    chat_id,
    user_text
):
    """
    IMPORTANT PERFORMANCE RULE:

    Gemini is NOT called here for normal messages.

    Memory extraction happens only when:
    - user explicitly asks to remember/save something
    - user confirms a fact
    - direct company fact is detected
    """

    try:

        direct = capture_direct_company_fact(
            chat_id,
            user_text
        )

        if direct:
            return

        if not is_confirmed_fact_request(
            user_text
        ):
            return

        memory_text = extract_simple_memory_text(
            user_text
        )

        if not memory_text:
            return

        fact_id = create_fact(
            chat_id,
            memory_text,
            status="CONFIRMED",
            source="user",
            category="user_memory"
        )

        save_memory(
            chat_id,
            memory_text,
            memory_type="user_memory",
            importance=9,
            source="user"
        )

        log(
            f"MEMORY CAPTURED chat={chat_id} "
            f"fact_id={fact_id}"
        )

    except Exception as e:
        log(
            f"capture_memory ERROR: {repr(e)}"
        )


# ============================================================
# CONTEXT
# ============================================================

def format_context(
    chat_id,
    user_text
):
    confirmed = get_facts(
        chat_id,
        status="CONFIRMED",
        limit=MAX_CONTEXT_FACTS
    )

    memories = get_memories(
        chat_id,
        limit=MAX_CONTEXT_MEMORIES
    )

    pending = get_facts(
        chat_id,
        status="PENDING",
        limit=MAX_PENDING_FACTS
    )

    conflicts = get_facts(
        chat_id,
        status="CONFLICT",
        limit=MAX_CONFLICT_FACTS
    )

    recent = get_recent_messages(
        chat_id,
        limit=MAX_CONTEXT_MESSAGES
    )

    parts = []

    # --------------------------------------------------------
    # CONFIRMED FACTS
    # --------------------------------------------------------

    parts.append(
        "CONFIRMED FACTS:"
    )

    for row in confirmed:

        parts.append(
            f"- [{row.get('category', 'general')}] "
            f"{row.get('content', '')}"
        )

    # --------------------------------------------------------
    # MEMORIES
    # --------------------------------------------------------

    parts.append(
        "\nUSER MEMORIES:"
    )

    for row in memories:

        parts.append(
            f"- {row.get('content', '')}"
        )

    # --------------------------------------------------------
    # PENDING
    # --------------------------------------------------------

    if pending:

        parts.append(
            "\nPENDING FACTS:"
        )

        for row in pending:

            parts.append(
                f"- {row.get('content', '')}"
            )

    # --------------------------------------------------------
    # CONFLICTS
    # --------------------------------------------------------

    if conflicts:

        parts.append(
            "\nCONFLICTING FACTS:"
        )

        for row in conflicts:

            parts.append(
                f"- {row.get('content', '')}"
            )

    # --------------------------------------------------------
    # RECENT CONVERSATION
    # --------------------------------------------------------

    parts.append(
        "\nRECENT CONVERSATION:"
    )

    for row in recent:

        role = row.get(
            "role",
            "unknown"
        )

        message = row.get(
            "message",
            ""
        )

        if message:

            parts.append(
                f"{role.upper()}: {message}"
            )

    return "\n".join(parts)


# ============================================================
# SYSTEM PROMPT
# ============================================================

def build_system_prompt(
    chat_id,
    user_text
):

    context = format_context(
        chat_id,
        user_text
    )

    return f"""
You are GENIOSA 4.9.

You are the personal business advisor and assistant
of the founder of SAMTISI CONSTRUCTION LLC.

Your main functions:

1. Business advisory
2. Construction and development analysis
3. Project financial analysis
4. Investment analysis
5. Investor communication
6. Project management support
7. Research assistance
8. Strategic decision support
9. Memory and knowledge management
10. Technical assistance

IMPORTANT RULES:

- Always distinguish confirmed facts from assumptions.
- Never invent financial numbers.
- Never invent legal facts.
- Never present an assumption as a confirmed fact.
- If information is missing, say that it is missing.
- If you are uncertain, clearly say so.
- When current market, legal, regulatory or other external
  information is required, state that current verification
  is necessary.
- Do not silently change confirmed project numbers.
- When the user provides a new explicit fact and asks you
  to remember it, it should be treated as persistent memory.
- Existing confirmed facts have priority over guesses.
- If two facts conflict, point out the conflict rather than
  choosing silently.
- Answer in the language used by the user unless another
  language is requested.
- Be practical and business-oriented.
- For financial calculations, show the logic and assumptions.
- Do not claim that you performed an external action if you
  did not actually perform it.

GENIOSA DATABASE CONTEXT:

{context}

CURRENT USER MESSAGE:

{user_text}
"""


# ============================================================
# GEMINI
# ============================================================

def gemini_generate(
    prompt,
    temperature=0.2,
    max_output_tokens=2048
):
    global gemini_ok

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured"
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
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
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens
        }
    }

    response = requests.post(
        url,
        params={
            "key": GEMINI_API_KEY
        },
        json=payload,
        timeout=GEMINI_TIMEOUT
    )

    if response.status_code != 200:

        gemini_ok = False

        raise RuntimeError(
            "Gemini API error "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

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

        text = part.get(
            "text"
        )

        if text:
            answer_parts.append(text)

    answer = "".join(
        answer_parts
    ).strip()

    if not answer:
        raise RuntimeError(
            "Gemini returned empty answer"
        )

    gemini_ok = True

    return answer


# ============================================================
# ANSWER GENERATION
# ============================================================

def generate_answer(
    chat_id,
    user_text
):

    prompt = build_system_prompt(
        chat_id,
        user_text
    )

    return gemini_generate(
        prompt,
        temperature=0.2,
        max_output_tokens=2048
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )


def telegram_call(
    method,
    payload=None,
    timeout=TELEGRAM_TIMEOUT
):
    global telegram_ok

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

        telegram_ok = False

        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    data = response.json()

    if not data.get("ok"):

        telegram_ok = False

        raise RuntimeError(
            f"Telegram API error: {data}"
        )

    telegram_ok = True

    return data


def send_message(
    chat_id,
    text
):
    if not text:
        return

    # Telegram has a message-size limit.
    chunks = []

    while len(text) > MAX_TELEGRAM_MESSAGE:

        cut = text.rfind(
            "\n",
            0,
            MAX_TELEGRAM_MESSAGE
        )

        if cut < 1000:
            cut = MAX_TELEGRAM_MESSAGE

        chunks.append(
            text[:cut]
        )

        text = text[cut:].lstrip()

    if text:
        chunks.append(text)

    for chunk in chunks:

        telegram_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            },
            timeout=TELEGRAM_SEND_TIMEOUT
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def help_text():
    return """
GENIOSA 4.9

ძირითადი ბრძანებები:

/health
/summary
/memory
/facts
/tasks
/projects
/help

ფაქტები:

/confirm_fact ID
/reject_fact ID

GENIOSA ინარჩუნებს:
• დადასტურებულ ფაქტებს
• მეხსიერებას
• პროექტებს
• გადაწყვეტილებებს
• ამოცანებს
• საუბრის ისტორიას

თუ გინდა რამე დავიმახსოვრო, დაწერე მაგალითად:

„დაიმახსოვრე: ეს არის ახალი პროექტის ძირითადი პირობა.“
"""


def health_text():
    return f"""
GENIOSA {APP_VERSION}

Telegram: {"OK" if telegram_ok else "CONFIGURED"}
Gemini: {"OK" if gemini_ok else "CONFIGURED"}
Database: {"READY" if database_ready else "NOT READY"}
Model: {GEMINI_MODEL}
Polling: {"RUNNING" if polling_started else "STOPPED"}
Startup: {"COMPLETE" if startup_completed else "IN PROGRESS"}
"""


def memory_report(chat_id):
    memories = get_memories(
        chat_id,
        limit=50
    )

    if not memories:
        return "GENIOSA-ს მეხსიერებაში ამ ჩატისთვის ჩანაწერები არ მოიძებნა."

    lines = [
        "GENIOSA MEMORY"
    ]

    for index, row in enumerate(
        memories,
        start=1
    ):

        lines.append(
            f"{index}. "
            f"{row.get('content', '')}"
        )

    return "\n".join(lines)


def facts_report(chat_id):
    facts = get_facts(
        chat_id,
        status=None,
        limit=100
    )

    if not facts:
        return "ფაქტები ვერ მოიძებნა."

    lines = [
        "GENIOSA FACTS"
    ]

    for row in facts:

        lines.append(
            f"[{row.get('id')}] "
            f"{row.get('status')} | "
            f"{row.get('category')} | "
            f"{row.get('content')}"
        )

    return "\n".join(lines)


def tasks_report(chat_id):
    try:
        rows = db_execute(
            """
            SELECT
                id,
                title,
                description,
                status,
                priority,
                due_date
            FROM tasks
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT 100
            """,
            (chat_id,),
            fetch=True
        )

        if not rows:
            return "ამოცანები არ მოიძებნა."

        lines = [
            "GENIOSA TASKS"
        ]

        for row in rows:

            lines.append(
                f"[{row.get('id')}] "
                f"{row.get('status')} | "
                f"{row.get('priority')} | "
                f"{row.get('title')}"
            )

        return "\n".join(lines)

    except Exception as e:

        log(
            f"tasks_report ERROR: {repr(e)}"
        )

        return "ამოცანების წაკითხვა ვერ მოხერხდა."


def projects_report(chat_id):
    try:
        rows = db_execute(
            """
            SELECT
                id,
                name,
                description,
                status
            FROM projects
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT 100
            """,
            (chat_id,),
            fetch=True
        )

        if not rows:
            return "პროექტები არ მოიძებნა."

        lines = [
            "GENIOSA PROJECTS"
        ]

        for row in rows:

            lines.append(
                f"[{row.get('id')}] "
                f"{row.get('status')} | "
                f"{row.get('name')}"
            )

        return "\n".join(lines)

    except Exception as e:

        log(
            f"projects_report ERROR: {repr(e)}"
        )

        return "პროექტების წაკითხვა ვერ მოხერხდა."


# ============================================================
# COMMAND PROCESSING
# ============================================================

def process_command(
    chat_id,
    text
):
    stripped = text.strip()

    if stripped == "/start":
        ensure_system_facts(chat_id)

        return (
            "გამარჯობა. მე GENIOSA 4.9 ვარ.\n\n"
            "მეხსიერება და მონაცემთა ბაზა მზად არის.\n"
            "გამოიყენე /help ბრძანებების სანახავად."
        )

    if stripped == "/help":
        return help_text()

    if stripped == "/health":
        return health_text()

    if stripped == "/memory":
        return memory_report(chat_id)

    if stripped == "/facts":
        return facts_report(chat_id)

    if stripped == "/tasks":
        return tasks_report(chat_id)

    if stripped == "/projects":
        return projects_report(chat_id)

    if stripped == "/summary":

        recent = get_recent_messages(
            chat_id,
            limit=20
        )

        if not recent:
            return "საუბრის ისტორია ჯერ არ არის."

        lines = [
            "ბოლო საუბრის მოკლე ისტორია:"
        ]

        for row in recent:

            lines.append(
                f"{row.get('role', '').upper()}: "
                f"{row.get('message', '')}"
            )

        return "\n".join(lines)

    confirm_match = re.match(
        r"^/confirm_fact\s+(\d+)$",
        stripped,
        re.IGNORECASE
    )

    if confirm_match:

        fact_id = int(
            confirm_match.group(1)
        )

        if confirm_fact(
            chat_id,
            fact_id
        ):
            return (
                f"ფაქტი #{fact_id} დადასტურებულია."
            )

        return (
            f"ფაქტი #{fact_id} ვერ მოიძებნა."
        )

    reject_match = re.match(
        r"^/reject_fact\s+(\d+)$",
        stripped,
        re.IGNORECASE
    )

    if reject_match:

        fact_id = int(
            reject_match.group(1)
        )

        if reject_fact(
            chat_id,
            fact_id
        ):
            return (
                f"ფაქტი #{fact_id} უარყოფილია."
            )

        return (
            f"ფაქტი #{fact_id} ვერ მოიძებნა."
        )

    return None


# ============================================================
# USER MESSAGE PROCESSING
# ============================================================

def process_user_message(
    chat_id,
    user_text
):
    try:

        log(
            f"USER MESSAGE chat={chat_id}: "
            f"{user_text[:500]}"
        )

        # ----------------------------------------------------
        # Make sure this chat has system knowledge.
        #
        # On already initialized chats this function performs
        # one very cheap metadata query and immediately returns.
        # ----------------------------------------------------

        ensure_system_facts(chat_id)

        # ----------------------------------------------------
        # SAVE USER MESSAGE
        # ----------------------------------------------------

        save_message(
            chat_id,
            "user",
            user_text
        )

        # ----------------------------------------------------
        # MEMORY
        #
        # Only explicit memory requests are processed.
        # Normal messages do NOT trigger Gemini extraction.
        # ----------------------------------------------------

        capture_memory(
            chat_id,
            user_text
        )

        # ----------------------------------------------------
        # COMMAND
        # ----------------------------------------------------

        command_result = process_command(
            chat_id,
            user_text
        )

        if command_result is not None:

            save_message(
                chat_id,
                "assistant",
                command_result
            )

            send_message(
                chat_id,
                command_result
            )

            return

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        answer = generate_answer(
            chat_id,
            user_text
        )

        # ----------------------------------------------------
        # SAVE ANSWER
        # ----------------------------------------------------

        save_message(
            chat_id,
            "assistant",
            answer
        )

        # ----------------------------------------------------
        # SEND TELEGRAM
        # ----------------------------------------------------

        send_message(
            chat_id,
            answer
        )

        log(
            f"ANSWER SENT chat={chat_id}"
        )

    except Exception as e:

        log(
            f"PROCESS MESSAGE ERROR chat={chat_id}: "
            f"{repr(e)}"
        )

        traceback.print_exc()

        fallback = (
            "ბოდიში, ტექნიკური შეცდომა დაფიქსირდა. "
            "მონაცემები არ წაშლილა. "
            "გთხოვ, იგივე კითხვა კიდევ ერთხელ გამომიგზავნე."
        )

        try:
            save_message(
                chat_id,
                "assistant",
                fallback
            )

            send_message(
                chat_id,
                fallback
            )

        except Exception as send_error:

            log(
                f"FALLBACK SEND ERROR: "
                f"{repr(send_error)}"
            )


# ============================================================
# TELEGRAM UPDATE PROCESSING
# ============================================================

def process_update(update):

    try:

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

        text = message.get(
            "text"
        )

        if not text:
            return

        process_user_message(
            chat_id,
            text
        )

    except Exception as e:

        log(
            f"UPDATE ERROR: {repr(e)}"
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_delete_webhook():

    try:

        telegram_call(
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
            f"Webhook clear warning: {repr(e)}"
        )


def telegram_polling_loop():

    global last_update_id
    global last_poll_error

    log(
        f"GENIOSA {APP_VERSION} polling loop starting..."
    )

    telegram_delete_webhook()

    while True:

        try:

            payload = {
                "timeout": TELEGRAM_TIMEOUT,
                "allowed_updates": [
                    "message"
                ]
            }

            if last_update_id is not None:

                payload["offset"] = (
                    last_update_id + 1
                )

            result = telegram_call(
                "getUpdates",
                payload,
                timeout=TELEGRAM_TIMEOUT + 10
            )

            updates = result.get(
                "result",
                []
            )

            last_poll_error = None

            for update in updates:

                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:
                    last_update_id = update_id

                process_update(
                    update
                )

        except Exception as e:

            last_poll_error = repr(e)

            log(
                f"TELEGRAM POLLING ERROR: "
                f"{repr(e)}"
            )

            time.sleep(
                min(
                    10,
                    POLL_SLEEP * 3
                )
            )

        time.sleep(
            POLL_SLEEP
        )


# ============================================================
# GET KNOWN CHATS
# ============================================================

def get_known_chat_ids():

    chat_ids = set()

    try:

        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM messages
            WHERE chat_id IS NOT NULL
            """,
            fetch=True
        )

        for row in rows or []:

            if row["chat_id"] is not None:
                chat_ids.add(
                    int(row["chat_id"])
                )

    except Exception as e:

        log(
            f"KNOWN CHATS messages error: {repr(e)}"
        )

    try:

        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM facts
            WHERE chat_id IS NOT NULL
            """,
            fetch=True
        )

        for row in rows or []:

            if row["chat_id"] is not None:
                chat_ids.add(
                    int(row["chat_id"])
                )

    except Exception as e:

        log(
            f"KNOWN CHATS facts error: {repr(e)}"
        )

    try:

        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM memories
            WHERE chat_id IS NOT NULL
            """,
            fetch=True
        )

        for row in rows or []:

            if row["chat_id"] is not None:
                chat_ids.add(
                    int(row["chat_id"])
                )

    except Exception as e:

        log(
            f"KNOWN CHATS memories error: {repr(e)}"
        )

    return sorted(
        chat_ids
    )


# ============================================================
# STARTUP
# ============================================================

def startup_worker():

    global startup_completed
    global polling_started

    try:

        log(
            f"GENIOSA {APP_VERSION} STARTUP"
        )

        log(
            "CONFIG: "
            f"telegram={bool(TELEGRAM_BOT_TOKEN)}, "
            f"gemini={bool(GEMINI_API_KEY)}, "
            f"database={bool(DATABASE_URL)}, "
            f"model={GEMINI_MODEL}"
        )

        # ----------------------------------------------------
        # DATABASE
        # ----------------------------------------------------

        init_db()

        log(
            "DATABASE READY"
        )

        # ----------------------------------------------------
        # EXISTING CHATS
        # ----------------------------------------------------

        chat_ids = get_known_chat_ids()

        log(
            f"KNOWN CHAT IDS: {chat_ids}"
        )

        # Seed only once/version.
        for chat_id in chat_ids:

            try:

                ensure_system_facts(
                    chat_id
                )

            except Exception as e:

                log(
                    f"SEED EXISTING CHAT ERROR "
                    f"{chat_id}: {repr(e)}"
                )

        # ----------------------------------------------------
        # TELEGRAM POLLING
        # ----------------------------------------------------

        if not TELEGRAM_BOT_TOKEN:

            log(
                "TELEGRAM TOKEN NOT SET"
            )

        else:

            with polling_lock:

                if not polling_started:

                    thread = threading.Thread(
                        target=telegram_polling_loop,
                        daemon=True,
                        name="geniosa-telegram-polling"
                    )

                    thread.start()

                    polling_started = True

                    log(
                        "Telegram polling thread started."
                    )

        startup_completed = True

        log(
            f"GENIOSA {APP_VERSION} STARTUP COMPLETE"
        )

    except Exception as e:

        log(
            f"STARTUP ERROR: {repr(e)}"
        )

        traceback.print_exc()


# ============================================================
# FASTAPI STARTUP
# ============================================================

@app.on_event("startup")
def startup_event():

    thread = threading.Thread(
        target=startup_worker,
        daemon=True,
        name="geniosa-startup"
    )

    thread.start()


# ============================================================
# FASTAPI SHUTDOWN
# ============================================================

@app.on_event("shutdown")
def shutdown_event():

    log(
        "GENIOSA SHUTDOWN"
    )

    close_db_pool()


# ============================================================
# HTTP ROUTES
# ============================================================

@app.get("/")
def root():

    return {
        "service": "GENIOSA",
        "version": APP_VERSION,
        "status": "online",
        "database": database_ready,
        "polling": polling_started
    }


@app.head("/")
def root_head():

    return


@app.get("/health")
def http_health():

    database_status = False

    if database_ready:

        try:

            row = db_execute(
                "SELECT 1 AS ok",
                fetchone=True
            )

            database_status = bool(
                row and row["ok"] == 1
            )

        except Exception:
            database_status = False

    return {
        "service": "GENIOSA",
        "version": APP_VERSION,
        "status": "online",
        "database": database_status,
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "model": GEMINI_MODEL,
        "polling_started": polling_started,
        "startup_completed": startup_completed,
        "last_poll_error": last_poll_error
    }


# ============================================================
# LOCAL RUN
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
