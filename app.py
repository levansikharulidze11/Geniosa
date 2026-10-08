# ============================================================
# GENIOSA 5.0
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL + FastAPI
#
# IMPORTANT:
# - Existing PostgreSQL data is preserved.
# - No DROP TABLE.
# - Legacy schemas are migrated automatically.
# - Telegram polling uses PostgreSQL advisory lock.
# - System facts are seeded only once per chat/version.
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
# CONFIG
# ============================================================

APP_VERSION = "5.0"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()

TELEGRAM_TIMEOUT = 35
POLLING_SLEEP = 2
MAX_MESSAGE_LENGTH = 3800

SEED_VERSION = "5.0-v1"

app = FastAPI(title="GENIOSA 5.0")


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).isoformat()
    print(f"[GENIOSA {now}] {message}", flush=True)


# ============================================================
# CONFIG STATUS
# ============================================================

def config_status():
    return {
        "telegram": bool(TELEGRAM_BOT_TOKEN),
        "gemini": bool(GEMINI_API_KEY),
        "database": bool(DATABASE_URL),
        "model": GEMINI_MODEL,
    }


# ============================================================
# DATABASE POOL
# ============================================================

DB_POOL = None
DB_POOL_LOCK = threading.Lock()


def init_db_pool():
    global DB_POOL

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    with DB_POOL_LOCK:
        if DB_POOL is not None:
            return

        DB_POOL = psycopg2.pool.ThreadedConnectionPool(
            1,
            5,
            DATABASE_URL
        )

        log("DATABASE CONNECTION POOL READY")


def db_connection():
    if DB_POOL is None:
        init_db_pool()

    return DB_POOL.getconn()


def release_connection(conn):
    if DB_POOL is not None and conn is not None:
        DB_POOL.putconn(conn)


def db_execute(
    sql,
    params=None,
    fetch=False,
    fetchone=False,
    commit=True
):
    conn = None
    cur = None

    try:
        conn = db_connection()

        cur = conn.cursor(
            RealDictCursor if fetch or fetchone else None
        )

        cur.execute(sql, params or ())

        result = None

        if fetch:
            result = cur.fetchall()

        elif fetchone:
            result = cur.fetchone()

        if commit:
            conn.commit()

        return result

    except Exception:
        if conn:
            conn.rollback()
        raise

    finally:
        if cur:
            cur.close()
        if conn:
            release_connection(conn)


# ============================================================
# SCHEMA HELPERS
# ============================================================

def table_exists(table_name):
    row = db_execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public'
              AND table_name=%s
        ) AS exists
        """,
        (table_name,),
        fetchone=True
    )

    return bool(row["exists"])


def column_exists(table_name, column_name):
    row = db_execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name=%s
              AND column_name=%s
        ) AS exists
        """,
        (table_name, column_name),
        fetchone=True
    )

    return bool(row["exists"])


def add_column(table_name, column_name, definition):
    if not column_exists(table_name, column_name):
        db_execute(
            f'ALTER TABLE "{table_name}" '
            f'ADD COLUMN "{column_name}" {definition}'
        )
        log(f"ADDED COLUMN {table_name}.{column_name}")


def create_table_if_missing(sql):
    db_execute(sql)


# ============================================================
# DATABASE MIGRATION
# ============================================================

def migrate_database():

    init_db_pool()

    # --------------------------------------------------------
    # messages
    # --------------------------------------------------------

    create_table_if_missing(
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

    add_column("messages", "chat_id", "BIGINT")
    add_column("messages", "role", "TEXT")
    add_column("messages", "message", "TEXT")
    add_column("messages", "text", "TEXT")
    add_column(
        "messages",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # Preserve old text/message data.
    db_execute(
        """
        UPDATE messages
        SET message = text
        WHERE (message IS NULL OR message='')
          AND text IS NOT NULL
        """
    )

    db_execute(
        """
        UPDATE messages
        SET text = message
        WHERE (text IS NULL OR text='')
          AND message IS NOT NULL
        """
    )

    # --------------------------------------------------------
    # memories
    # --------------------------------------------------------

    create_table_if_missing(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            content TEXT,
            memory TEXT,
            memory_type TEXT DEFAULT 'general',
            importance INTEGER DEFAULT 5,
            source TEXT DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    add_column("memories", "chat_id", "BIGINT")
    add_column("memories", "content", "TEXT")
    add_column("memories", "memory", "TEXT")
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
        "TEXT DEFAULT 'user'"
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

    db_execute(
        """
        UPDATE memories
        SET content = memory
        WHERE (content IS NULL OR content='')
          AND memory IS NOT NULL
        """
    )

    db_execute(
        """
        UPDATE memories
        SET memory = content
        WHERE (memory IS NULL OR memory='')
          AND content IS NOT NULL
        """
    )

    # --------------------------------------------------------
    # memory_events
    # --------------------------------------------------------

    create_table_if_missing(
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

    add_column("memory_events", "chat_id", "BIGINT")
    add_column("memory_events", "event_type", "TEXT")
    add_column("memory_events", "content", "TEXT")
    add_column(
        "memory_events",
        "created_at",
        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    )

    # --------------------------------------------------------
    # facts
    # --------------------------------------------------------

    create_table_if_missing(
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Legacy compatibility.
    add_column("facts", "chat_id", "BIGINT")
    add_column("facts", "fact_key", "TEXT")
    add_column("facts", "value", "TEXT")
    add_column("facts", "fact", "TEXT")
    add_column("facts", "content", "TEXT")
    add_column(
        "facts",
        "source",
        "TEXT DEFAULT 'user'"
    )
    add_column(
        "facts",
        "status",
        "TEXT DEFAULT 'CONFIRMED'"
    )
    add_column(
        "facts",
        "category",
        "TEXT DEFAULT 'general'"
    )
    add_column(
        "facts",
        "memory_type",
        "TEXT DEFAULT 'fact'"
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

    # Copy all legacy content into the common content/value fields.
    db_execute(
        """
        UPDATE facts
        SET content = COALESCE(
            NULLIF(content,''),
            NULLIF(fact,''),
            NULLIF(value,'')
        )
        WHERE content IS NULL OR content=''
        """
    )

    db_execute(
        """
        UPDATE facts
        SET fact = content
        WHERE (fact IS NULL OR fact='')
          AND content IS NOT NULL
        """
    )

    db_execute(
        """
        UPDATE facts
        SET value = content
        WHERE (value IS NULL OR value='')
          AND content IS NOT NULL
        """
    )

    # --------------------------------------------------------
    # CRITICAL LEGACY FIX:
    # fact_key may be NOT NULL in the old database.
    # Fill missing keys without deleting anything.
    # --------------------------------------------------------

    db_execute(
        """
        UPDATE facts
        SET fact_key =
            CASE
                WHEN category IS NOT NULL
                    AND category <> ''
                THEN category || '_' || id::TEXT
                ELSE 'legacy_fact_' || id::TEXT
            END
        WHERE fact_key IS NULL
           OR fact_key=''
        """
    )

    # Normalize status.
    db_execute(
        """
        UPDATE facts
        SET status = 'CONFIRMED'
        WHERE status IS NULL
           OR LOWER(status) IN (
               'confirmed',
               'confirm',
               'approved',
               'active'
           )
        """
    )

    # --------------------------------------------------------
    # fact_history
    # --------------------------------------------------------

    create_table_if_missing(
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id BIGSERIAL PRIMARY KEY,
            fact_id BIGINT,
            chat_id BIGINT,
            old_value TEXT,
            new_value TEXT,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # decisions
    # --------------------------------------------------------

    create_table_if_missing(
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
    # tasks
    # --------------------------------------------------------

    create_table_if_missing(
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
    # projects
    # --------------------------------------------------------

    create_table_if_missing(
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
    # research
    # --------------------------------------------------------

    create_table_if_missing(
        """
        CREATE TABLE IF NOT EXISTS research (
            id BIGSERIAL PRIMARY KEY,
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

    create_table_if_missing(
        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            project_name TEXT,
            data JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # bot_projects
    # --------------------------------------------------------

    create_table_if_missing(
        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            name TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # bot_files
    # --------------------------------------------------------

    create_table_if_missing(
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
    # system seed metadata
    # --------------------------------------------------------

    create_table_if_missing(
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

    safe_indexes = [
        """
        CREATE INDEX IF NOT EXISTS idx_messages_chat_time
        ON messages(chat_id, created_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_memories_chat
        ON memories(chat_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_facts_chat
        ON facts(chat_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_facts_chat_status
        ON facts(chat_id, status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_chat_status
        ON tasks(chat_id, status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_projects_chat
        ON projects(chat_id)
        """,
    ]

    for sql in safe_indexes:
        try:
            db_execute(sql)
        except Exception as e:
            log(f"INDEX WARNING: {repr(e)}")

    log("DATABASE INITIALIZED AND MIGRATED SUCCESSFULLY")
    log("DATABASE READY")


# ============================================================
# SYSTEM KNOWLEDGE
# ============================================================

SYSTEM_FACTS = [

    # --------------------------------------------------------
    # COMPANY
    # --------------------------------------------------------

    (
        "company_name",
        "კომპანიის სახელი: SAMTISI CONSTRUCTION LLC",
        "company"
    ),
    (
        "company_id",
        "კომპანიის ID: 406391202",
        "company"
    ),
    (
        "company_founded",
        "კომპანია დაარსებულია 2022 წლის 5 დეკემბერს.",
        "company"
    ),
    (
        "company_location",
        "კომპანიის სათაო ოფისი მდებარეობს თბილისში, საქართველოში.",
        "company"
    ),
    (
        "company_industry",
        "SAMTISI CONSTRUCTION LLC არის Construction & Development Company.",
        "company"
    ),
    (
        "company_role",
        "მომხმარებელი არის SAMTISI CONSTRUCTION LLC-ის დამფუძნებელი/ხელმძღვანელი.",
        "company"
    ),
    (
        "company_scope",
        "კომპანიის საქმიანობა მოიცავს საცხოვრებელ, კომერციულ და ინფრასტრუქტურულ სამშენებლო/დეველოპერულ პროექტებს.",
        "company"
    ),

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    (
        "nikkea_location",
        "NIKKEA 12 პროექტი მდებარეობს ქუთაისში, ნიკეას ქუჩა 12-ში.",
        "nikkea12"
    ),
    (
        "nikkea_land",
        "NIKKEA 12-ის მიწის ფართობია 3,070 მ².",
        "nikkea12"
    ),
    (
        "nikkea_saleable",
        "NIKKEA 12-ის საერთო გასაყიდი ფართობია 10,854 მ².",
        "nikkea12"
    ),
    (
        "nikkea_rooms",
        "NIKKEA 12-ში სასტუმროს ტიპის აპარტამენტების/ნომრების ფართობია 7,212 მ².",
        "nikkea12"
    ),
    (
        "nikkea_monolith",
        "NIKKEA 12-ის მონოლითური სამუშაოების მოცულობაა დაახლოებით 21,500 მ².",
        "nikkea12"
    ),
    (
        "nikkea_architecture",
        "NIKKEA 12-ის არქიტექტურული ფართობი შეადგენს დაახლოებით 16,000 მ²-ს.",
        "nikkea12"
    ),
    (
        "nikkea_commercial_1",
        "NIKKEA 12-ის პირველი სართულის კომერციული ფართობია 836 მ².",
        "nikkea12"
    ),
    (
        "nikkea_commercial_2",
        "NIKKEA 12-ის მეორე სართულის კომერციული ფართობია 1,006 მ².",
        "nikkea12"
    ),
    (
        "nikkea_concept",
        "NIKKEA 12-ის კონცეფცია მოიცავს სასტუმროს ტიპის აპარტამენტებს, კაზინოს, სავაჭრო ცენტრს, რესტორანს და ზედა სართულზე lounge bar-ს.",
        "nikkea12"
    ),
    (
        "nikkea_landowner",
        "NIKKEA 12-ის მიწის მესაკუთრის მოთხოვნაა 1,800 მ² აპარტამენტები, 25 საპარკინგე ადგილი და მხოლოდ 300,000 აშშ დოლარი ნაღდი თანხა.",
        "nikkea12"
    ),
    (
        "nikkea_monolith_cost",
        "NIKKEA 12-ში მონოლითური მშენებლობის გათვალისწინებული ფასი არის ზუსტად 170 აშშ დოლარი/მ².",
        "nikkea12"
    ),
    (
        "nikkea_fitout",
        "NIKKEA 12-ის დარჩენილი ფართების fit-out-ის სამუშაოებში გამოყენებული მაქსიმალური გათვლითი დაშვებაა 450 აშშ დოლარი/მ².",
        "nikkea12"
    ),
    (
        "nikkea_apartment_price",
        "NIKKEA 12-ში აპარტამენტების საწყისი გასაყიდი ფასის სამუშაო დიაპაზონია 1,500–1,800 აშშ დოლარი/მ².",
        "nikkea12"
    ),
    (
        "nikkea_commercial_price",
        "NIKKEA 12-ში კომერციული ფართების სამუშაო გასაყიდი ფასი არის 2,500 აშშ დოლარი/მ².",
        "nikkea12"
    ),
    (
        "nikkea_jv",
        "NIKKEA 12-ის განვითარების JV მოდელში ინვესტორის წილია 80%, ხოლო SAMTISI-ის ოპერატორის წილი 20%.",
        "nikkea12"
    ),
    (
        "nikkea_investor",
        "NIKKEA 12-ში ინვესტორის მოთხოვნილი მონაწილეობა განიხილებოდა პროექტის საერთო ღირებულების 35% + 300,000 აშშ დოლარის ოდენობით.",
        "nikkea12"
    ),
    (
        "nikkea_bank",
        "NIKKEA 12-ის დაფინანსების დარჩენილი ნაწილი განიხილება საბანკო დაფინანსებით; სამუშაო დაშვება ბანკის პროცენტზე არის დაახლოებით 11.5–13.5% მაქსიმუმ.",
        "nikkea12"
    ),
    (
        "nikkea_casino",
        "NIKKEA 12-ში 1,006 მ² ფართობის კაზინოს გაყიდვა არ არის დაგეგმილი.",
        "nikkea12"
    ),
    (
        "nikkea_casino_value",
        "NIKKEA 12-ის კაზინოს ფართობის სამუშაო შეფასება არის მინიმუმ 2,000 აშშ დოლარი/მ².",
        "nikkea12"
    ),
    (
        "nikkea_casino_rent",
        "NIKKEA 12-ის კაზინოს შესაძლო მინიმალურ ქირად განხილულია დაახლოებით 50 აშშ დოლარი/მ²/თვეში.",
        "nikkea12"
    ),
    (
        "nikkea_property_company",
        "NIKKEA 12-ის აპარტამენტების გაქირავებისა და მომსახურებისთვის განიხილება ცალკე property/service კომპანია, სადაც ინვესტორისა და SAMTISI-ის წილები 50/50 იქნება.",
        "nikkea12"
    ),
    (
        "nikkea_rental",
        "NIKKEA 12-ის აპარტამენტების შესაძლო დღიური გაქირავების სამუშაო დიაპაზონია 150–300 აშშ დოლარი.",
        "nikkea12"
    ),

    # --------------------------------------------------------
    # SAMGORI
    # --------------------------------------------------------

    (
        "samgori_location",
        "Samgori პროექტი მდებარეობს თბილისში, სამგორის რაიონში, გიორგი ნადერიშვილის ქუჩაზე.",
        "samgori"
    ),
    (
        "samgori_land",
        "Samgori პროექტის მიწის ფართობია 7,390 მ².",
        "samgori"
    ),
    (
        "samgori_build",
        "Samgori პროექტის აშენებადი ფართობია 46,131 მ².",
        "samgori"
    ),
    (
        "samgori_volume",
        "Samgori პროექტის სამშენებლო მოცულობაა 41,874 მ³.",
        "samgori"
    ),
    (
        "samgori_saleable",
        "Samgori პროექტის გასაყიდი ფართობი, პარკინგის გამოკლებით, არის 30,540 მ².",
        "samgori"
    ),
    (
        "samgori_parking",
        "Samgori პროექტის პარკინგის ფართობია 6,516 მ².",
        "samgori"
    ),
    (
        "samgori_land_price",
        "Samgori პროექტის მიწის მესაკუთრის მოთხოვნაა 7,750,000 აშშ დოლარი.",
        "samgori"
    ),
    (
        "samgori_construction",
        "Samgori პროექტის მშენებლობის საჭირო ბიუჯეტად განიხილება დაახლოებით 15,000,000 აშშ დოლარი.",
        "samgori"
    ),
    (
        "samgori_total",
        "Samgori პროექტის საერთო კაპიტალის სამუშაო შეფასებაა დაახლოებით 23,500,000 აშშ დოლარი.",
        "samgori"
    ),
    (
        "samgori_profit",
        "Samgori პროექტის მოსალოდნელ წმინდა მოგებად განხილულია დაახლოებით 10,000,000 აშშ დოლარი.",
        "samgori"
    ),
    (
        "samgori_construction_unit",
        "Samgori პროექტის სამუშაო სამშენებლო თვითღირებულების გათვლა არის დაახლოებით 253 აშშ დოლარი/მ² გასაყიდ ფართზე.",
        "samgori"
    ),

    # --------------------------------------------------------
    # GOLDEN LAKE / OQRI
    # --------------------------------------------------------

    (
        "goldenlake_land",
        "Golden Lake / Oqri Lake კონცეფციის ძირითადი მიწის ფართობია დაახლოებით 46 ჰა.",
        "goldenlake"
    ),
    (
        "goldenlake_area",
        "ტბისა და მიმდებარე ტერიტორიის საერთო კონცეფციური მასშტაბი განიხილებოდა დაახლოებით 70–80 ჰა.",
        "goldenlake"
    ),
    (
        "goldenlake_land_cost",
        "Golden Lake / Oqri პროექტისთვის მიწის ღირებულების სამუშაო დიაპაზონია 35–40 მილიონი აშშ დოლარი.",
        "goldenlake"
    ),
    (
        "goldenlake_construction",
        "Golden Lake / Oqri პროექტის დაგეგმილი სამშენებლო პროგრამაა დაახლოებით 250,000 მ².",
        "goldenlake"
    ),
    (
        "goldenlake_hotel",
        "კონცეფცია მოიცავს 5-ვარსკვლავიან 200-ნომრიან სასტუმროს, aquapark-ს და კაზინოს.",
        "goldenlake"
    ),
    (
        "goldenlake_fourstar",
        "კონცეფცია ასევე მოიცავს 4-ვარსკვლავიან სასტუმროს დაახლოებით 100–120 ნომრით.",
        "goldenlake"
    ),
    (
        "goldenlake_arena",
        "Golden Lake / Oqri-ის კონცეფციაში განიხილება დაახლოებით 10,000 მაყურებელზე გათვლილი სპორტული/ღონისძიებების არენა, დაახლოებით 20,000–30,000 მ² ფართობით.",
        "goldenlake"
    ),
    (
        "goldenlake_apartments",
        "Golden Lake / Oqri-ის კონცეფციაში განიხილება დაახლოებით 150,000 მ² გასაყიდი აპარტამენტები.",
        "goldenlake"
    ),
    (
        "goldenlake_commercial",
        "Golden Lake / Oqri-ის კონცეფციაში განიხილება დაახლოებით 30,000 მ² კომერციული ფართები.",
        "goldenlake"
    ),
    (
        "goldenlake_apartment_price",
        "Golden Lake / Oqri-ის აპარტამენტების სამუშაო გასაყიდი ფასი განიხილებოდა დაახლოებით 2,500–3,000 აშშ დოლარი/მ².",
        "goldenlake"
    ),
    (
        "goldenlake_commercial_price",
        "Golden Lake / Oqri-ის კომერციული ფართების სამუშაო ფასი განიხილებოდა დაახლოებით 3,500–5,000 აშშ დოლარი/მ².",
        "goldenlake"
    ),
    (
        "goldenlake_fitout",
        "Golden Lake / Oqri-ის აპარტამენტების fit-out-ის სამუშაო დაშვებაა დაახლოებით 1,000–1,200 აშშ დოლარი/მ².",
        "goldenlake"
    ),

]


# ============================================================
# SAFE KEY
# ============================================================

def safe_key(value):
    value = str(value or "").strip().lower()

    value = re.sub(
        r"[^a-zA-Z0-9ა-ჰ_-]+",
        "_",
        value
    )

    value = re.sub(
        r"_+",
        "_",
        value
    )

    value = value.strip("_")

    if not value:
        value = "fact"

    return value[:180]


# ============================================================
# FACT INSERT — LEGACY COMPATIBLE
# ============================================================

def create_fact(
    chat_id,
    content,
    category="general",
    source="user",
    status="CONFIRMED",
    fact_key=None
):
    content = str(content or "").strip()

    if not content:
        return None

    key = safe_key(
        fact_key or
        f"{category}_{content[:80]}"
    )

    try:

        # First try to update an existing matching fact.
        row = db_execute(
            """
            SELECT id
            FROM facts
            WHERE chat_id=%s
              AND LOWER(COALESCE(content, fact, value, ''))
                  = LOWER(%s)
            ORDER BY id
            LIMIT 1
            """,
            (chat_id, content),
            fetchone=True
        )

        if row:
            db_execute(
                """
                UPDATE facts
                SET
                    content=%s,
                    fact=%s,
                    value=%s,
                    fact_key=COALESCE(
                        NULLIF(fact_key,''),
                        %s
                    ),
                    source=%s,
                    status=%s,
                    category=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=%s
                """,
                (
                    content,
                    content,
                    content,
                    key,
                    source,
                    status,
                    category,
                    row["id"]
                )
            )

            return row["id"]

        # Normal insert.
        row = db_execute(
            """
            INSERT INTO facts
            (
                chat_id,
                fact_key,
                value,
                fact,
                content,
                source,
                status,
                category,
                memory_type,
                created_at,
                updated_at
            )
            VALUES
            (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            RETURNING id
            """,
            (
                chat_id,
                key,
                content,
                content,
                content,
                source,
                status,
                category,
                "fact"
            ),
            fetchone=True
        )

        return row["id"] if row else None

    except Exception as e:
        log(f"create_fact ERROR: {repr(e)}")
        return None


# ============================================================
# MEMORY
# ============================================================

def create_memory(
    chat_id,
    content,
    memory_type="general",
    importance=7,
    source="user"
):
    content = str(content or "").strip()

    if not content:
        return None

    try:

        row = db_execute(
            """
            SELECT id
            FROM memories
            WHERE chat_id=%s
              AND LOWER(COALESCE(content,memory,''))=LOWER(%s)
            LIMIT 1
            """,
            (chat_id, content),
            fetchone=True
        )

        if row:
            db_execute(
                """
                UPDATE memories
                SET
                    content=%s,
                    memory=%s,
                    memory_type=%s,
                    importance=%s,
                    source=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=%s
                """,
                (
                    content,
                    content,
                    memory_type,
                    importance,
                    source,
                    row["id"]
                )
            )

            return row["id"]

        row = db_execute(
            """
            INSERT INTO memories
            (
                chat_id,
                content,
                memory,
                memory_type,
                importance,
                source,
                created_at,
                updated_at
            )
            VALUES
            (
                %s,%s,%s,%s,%s,%s,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            RETURNING id
            """,
            (
                chat_id,
                content,
                content,
                memory_type,
                importance,
                source
            ),
            fetchone=True
        )

        return row["id"] if row else None

    except Exception as e:
        log(f"create_memory ERROR: {repr(e)}")
        return None


# ============================================================
# SYSTEM SEED
# ============================================================

def seed_system_facts(chat_id):
    """
    Seeds known company/project facts once per version.
    Existing user data is never deleted.
    """

    try:
        row = db_execute(
            """
            SELECT seed_version
            FROM system_seed_meta
            WHERE chat_id=%s
            """,
            (chat_id,),
            fetchone=True
        )

        if row and row["seed_version"] == SEED_VERSION:
            return

        log(f"SEED START chat={chat_id}")

        for fact_key, content, category in SYSTEM_FACTS:

            create_fact(
                chat_id=chat_id,
                content=content,
                category=category,
                source="system",
                status="CONFIRMED",
                fact_key=fact_key
            )

        db_execute(
            """
            INSERT INTO system_seed_meta
            (
                chat_id,
                seed_version,
                seeded_at
            )
            VALUES
            (%s,%s,CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id)
            DO UPDATE SET
                seed_version=EXCLUDED.seed_version,
                seeded_at=CURRENT_TIMESTAMP
            """,
            (
                chat_id,
                SEED_VERSION
            )
        )

        log(f"SEED COMPLETE chat={chat_id}")

    except Exception as e:
        log(f"SEED ERROR chat={chat_id}: {repr(e)}")


# ============================================================
# GET FACTS
# ============================================================

def get_facts(chat_id, limit=60):
    try:
        return db_execute(
            """
            SELECT
                id,
                fact_key,
                COALESCE(
                    NULLIF(content,''),
                    NULLIF(fact,''),
                    NULLIF(value,'')
                ) AS content,
                source,
                status,
                category,
                created_at
            FROM facts
            WHERE chat_id=%s
              AND UPPER(COALESCE(status,'CONFIRMED'))
                  = 'CONFIRMED'
            ORDER BY
                CASE
                    WHEN source='system' THEN 0
                    ELSE 1
                END,
                id ASC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True
        ) or []

    except Exception as e:
        log(f"get_facts ERROR: {repr(e)}")
        return []


# ============================================================
# GET MEMORIES
# ============================================================

def get_memories(chat_id, limit=30):
    try:
        return db_execute(
            """
            SELECT
                id,
                COALESCE(
                    NULLIF(content,''),
                    NULLIF(memory,'')
                ) AS content,
                memory_type,
                importance,
                source,
                created_at
            FROM memories
            WHERE chat_id=%s
            ORDER BY importance DESC, id DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True
        ) or []

    except Exception as e:
        log(f"get_memories ERROR: {repr(e)}")
        return []


# ============================================================
# RECENT MESSAGES
# ============================================================

def get_recent_messages(chat_id, limit=12):
    try:
        rows = db_execute(
            """
            SELECT
                role,
                COALESCE(message,text) AS message,
                created_at
            FROM messages
            WHERE chat_id=%s
            ORDER BY id DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True
        ) or []

        return list(reversed(rows))

    except Exception as e:
        log(f"get_recent_messages ERROR: {repr(e)}")
        return []


# ============================================================
# SAVE MESSAGE
# ============================================================

def save_message(chat_id, role, message):
    message = str(message or "")

    if not message:
        return

    try:
        db_execute(
            """
            INSERT INTO messages
            (
                chat_id,
                role,
                message,
                text,
                created_at
            )
            VALUES
            (
                %s,%s,%s,%s,CURRENT_TIMESTAMP
            )
            """,
            (
                chat_id,
                role,
                message,
                message
            )
        )

    except Exception as e:
        log(f"save_message ERROR: {repr(e)}")


# ============================================================
# EXPLICIT MEMORY DETECTION
# ============================================================

MEMORY_PHRASES = [
    "დაიმახსოვრე",
    "შეინახე მეხსიერებაში",
    "დაიმახსოვრეთ",
    "remember this",
    "remember that",
    "save this",
    "store this",
    "memorize",
]


def should_save_memory(text):
    low = text.lower()

    return any(
        phrase in low
        for phrase in MEMORY_PHRASES
    )


def capture_explicit_memory(chat_id, text):

    if not should_save_memory(text):
        return

    memory_id = create_memory(
        chat_id=chat_id,
        content=text,
        memory_type="explicit_user_memory",
        importance=9,
        source="user"
    )

    if memory_id:
        create_fact(
            chat_id=chat_id,
            content=text,
            category="user_memory",
            source="user",
            status="CONFIRMED",
            fact_key=f"user_memory_{memory_id}"
        )

        log(
            f"MEMORY SAVED chat={chat_id} id={memory_id}"
        )


# ============================================================
# DIRECT FACT CAPTURE
# ============================================================

def capture_direct_company_fact(chat_id, text):

    patterns = [
        r"კომპანია არის (.+)",
        r"კომპანიის სახელია (.+)",
        r"ჩემი კომპანიაა (.+)",
        r"my company is (.+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = match.group(1).strip()

            if len(value) > 2:

                create_fact(
                    chat_id=chat_id,
                    content=f"კომპანიის შესახებ: {value}",
                    category="company",
                    source="user",
                    status="CONFIRMED",
                    fact_key="user_company_statement"
                )

                return True

    return False


# ============================================================
# FORMAT MEMORY
# ============================================================

def format_facts(facts):
    if not facts:
        return "ფაქტები ვერ მოიძებნა."

    lines = []

    for index, row in enumerate(facts, 1):

        content = row.get("content") or ""

        if not content:
            continue

        category = row.get("category") or "general"

        lines.append(
            f"{index}. [{category}] {content}"
        )

    return "\n".join(lines) if lines else "ფაქტები ვერ მოიძებნა."


def format_memories(memories):
    if not memories:
        return "ამ ჩატისთვის მეხსიერებაში ჩანაწერები არ მოიძებნა."

    lines = []

    for index, row in enumerate(memories, 1):

        content = row.get("content") or ""

        if content:
            lines.append(
                f"{index}. {content}"
            )

    return "\n".join(lines) if lines else \
        "ამ ჩატისთვის მეხსიერებაში ჩანაწერები არ მოიძებნა."


# ============================================================
# GEMINI
# ============================================================

def gemini_generate(prompt):

    if not GEMINI_API_KEY:
        return (
            "Gemini API Key კონფიგურირებული არ არის. "
            "გთხოვ Render-ის Environment-ში შეამოწმე GEMINI_API_KEY."
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
        f"?key={GEMINI_API_KEY}"
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
            "temperature": 0.25,
            "maxOutputTokens": 1800
        }
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=60
        )

        if response.status_code != 200:

            log(
                "GEMINI ERROR "
                f"{response.status_code}: "
                f"{response.text[:1000]}"
            )

            return (
                "Gemini-სთან დაკავშირებისას ტექნიკური "
                f"შეცდომა მოხდა ({response.status_code})."
            )

        data = response.json()

        candidates = data.get(
            "candidates",
            []
        )

        if not candidates:
            return "Gemini-მ პასუხი ვერ დააბრუნა."

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        answer = ""

        for part in parts:

            if "text" in part:
                answer += part["text"]

        answer = answer.strip()

        if not answer:
            return "პასუხის ტექსტი ვერ მივიღე Gemini-სგან."

        return answer

    except requests.RequestException as e:

        log(
            f"GEMINI REQUEST ERROR: {repr(e)}"
        )

        return (
            "Gemini-სთან კავშირისას დროებითი "
            "ტექნიკური პრობლემა მოხდა."
        )

    except Exception as e:

        log(
            f"GEMINI UNKNOWN ERROR: {repr(e)}"
        )

        return (
            "პასუხის დამუშავებისას ტექნიკური "
            "შეცდომა მოხდა."
        )


# ============================================================
# AI CONTEXT
# ============================================================

SYSTEM_PROMPT = """
შენ ხარ GENIOSA 5.0 — მომხმარებლის პერსონალური
ბიზნეს-მრჩეველი და ასისტენტი.

მომხმარებელი არის SAMTISI CONSTRUCTION LLC-ის
დამფუძნებელი/ხელმძღვანელი.

შენი მთავარი წესებია:

1. არ გამოიგონო ფაქტები, ფინანსური ციფრები ან იურიდიული ინფორმაცია.
2. თუ ინფორმაცია არ გაქვს, პირდაპირ თქვი, რომ არ იცი.
3. დადასტურებული ფაქტები გამოიყენე როგორც სამუშაო ცოდნა.
4. მომხმარებლის ახალი ინფორმაცია არ ჩათვალო ავტომატურად
   დადასტურებულ ფაქტად, თუ მისი ბუნება გაურკვეველია.
5. ფინანსურ ან საინვესტიციო ანალიზში მკაფიოდ გამოყავი:
   - დადასტურებული მონაცემი
   - მომხმარებლის დაშვება
   - შენი შეფასება
   - საჭირო დამატებითი ინფორმაცია.
6. არასოდეს შეცვალო ან წაშალო ძველი ინფორმაცია თვითნებურად.
7. თუ ორი ფაქტი ეწინააღმდეგება ერთმანეთს, მიუთითე კონფლიქტი.
8. პასუხი იყოს პრაქტიკული, საქმიანი და გასაგები.
9. მომხმარებელს არ უთხრა, რომ რაღაც გახსოვს, თუ მონაცემი
   რეალურად მოწოდებულ კონტექსტში არ არის.
10. თუ კითხვა ეხება მიმდინარე ბაზრის, კანონის, ფასების,
    კომპანიების ან სხვა სწრაფად ცვალებად ინფორმაციას,
    არ წარმოადგინო მოძველებული ინფორმაცია როგორც მიმდინარე ფაქტი.
"""


def build_context(chat_id):

    facts = get_facts(
        chat_id,
        limit=60
    )

    memories = get_memories(
        chat_id,
        limit=25
    )

    messages = get_recent_messages(
        chat_id,
        limit=12
    )

    context_parts = []

    context_parts.append(
        "=== CONFIRMED FACTS ==="
    )

    for row in facts:

        content = row.get("content") or ""

        if content:
            context_parts.append(
                f"- {content}"
            )

    context_parts.append(
        "\n=== SAVED MEMORIES ==="
    )

    for row in memories:

        content = row.get("content") or ""

        if content:
            context_parts.append(
                f"- {content}"
            )

    context_parts.append(
        "\n=== RECENT CONVERSATION ==="
    )

    for row in messages:

        role = row.get("role") or "user"
        message = row.get("message") or ""

        if message:
            context_parts.append(
                f"{role}: {message}"
            )

    return "\n".join(context_parts)


# ============================================================
# ANSWER
# ============================================================

def answer_user(chat_id, user_text):

    context = build_context(chat_id)

    prompt = f"""
{SYSTEM_PROMPT}

ქვემოთ მოცემულია GENIOSA-ს არსებული კონტექსტი.

{context}

=== CURRENT USER MESSAGE ===

{user_text}

უპასუხე მომხმარებლის კითხვას ზუსტად და პრაქტიკულად.

თუ არსებული ფაქტები საკმარისი არ არის,
არ გამოიგონო ინფორმაცია.

თუ მომხმარებელი კითხულობს:
„რა გახსოვს?“ ან მსგავს რამეს,
გამოიყენე ზემოთ მოცემული CONFIRMED FACTS
და SAVED MEMORIES.
"""

    return gemini_generate(prompt)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_call(method, params=None):

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )

    response = requests.post(
        url,
        json=params or {},
        timeout=TELEGRAM_TIMEOUT
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

    return data.get("result")


def telegram_send_message(chat_id, text):

    text = str(text or "").strip()

    if not text:
        return

    # Telegram message limit protection.
    chunks = []

    while len(text) > MAX_MESSAGE_LENGTH:

        split_at = text.rfind(
            "\n",
            0,
            MAX_MESSAGE_LENGTH
        )

        if split_at < 500:
            split_at = MAX_MESSAGE_LENGTH

        chunks.append(
            text[:split_at]
        )

        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    for chunk in chunks:

        telegram_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            }
        )


def telegram_clear_webhook():

    try:

        telegram_call(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            }
        )

        log("Telegram webhook cleared")

    except Exception as e:

        log(
            f"Telegram webhook warning: {repr(e)}"
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def handle_command(chat_id, text):

    command = text.strip().split()[0].lower()

    if command == "/start":

        telegram_send_message(
            chat_id,
            """
გამარჯობა. მე ვარ GENIOSA 5.0 — შენი პერსონალური
ბიზნეს-მრჩეველი და ასისტენტი.

მე შემიძლია დაგეხმარო:
• ბიზნესისა და საინვესტიციო ანალიზში
• სამშენებლო/დეველოპერულ პროექტებში
• ფინანსურ მოდელებში
• პროექტების მართვაში
• დოკუმენტების/იდეების სტრუქტურირებაში
• შენახული ფაქტებისა და მეხსიერების მართვაში

მომწერე ჩვეულებრივი შეტყობინება და დავიწყოთ.
"""
        )

        return True

    if command == "/help":

        telegram_send_message(
            chat_id,
            """
GENIOSA 5.0 ბრძანებები:

/health — სისტემის სტატუსი
/facts — დადასტურებული ფაქტები
/memory — შენახული მეხსიერება
/projects — პროექტების სია
/tasks — დავალებები
/summary — მოკლე შეჯამება

მეხსიერებისთვის:
„დაიმახსოვრე, რომ ...“
"""
        )

        return True

    if command == "/health":

        status = config_status()

        db_ok = False

        try:
            db_execute(
                "SELECT 1",
                fetchone=True
            )
            db_ok = True
        except Exception:
            db_ok = False

        telegram_send_message(
            chat_id,
            "\n".join([
                "GENIOSA 5.0",
                "",
                f"Telegram: {'OK' if TELEGRAM_BOT_TOKEN else 'NOT CONFIGURED'}",
                f"Gemini: {'CONFIGURED' if status['gemini'] else 'NOT CONFIGURED'}",
                f"Database: {'READY' if db_ok else 'ERROR'}",
                f"Model: {GEMINI_MODEL}",
                "Polling: RUNNING",
            ])
        )

        return True

    if command == "/facts":

        seed_system_facts(chat_id)

        facts = get_facts(
            chat_id,
            limit=100
        )

        telegram_send_message(
            chat_id,
            "📋 დადასტურებული ფაქტები:\n\n"
            + format_facts(facts)
        )

        return True

    if command == "/memory":

        seed_system_facts(chat_id)

        memories = get_memories(
            chat_id,
            limit=50
        )

        telegram_send_message(
            chat_id,
            "🧠 შენახული მეხსიერება:\n\n"
            + format_memories(memories)
        )

        return True

    if command == "/projects":

        try:

            rows = db_execute(
                """
                SELECT name, description, status
                FROM projects
                WHERE chat_id=%s
                ORDER BY id DESC
                LIMIT 30
                """,
                (chat_id,),
                fetch=True
            ) or []

            if not rows:
                telegram_send_message(
                    chat_id,
                    "პროექტების ცალკე ჩანაწერები ჯერ არ მოიძებნა.\n\n"
                    "თუმცა პროექტების დადასტურებული ფაქტები "
                    "ხელმისაწვდომია /facts-ში."
                )

            else:

                lines = []

                for row in rows:

                    lines.append(
                        f"• {row.get('name') or 'Unnamed'}"
                        f" — {row.get('status') or 'active'}"
                    )

                    if row.get("description"):
                        lines.append(
                            f"  {row['description']}"
                        )

                telegram_send_message(
                    chat_id,
                    "📁 პროექტები:\n\n"
                    + "\n".join(lines)
                )

        except Exception as e:

            log(
                f"PROJECTS ERROR: {repr(e)}"
            )

            telegram_send_message(
                chat_id,
                "პროექტების მონაცემების წაკითხვა ვერ მოხერხდა."
            )

        return True

    if command == "/tasks":

        try:

            rows = db_execute(
                """
                SELECT id, title, description, status, priority
                FROM tasks
                WHERE chat_id=%s
                ORDER BY id DESC
                LIMIT 30
                """,
                (chat_id,),
                fetch=True
            ) or []

            if not rows:

                telegram_send_message(
                    chat_id,
                    "დავალებები ჯერ არ არის შენახული."
                )

            else:

                lines = []

                for row in rows:

                    lines.append(
                        f"#{row['id']} "
                        f"{row.get('title') or 'Untitled'} "
                        f"[{row.get('status') or 'open'}]"
                    )

                telegram_send_message(
                    chat_id,
                    "📝 დავალებები:\n\n"
                    + "\n".join(lines)
                )

        except Exception as e:

            log(
                f"TASKS ERROR: {repr(e)}"
            )

            telegram_send_message(
                chat_id,
                "დავალებების წაკითხვა ვერ მოხერხდა."
            )

        return True

    if command == "/summary":

        answer = answer_user(
            chat_id,
            "მომეცი ჩემი ბიზნესის, კომპანიისა და პროექტების მოკლე მიმდინარე შეჯამება მხოლოდ შენახული დადასტურებული ინფორმაციის მიხედვით."
        )

        telegram_send_message(
            chat_id,
            answer
        )

        return True

    return False


# ============================================================
# UPDATE PROCESSING
# ============================================================

def process_update(update):

    try:

        message = update.get("message")

        if not message:
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        if chat_id is None:
            return

        user = message.get("from") or {}

        text = message.get("text")

        if not text:
            return

        text = str(text).strip()

        log(
            f"USER MESSAGE chat={chat_id}: {text[:500]}"
        )

        # Make sure system knowledge exists.
        seed_system_facts(chat_id)

        # Commands.
        if text.startswith("/"):

            if handle_command(
                chat_id,
                text
            ):
                return

        # Save user message.
        save_message(
            chat_id,
            "user",
            text
        )

        # Explicit memory.
        capture_explicit_memory(
            chat_id,
            text
        )

        # Lightweight direct fact capture.
        capture_direct_company_fact(
            chat_id,
            text
        )

        # Generate one answer.
        answer = answer_user(
            chat_id,
            text
        )

        # Save assistant answer.
        save_message(
            chat_id,
            "assistant",
            answer
        )

        telegram_send_message(
            chat_id,
            answer
        )

        log(
            f"ANSWER SENT chat={chat_id}"
        )

    except Exception as e:

        log(
            f"PROCESS UPDATE ERROR: {repr(e)}"
        )

        traceback.print_exc()

        try:

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")

            if chat_id:

                telegram_send_message(
                    chat_id,
                    "დამუშვებისას ტექნიკური შეცდომა მოხდა. "
                    "გთხოვ იგივე შეტყობინება კიდევ ერთხელ გამომიგზავნე."
                )

        except Exception:
            pass


# ============================================================
# POSTGRESQL POLLING LOCK
# ============================================================

POLLING_LOCK_KEY = 735050


def acquire_polling_lock():

    conn = None

    try:

        conn = db_connection()

        cur = conn.cursor()

        cur.execute(
            "SELECT pg_try_advisory_lock(%s)",
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

            # IMPORTANT:
            # Keep this connection open.
            return conn

        cur.close()
        release_connection(conn)

        log(
            "ANOTHER GENIOSA INSTANCE OWNS "
            "THE POLLING LOCK"
        )

        return None

    except Exception as e:

        log(
            f"POLLING LOCK ERROR: {repr(e)}"
        )

        if conn:
            release_connection(conn)

        return None


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():

    log("GENIOSA 5.0 polling loop starting...")

    if not TELEGRAM_BOT_TOKEN:

        log(
            "POLLING DISABLED: TELEGRAM_BOT_TOKEN missing"
        )

        return

    # Clear webhook before polling.
    telegram_clear_webhook()

    lock_connection = None

    # Wait until DB lock is available.
    while lock_connection is None:

        lock_connection = acquire_polling_lock()

        if lock_connection is None:
            time.sleep(10)

    offset = 0

    while True:

        try:

            updates = telegram_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": [
                        "message"
                    ]
                }
            )

            for update in updates or []:

                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:
                    offset = update_id + 1

                process_update(update)

        except Exception as e:

            message = str(e)

            # Telegram 409 should never kill the application.
            if "409" in message:

                log(
                    "TELEGRAM 409 CONFLICT — "
                    "waiting before retry"
                )

                time.sleep(10)

                continue

            log(
                f"POLLING LOOP ERROR: {repr(e)}"
            )

            time.sleep(5)


# ============================================================
# STARTUP
# ============================================================

def startup_worker():

    try:

        log(
            f"GENIOSA {APP_VERSION} STARTUP"
        )

        log(
            f"CONFIG: {config_status()}"
        )

        migrate_database()

        # Seed all known existing chat IDs.
        try:

            rows = db_execute(
                """
                SELECT DISTINCT chat_id
                FROM messages
                WHERE chat_id IS NOT NULL
                ORDER BY chat_id
                """,
                fetch=True
            ) or []

            chat_ids = [
                int(row["chat_id"])
                for row in rows
            ]

            log(
                f"KNOWN CHAT IDS: {chat_ids}"
            )

            for chat_id in chat_ids:
                seed_system_facts(chat_id)

        except Exception as e:

            log(
                f"EXISTING CHAT SEED WARNING: {repr(e)}"
            )

        thread = threading.Thread(
            target=telegram_polling,
            daemon=True,
            name="geniosa-telegram-polling"
        )

        thread.start()

        log(
            "Telegram polling thread started."
        )

        log(
            f"GENIOSA {APP_VERSION} STARTUP COMPLETE"
        )

    except Exception as e:

        log(
            f"STARTUP ERROR: {repr(e)}"
        )

        traceback.print_exc()


@app.on_event("startup")
def on_startup():

    thread = threading.Thread(
        target=startup_worker,
        daemon=True,
        name="geniosa-startup"
    )

    thread.start()


# ============================================================
# HTTP ENDPOINTS
# ============================================================

@app.get("/")
def root():

    return {
        "name": "GENIOSA",
        "version": APP_VERSION,
        "status": "online",
        "telegram": bool(TELEGRAM_BOT_TOKEN),
        "gemini": bool(GEMINI_API_KEY),
        "database": bool(DATABASE_URL),
        "model": GEMINI_MODEL
    }


@app.head("/")
def root_head():

    return


@app.get("/health")
def health():

    database = False

    try:

        db_execute(
            "SELECT 1",
            fetchone=True
        )

        database = True

    except Exception:
        database = False

    return {
        "name": "GENIOSA",
        "version": APP_VERSION,
        "telegram": bool(TELEGRAM_BOT_TOKEN),
        "gemini": bool(GEMINI_API_KEY),
        "database": database,
        "model": GEMINI_MODEL,
        "polling": True,
        "startup": True
    }


# ============================================================
# MAIN
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
