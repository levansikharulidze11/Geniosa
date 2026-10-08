# ============================================================
# GENIOSA 4.8
# Personal Business Advisor
# Telegram + Gemini + PostgreSQL
#
# Persistent:
#   - Confirmed Facts
#   - Memories
#   - Projects
#   - Decisions
#   - Tasks
#   - Conversation history
#
# IMPORTANT:
#   System facts are seeded automatically on startup.
#   User facts are persisted in PostgreSQL.
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


# ============================================================
# CONFIG
# ============================================================

APP_VERSION = "4.8"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
).strip()

OWNER_ID = os.getenv(
    "GENIOSA_OWNER_ID",
    ""
).strip()

TELEGRAM_TIMEOUT = 30
POLL_SLEEP = 2

app = FastAPI(title=f"GENIOSA {APP_VERSION}")


# ============================================================
# BASIC LOGGING
# ============================================================

def log(message):
    print(
        f"[GENIOSA {datetime.now(timezone.utc).isoformat()}] "
        f"{message}",
        flush=True
    )


# ============================================================
# ENV CHECK
# ============================================================

def configuration_status():
    return {
        "telegram": bool(TELEGRAM_BOT_TOKEN),
        "gemini": bool(GEMINI_API_KEY),
        "database": bool(DATABASE_URL),
        "model": GEMINI_MODEL,
    }


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    conn = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15
    )

    return conn


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    conn = None

    try:
        conn = get_db()

        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    message TEXT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Legacy compatibility:
            # older version used "text"
            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS message TEXT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS text TEXT
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    content TEXT NOT NULL,
                    memory_type TEXT DEFAULT 'general',
                    importance INTEGER DEFAULT 5,
                    source TEXT DEFAULT 'user',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS memory_events (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    source TEXT DEFAULT 'user',
                    category TEXT DEFAULT 'general',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS fact_history (
                    id SERIAL PRIMARY KEY,
                    fact_id INTEGER,
                    old_status TEXT,
                    new_status TEXT,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    decision TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT DEFAULT 'OPEN',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS research (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    query TEXT,
                    result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS financial_models (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_projects (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_files (
                    id SERIAL PRIMARY KEY,
                    bot_project_id INTEGER,
                    filename TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Indexes
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_chat
                ON messages(chat_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_chat
                ON memories(chat_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_chat_status
                ON facts(chat_id, status)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_chat
                ON tasks(chat_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_projects_chat
                ON projects(chat_id)
            """)

        conn.commit()

        log("DATABASE INITIALIZED")

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        log(f"DATABASE INIT ERROR: {repr(e)}")
        raise

    finally:
        if conn:
            conn.close()


# ============================================================
# SAFE DB HELPERS
# ============================================================

def db_execute(query, params=None, fetch=False, fetchone=False):
    conn = None

    try:
        conn = get_db()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())

            result = None

            if fetch:
                result = cur.fetchall()

            elif fetchone:
                result = cur.fetchone()

        conn.commit()

        return result

    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        raise

    finally:
        if conn:
            conn.close()


# ============================================================
# MESSAGE STORAGE
# ============================================================

def save_message(chat_id, role, message):
    if not message:
        return

    try:
        db_execute(
            """
            INSERT INTO messages
                (chat_id, role, message, text)
            VALUES
                (%s, %s, %s, %s)
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


def get_recent_messages(chat_id, limit=20):
    try:
        rows = db_execute(
            """
            SELECT
                role,
                COALESCE(message, text) AS content,
                created_at
            FROM messages
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (chat_id, limit),
            fetch=True
        )

        return list(reversed(rows or []))

    except Exception as e:
        log(f"get_recent_messages ERROR: {repr(e)}")
        return []


# ============================================================
# MEMORY
# ============================================================

def save_memory(
    chat_id,
    content,
    memory_type="general",
    importance=5,
    source="user"
):
    if not content:
        return False

    try:
        existing = db_execute(
            """
            SELECT id
            FROM memories
            WHERE chat_id = %s
              AND LOWER(content) = LOWER(%s)
            LIMIT 1
            """,
            (chat_id, content),
            fetchone=True
        )

        if existing:
            return True

        db_execute(
            """
            INSERT INTO memories
                (
                    chat_id,
                    content,
                    memory_type,
                    importance,
                    source
                )
            VALUES
                (%s, %s, %s, %s, %s)
            """,
            (
                chat_id,
                content,
                memory_type,
                importance,
                source
            )
        )

        db_execute(
            """
            INSERT INTO memory_events
                (
                    chat_id,
                    event_type,
                    content
                )
            VALUES
                (%s, %s, %s)
            """,
            (
                chat_id,
                "MEMORY_SAVED",
                content
            )
        )

        log(
            f"MEMORY SAVED chat={chat_id}: "
            f"{content[:150]}"
        )

        return True

    except Exception as e:
        log(f"save_memory ERROR: {repr(e)}")
        return False


def get_memories(chat_id):
    try:
        return db_execute(
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
            ORDER BY importance DESC, id DESC
            """,
            (chat_id,),
            fetch=True
        ) or []

    except Exception as e:
        log(f"get_memories ERROR: {repr(e)}")
        return []


# ============================================================
# FACT ENGINE
# ============================================================

CONFIRMED = "CONFIRMED"
PENDING = "PENDING"
CONFLICT = "CONFLICT"
REJECTED = "REJECTED"


def create_fact(
    chat_id,
    content,
    status=PENDING,
    source="user",
    category="general"
):
    if not content:
        return None

    try:
        existing = db_execute(
            """
            SELECT id, status
            FROM facts
            WHERE chat_id = %s
              AND LOWER(content) = LOWER(%s)
            LIMIT 1
            """,
            (
                chat_id,
                content
            ),
            fetchone=True
        )

        if existing:
            if status == CONFIRMED and existing["status"] != CONFIRMED:
                db_execute(
                    """
                    UPDATE facts
                    SET
                        status = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        CONFIRMED,
                        existing["id"]
                    )
                )

            return existing["id"]

        row = db_execute(
            """
            INSERT INTO facts
                (
                    chat_id,
                    content,
                    status,
                    source,
                    category
                )
            VALUES
                (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                chat_id,
                content,
                status,
                source,
                category
            ),
            fetchone=True
        )

        fact_id = row["id"] if row else None

        log(
            f"FACT CREATED "
            f"chat={chat_id} "
            f"status={status}: "
            f"{content[:150]}"
        )

        return fact_id

    except Exception as e:
        log(f"create_fact ERROR: {repr(e)}")
        return None


def get_facts(chat_id, status=None):
    try:

        if status:
            return db_execute(
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
                ORDER BY id ASC
                """,
                (
                    chat_id,
                    status
                ),
                fetch=True
            ) or []

        return db_execute(
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
            ORDER BY id ASC
            """,
            (chat_id,),
            fetch=True
        ) or []

    except Exception as e:
        log(f"get_facts ERROR: {repr(e)}")
        return []


def confirm_fact(chat_id, fact_id):
    try:
        row = db_execute(
            """
            SELECT status
            FROM facts
            WHERE id = %s
              AND chat_id = %s
            """,
            (
                fact_id,
                chat_id
            ),
            fetchone=True
        )

        if not row:
            return False

        old_status = row["status"]

        db_execute(
            """
            UPDATE facts
            SET
                status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND chat_id = %s
            """,
            (
                CONFIRMED,
                fact_id,
                chat_id
            )
        )

        db_execute(
            """
            INSERT INTO fact_history
                (
                    fact_id,
                    old_status,
                    new_status,
                    reason
                )
            VALUES
                (%s, %s, %s, %s)
            """,
            (
                fact_id,
                old_status,
                CONFIRMED,
                "Confirmed by user"
            )
        )

        return True

    except Exception as e:
        log(f"confirm_fact ERROR: {repr(e)}")
        return False


def reject_fact(chat_id, fact_id):
    try:
        row = db_execute(
            """
            SELECT status
            FROM facts
            WHERE id = %s
              AND chat_id = %s
            """,
            (
                fact_id,
                chat_id
            ),
            fetchone=True
        )

        if not row:
            return False

        old_status = row["status"]

        db_execute(
            """
            UPDATE facts
            SET
                status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND chat_id = %s
            """,
            (
                REJECTED,
                fact_id,
                chat_id
            )
        )

        db_execute(
            """
            INSERT INTO fact_history
                (
                    fact_id,
                    old_status,
                    new_status,
                    reason
                )
            VALUES
                (%s, %s, %s, %s)
            """,
            (
                fact_id,
                old_status,
                REJECTED,
                "Rejected by user"
            )
        )

        return True

    except Exception as e:
        log(f"reject_fact ERROR: {repr(e)}")
        return False


# ============================================================
# FACT DETECTION
# ============================================================

def is_confirmed_fact_request(text):
    if not text:
        return False

    lowered = text.lower()

    phrases = [
        "confirmed fact",
        "confirmed facts",
        "დადასტურებული ფაქტი",
        "დადასტურებულ ფაქტად",
        "დაიმახსოვრე როგორც დადასტურებული ფაქტი",
        "დაიმახსოვრე როგორც confirmed fact",
        "ჩაიწერე როგორც დადასტურებული ფაქტი",
        "შეინახე როგორც დადასტურებული ფაქტი",
    ]

    return any(
        phrase in lowered
        for phrase in phrases
    )


def capture_direct_company_fact(chat_id, text):
    if not text:
        return False

    patterns = [
        r"ჩემი კომპანიის სახელია\s+(.+?)(?:\.|$)",
        r"ჩვენი კომპანიის სახელია\s+(.+?)(?:\.|$)",
        r"ჩემი კომპანიაა\s+(.+?)(?:\.|$)",
        r"ჩვენი კომპანიაა\s+(.+?)(?:\.|$)",
        r"my company is\s+(.+?)(?:\.|$)",
        r"our company is\s+(.+?)(?:\.|$)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            company_name = match.group(1).strip()

            if company_name:

                status = (
                    CONFIRMED
                    if is_confirmed_fact_request(text)
                    else PENDING
                )

                create_fact(
                    chat_id,
                    f"კომპანიის სახელი: {company_name}",
                    status=status,
                    source="user",
                    category="company"
                )

                save_memory(
                    chat_id,
                    f"კომპანიის სახელი: {company_name}",
                    memory_type="company",
                    importance=10,
                    source="user"
                )

                return True

    return False


# ============================================================
# GEMINI FACT EXTRACTION
# ============================================================

def gemini_generate(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent"
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
            "maxOutputTokens": 2048
        }
    }

    response = requests.post(
        url,
        params={
            "key": GEMINI_API_KEY
        },
        json=payload,
        timeout=60
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    data = response.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise RuntimeError(
            f"Unexpected Gemini response: {data}"
        )


def extract_facts_from_user_text(text):
    if not text:
        return []

    prompt = f"""
You are a fact extraction engine.

Analyze the user's message.

Extract ONLY explicit information that the user states
as a fact about:
- company
- project
- land
- building
- area
- price
- cost
- investment
- investor
- financial model
- business decision
- people/roles
- dates
- addresses
- construction
- hotel
- apartment
- commercial area
- casino
- rental
- partnership

Do NOT invent anything.

Return ONLY valid JSON.

Format:

[
  {{
    "content": "short factual statement",
    "category": "company/project/financial/construction/other",
    "status": "PENDING"
  }}
]

USER MESSAGE:
{text}
"""

    try:
        raw = gemini_generate(prompt)

        raw = raw.strip()

        if raw.startswith("```"):
            raw = re.sub(
                r"^```(?:json)?",
                "",
                raw,
                flags=re.IGNORECASE
            )

            raw = re.sub(
                r"```$",
                "",
                raw
            )

        parsed = json.loads(raw)

        if not isinstance(parsed, list):
            return []

        return parsed

    except Exception as e:
        log(
            f"FACT EXTRACTION ERROR: "
            f"{repr(e)}"
        )

        return []


# ============================================================
# MEMORY CAPTURE
# ============================================================

def capture_memory(chat_id, text):

    if not text:
        return

    confirmed_request = is_confirmed_fact_request(text)

    # Direct company fact
    capture_direct_company_fact(
        chat_id,
        text
    )

    # Explicit "remember" requests
    remember_words = [
        "დაიმახსოვრე",
        "დაიმახსოვრეთ",
        "შეინახე",
        "ჩაიწერე",
        "დამიმახსოვრე",
        "remember this",
        "remember that",
        "save this",
        "keep this in memory",
    ]

    explicit_memory = any(
        word in text.lower()
        for word in remember_words
    )

    # Gemini fact extraction
    extracted = extract_facts_from_user_text(text)

    for item in extracted:

        if not isinstance(item, dict):
            continue

        content = str(
            item.get("content", "")
        ).strip()

        if not content:
            continue

        category = str(
            item.get("category", "general")
        ).strip()

        status = str(
            item.get("status", PENDING)
        ).upper()

        if confirmed_request:
            status = CONFIRMED

        if status not in [
            CONFIRMED,
            PENDING,
            CONFLICT,
            REJECTED
        ]:
            status = PENDING

        create_fact(
            chat_id,
            content,
            status=status,
            source="user",
            category=category
        )

        if explicit_memory or status == CONFIRMED:

            save_memory(
                chat_id,
                content,
                memory_type=category,
                importance=10 if status == CONFIRMED else 7,
                source="user"
            )


# ============================================================
# SYSTEM FACTS
# ============================================================

SYSTEM_FACTS = [

    # --------------------------------------------------------
    # COMPANY
    # --------------------------------------------------------

    (
        "კომპანიის სახელი: SAMTISI CONSTRUCTION LLC",
        "company"
    ),

    (
        "SAMTISI CONSTRUCTION LLC არის Construction & Development Company.",
        "company"
    ),

    (
        "SAMTISI CONSTRUCTION LLC-ის სათავო ოფისი არის თბილისში, საქართველოში.",
        "company"
    ),

    (
        "SAMTISI CONSTRUCTION LLC-ის კომპანიის ID არის 406391202.",
        "company"
    ),

    (
        "SAMTISI CONSTRUCTION LLC დაარსდა 2022 წლის 5 დეკემბერს.",
        "company"
    ),

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    (
        "NIKKEA 12 პროექტის მდებარეობაა ქუთაისი, ნიკეას ქუჩა 12.",
        "project"
    ),

    (
        "NIKKEA 12 პროექტის მიწის ფართობია 3,070 მ².",
        "project"
    ),

    (
        "NIKKEA 12 პროექტის საერთო გასაყიდი ფართობია 10,854 მ².",
        "project"
    ),

    (
        "NIKKEA 12 პროექტის სასტუმროს ნომრების/აპარტამენტების ფართობია 7,212 მ².",
        "project"
    ),

    (
        "NIKKEA 12 პროექტის მონოლითის მოცულობა/ფართობი დაგეგმილია 21,500 მ².",
        "construction"
    ),

    (
        "NIKKEA 12 პროექტის არქიტექტურული ნაწილი შეადგენს 16,000 მ²-ს.",
        "construction"
    ),

    (
        "NIKKEA 12-ის პირველ სართულზე კომერციული ფართობია 836 მ².",
        "project"
    ),

    (
        "NIKKEA 12-ის მეორე სართულზე კომერციული ფართობია 1,006 მ².",
        "project"
    ),

    (
        "NIKKEA 12-ის მე-3-დან მე-14 სართულამდე აპარტამენტების/სასტუმროს ნომრების ფართობია 7,212 მ².",
        "project"
    ),

    (
        "NIKKEA 12-ის კონცეფცია მოიცავს hotel-type apartments-ს, casino-ს, shopping center-ს, restaurant-ს და ზედა სართულზე lounge bar-ს.",
        "project"
    ),

    (
        "NIKKEA 12-ის მიწის მესაკუთრის მოთხოვნაა 1,800 მ² აპარტამენტები + 25 საპარკინგე ადგილი + 300,000 აშშ დოლარი ნაღდი თანხა.",
        "financial"
    ),

    (
        "NIKKEA 12-ის მონოლითის მშენებლობის შეთანხმებული ღირებულებაა 170 აშშ დოლარი/მ².",
        "construction"
    ),

    (
        "NIKKEA 12-ის დარჩენილი 16,000 მ²-ის fit-out-ის სამუშაოებისთვის გამოყენებული სამუშაო დაშვება არის მაქსიმუმ 450 აშშ დოლარი/მ².",
        "financial"
    ),

    (
        "NIKKEA 12-ში აპარტამენტების საწყისი მინიმალური გასაყიდი ფასი განიხილება 1,500-1,800 აშშ დოლარი/მ².",
        "financial"
    ),

    (
        "NIKKEA 12-ში კომერციული ფართის განსახილველი ფასი არის დაახლოებით 2,500 აშშ დოლარი/მ².",
        "financial"
    ),

    (
        "NIKKEA 12-ის განვითარების JV მოდელში ინვესტორის წილი არის 80%, ხოლო SAMTISI-ის/operator-ის წილი 20%.",
        "financial"
    ),

    (
        "NIKKEA 12-ის investor contribution-ის სამუშაო მოდელი არის პროექტის საერთო ღირებულების 35% + 300,000 აშშ დოლარი.",
        "financial"
    ),

    (
        "NIKKEA 12-ის დარჩენილი დაფინანსების წყაროდ განიხილება საბანკო დაფინანსება.",
        "financial"
    ),

    (
        "NIKKEA 12-ის საბანკო დაფინანსების სამუშაო საპროცენტო განაკვეთის დაშვება არის დაახლოებით 11.5%-13.5% მაქსიმუმ.",
        "financial"
    ),

    (
        "NIKKEA 12-ის მოდელში ინვესტორის თანხა უნდა დაბრუნდეს პროექტის მოგებიდან, რის შემდეგაც ინვესტორი იღებს წმინდა მოგების 80%-ს.",
        "financial"
    ),

    (
        "NIKKEA 12-ის ცალკე property/service company განიხილება 50/50 საკუთრებით ინვესტორსა და SAMTISI-ს შორის.",
        "financial"
    ),

    (
        "NIKKEA 12-ის property/service company-ის მოდელში rental income-ის 30% განიხილება property company-ისთვის, ხოლო 70% owner-ისთვის.",
        "financial"
    ),

    (
        "NIKKEA 12-ის აპარტამენტების სავარაუდო დღიური rental rate განიხილება 150-300 აშშ დოლარის ფარგლებში.",
        "financial"
    ),

    (
        "NIKKEA 12-ში 1,006 მ² casino area არ უნდა გაიყიდოს და განიხილება მისი შენარჩუნება.",
        "project"
    ),

    (
        "NIKKEA 12-ის casino area-ის საორიენტაციო ღირებულებად განიხილებოდა მინიმუმ 2,000 აშშ დოლარი/მ².",
        "financial"
    ),

    (
        "NIKKEA 12-ის casino area-ის შესაძლო rental rate-ის სამუშაო სამიზნედ განიხილებოდა მინიმუმ 50 აშშ დოლარი/მ²/თვეში.",
        "financial"
    ),

    # --------------------------------------------------------
    # SAMGORI
    # --------------------------------------------------------

    (
        "Samgori პროექტის მდებარეობაა თბილისი, სამგორის რაიონი, გიორგი ნადერიშვილის ქუჩა.",
        "project"
    ),

    (
        "Samgori პროექტის მიწის ფართობია 7,390 მ².",
        "project"
    ),

    (
        "Samgori პროექტის საერთო აშენების ფართობია 46,131 მ².",
        "project"
    ),

    (
        "Samgori პროექტის სამშენებლო მოცულობა არის 41,874 მ³.",
        "construction"
    ),

    (
        "Samgori პროექტის გასაყიდი ფართობი პარკინგის გარეშე არის 30,540 მ².",
        "financial"
    ),

    (
        "Samgori პროექტში პარკინგის ფართობია 6,516 მ².",
        "project"
    ),

    (
        "Samgori პროექტის residential ფართობია 22,143 მ².",
        "project"
    ),

    (
        "Samgori პროექტის summer area არის 5,040 მ².",
        "project"
    ),

    (
        "Samgori პროექტის commercial ფართობია 1,647 მ².",
        "project"
    ),

    (
        "Samgori პროექტის office ფართობია 1,710 მ².",
        "project"
    ),

    (
        "Samgori პროექტის construction cost-ის სამუშაო მაჩვენებელია დაახლოებით 253 აშშ დოლარი/მ² გასაყიდ ფართობზე.",
        "financial"
    ),

    (
        "Samgori პროექტში მიწის მესაკუთრის მოთხოვნა არის დაახლოებით 7,750,000 აშშ დოლარი.",
        "financial"
    ),

    (
        "Samgori პროექტის მშენებლობისთვის განსახილველი საჭირო თანხაა დაახლოებით 15,000,000 აშშ დოლარი.",
        "financial"
    ),

    (
        "Samgori პროექტის საერთო კაპიტალის მოთხოვნა სამუშაო მოდელში დაახლოებით 23,500,000 აშშ დოლარია.",
        "financial"
    ),

    (
        "Samgori პროექტის მოსალოდნელი წმინდა მოგება სამუშაო მოდელში დაახლოებით 10,000,000 აშშ დოლარია.",
        "financial"
    ),

    # --------------------------------------------------------
    # GOLDEN LAKE / OQRI
    # --------------------------------------------------------

    (
        "Golden Lake/Oqri კონცეფციის ძირითადი მიწის ფართობი დაახლოებით 46 ჰექტარია.",
        "project"
    ),

    (
        "Golden Lake/Oqri-ის ტბისა და მიმდებარე ტერიტორიის საერთო კონცეფციური სივრცე დაახლოებით 70-80 ჰექტარია.",
        "project"
    ),

    (
        "Golden Lake/Oqri პროექტში მიწის ღირებულების სამუშაო დიაპაზონია დაახლოებით 35-40 მილიონი აშშ დოლარი.",
        "financial"
    ),

    (
        "Golden Lake/Oqri პროექტის დაგეგმილი მშენებლობა დაახლოებით 250,000 მ²-ია.",
        "construction"
    ),

    (
        "Golden Lake/Oqri კონცეფცია მოიცავს 5-ვარსკვლავიან 200-ნომრიან სასტუმროს, aquapark-ს და casino-ს.",
        "project"
    ),

    (
        "Golden Lake/Oqri კონცეფცია მოიცავს 4-ვარსკვლავიან სასტუმროს დაახლოებით 100-120 ნომრით.",
        "project"
    ),

    (
        "Golden Lake/Oqri სპორტული არენის კონცეფციური ტევადობა დაახლოებით 10,000 სტუმარია.",
        "project"
    ),

    (
        "Golden Lake/Oqri სპორტული არენის სამუშაო ფართობი დაახლოებით 20,000-30,000 მ² განიხილება.",
        "project"
    ),

    (
        "Golden Lake/Oqri-ში apartment sale area დაახლოებით 150,000 მ²-ია.",
        "project"
    ),

    (
        "Golden Lake/Oqri-ში commercial sale area დაახლოებით 30,000 მ²-ია.",
        "project"
    ),

    (
        "Golden Lake/Oqri-ის apartment fit-out-ის სამუშაო ღირებულება დაახლოებით 1,000-1,200 აშშ დოლარი/მ² განიხილება.",
        "financial"
    ),

    (
        "Golden Lake/Oqri-ის apartment sale price-ის სამუშაო დიაპაზონია დაახლოებით 2,500-3,000 აშშ დოლარი/მ².",
        "financial"
    ),

    (
        "Golden Lake/Oqri-ის commercial sale price-ის სამუშაო დიაპაზონია დაახლოებით 3,500-5,000 აშშ დოლარი/მ².",
        "financial"
    ),
]


def seed_system_facts(chat_id):
    inserted = 0

    for content, category in SYSTEM_FACTS:

        before = db_execute(
            """
            SELECT id
            FROM facts
            WHERE chat_id = %s
              AND LOWER(content) = LOWER(%s)
            LIMIT 1
            """,
            (
                chat_id,
                content
            ),
            fetchone=True
        )

        create_fact(
            chat_id,
            content,
            status=CONFIRMED,
            source="system_seed",
            category=category
        )

        # Also ensure it exists in memories.
        save_memory(
            chat_id,
            content,
            memory_type=category,
            importance=10,
            source="system_seed"
        )

        if not before:
            inserted += 1

    log(
        f"SYSTEM FACT SEED COMPLETE "
        f"chat={chat_id}, new={inserted}"
    )

    return inserted


# ============================================================
# CHAT ID DISCOVERY
# ============================================================

def get_known_chat_ids():
    ids = set()

    try:
        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM messages
            """,
            fetch=True
        ) or []

        for row in rows:
            ids.add(int(row["chat_id"]))

    except Exception as e:
        log(
            f"get_known_chat_ids messages ERROR: "
            f"{repr(e)}"
        )

    try:
        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM facts
            """,
            fetch=True
        ) or []

        for row in rows:
            ids.add(int(row["chat_id"]))

    except Exception as e:
        log(
            f"get_known_chat_ids facts ERROR: "
            f"{repr(e)}"
        )

    try:
        rows = db_execute(
            """
            SELECT DISTINCT chat_id
            FROM memories
            """,
            fetch=True
        ) or []

        for row in rows:
            ids.add(int(row["chat_id"]))

    except Exception as e:
        log(
            f"get_known_chat_ids memories ERROR: "
            f"{repr(e)}"
        )

    return ids


# ============================================================
# CONTEXT
# ============================================================

def format_context(chat_id):
    sections = []

    confirmed = get_facts(
        chat_id,
        CONFIRMED
    )

    memories = get_memories(
        chat_id
    )

    pending = get_facts(
        chat_id,
        PENDING
    )

    conflicts = get_facts(
        chat_id,
        CONFLICT
    )

    if confirmed:

        lines = [
            "CONFIRMED FACTS:"
        ]

        for item in confirmed:
            lines.append(
                f"- {item['content']}"
            )

        sections.append(
            "\n".join(lines)
        )

    if memories:

        lines = [
            "PERSISTENT MEMORIES:"
        ]

        for item in memories[:100]:
            lines.append(
                f"- {item['content']}"
            )

        sections.append(
            "\n".join(lines)
        )

    if pending:

        lines = [
            "PENDING / UNCONFIRMED:"
        ]

        for item in pending[:50]:
            lines.append(
                f"- {item['content']}"
            )

        sections.append(
            "\n".join(lines)
        )

    if conflicts:

        lines = [
            "CONFLICTING FACTS:"
        ]

        for item in conflicts[:50]:
            lines.append(
                f"- {item['content']}"
            )

        sections.append(
            "\n".join(lines)
        )

    recent = get_recent_messages(
        chat_id,
        limit=20
    )

    if recent:

        lines = [
            "RECENT CONVERSATION:"
        ]

        for item in recent:

            role = item["role"]
            content = item["content"]

            if not content:
                continue

            lines.append(
                f"{role}: {content}"
            )

        sections.append(
            "\n".join(lines)
        )

    return "\n\n".join(sections)


# ============================================================
# GENIOSA SYSTEM PROMPT
# ============================================================

def build_system_prompt(chat_id):

    context = format_context(
        chat_id
    )

    return f"""
You are GENIOSA 4.8.

You are the user's personal business advisor,
economic analyst, project-development assistant,
research assistant and technical assistant.

CORE RULES:

1. Never invent facts.

2. Clearly distinguish:
   - CONFIRMED FACT
   - USER MEMORY
   - ASSUMPTION
   - ESTIMATE
   - OPINION
   - UNKNOWN

3. Confirmed facts stored in the database have priority.

4. Never say that information is missing if it is present
   in the CONFIRMED FACTS or PERSISTENT MEMORIES below.

5. Never ask the user to repeat information that is already
   present in this context.

6. If two stored facts conflict, explicitly identify the conflict.

7. Do not silently replace an old confirmed fact.

8. Financial numbers must be treated carefully.
   If a number is only an assumption, call it an assumption.

9. When current information is needed, say that current
   web research is required. Do not pretend to have browsed
   if browsing was not actually performed.

10. For financial calculations, show the calculation logic.

11. For construction projects, separate:
    land cost,
    construction cost,
    financing cost,
    sales revenue,
    operating revenue,
    taxes,
    contingency,
    gross profit,
    net profit.

12. The user's company is SAMTISI CONSTRUCTION LLC.

13. The user wants practical answers, not generic motivational text.

14. If you don't know something, say:
    "ეს ინფორმაცია დადასტურებულად არ მაქვს."

15. Never claim that the database was cleared unless a real
    database deletion operation happened.

16. Memory is persistent and stored in PostgreSQL.

CURRENT DATABASE CONTEXT:

{context}
"""


# ============================================================
# GEMINI BUSINESS ANSWER
# ============================================================

def generate_answer(chat_id, user_text):

    system_prompt = build_system_prompt(
        chat_id
    )

    prompt = f"""
{system_prompt}

USER QUESTION:
{user_text}

Answer in Georgian unless the user asks for another language.
Be practical and concise, but include important numbers and
reasoning when needed.
"""

    try:
        return gemini_generate(
            prompt
        )

    except Exception as e:

        log(
            f"GEMINI ANSWER ERROR: "
            f"{repr(e)}"
        )

        return (
            "⚠️ Geniosa-ს Gemini AI-სთან დაკავშირებისას "
            "ტექნიკური შეცდომა მოხდა.\n\n"
            f"ტექნიკური დეტალი: {str(e)[:500]}"
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


def send_message(chat_id, text):

    if not text:
        return

    # Telegram message limit safety
    chunks = []

    while len(text) > 3900:
        split_at = text.rfind(
            "\n",
            0,
            3900
        )

        if split_at < 500:
            split_at = 3900

        chunks.append(
            text[:split_at]
        )

        text = text[split_at:]

    chunks.append(text)

    for chunk in chunks:

        telegram_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            },
            timeout=40
        )


# ============================================================
# COMMANDS
# ============================================================

def memory_report(chat_id):

    confirmed = get_facts(
        chat_id,
        CONFIRMED
    )

    memories = get_memories(
        chat_id
    )

    pending = get_facts(
        chat_id,
        PENDING
    )

    conflicts = get_facts(
        chat_id,
        CONFLICT
    )

    lines = [
        "🧠 GENIOSA MEMORY",
        ""
    ]

    if confirmed:

        lines.append(
            "📋 CONFIRMED FACTS"
        )

        for index, item in enumerate(
            confirmed,
            start=1
        ):
            lines.append(
                f"{index}. {item['content']}"
            )

        lines.append("")

    else:

        lines.append(
            "📋 CONFIRMED FACTS"
        )

        lines.append(
            "ცარიელია."
        )

        lines.append("")

    if memories:

        lines.append(
            "💾 PERSISTENT MEMORIES"
        )

        shown = set()

        for item in memories:

            content = item["content"]

            if content in shown:
                continue

            shown.add(content)

            lines.append(
                f"- {content}"
            )

        lines.append("")

    if pending:

        lines.append(
            "🟡 PENDING"
        )

        for item in pending[:30]:
            lines.append(
                f"- [{item['id']}] "
                f"{item['content']}"
            )

        lines.append("")

    if conflicts:

        lines.append(
            "🔴 CONFLICTS"
        )

        for item in conflicts[:30]:
            lines.append(
                f"- [{item['id']}] "
                f"{item['content']}"
            )

        lines.append("")

    total = (
        len(confirmed)
        + len(memories)
        + len(pending)
        + len(conflicts)
    )

    lines.append(
        f"📊 ჩანაწერები: {total}"
    )

    return "\n".join(lines)


def help_text():

    return """
🤖 GENIOSA 4.8

ძირითადი ბრძანებები:

/memory
მეხსიერება და დადასტურებული ფაქტები

/facts
ყველა ფაქტის სია

/tasks
დავალებები

/projects
პროექტები

/health
სისტემის მდგომარეობა

/help
ბრძანებების სია

ფაქტის დასამატებლად შეგიძლია დაწერო:

„ჩემი კომპანიის სახელია ... დაიმახსოვრე როგორც CONFIRMED FACT.“

ან ჩვეულებრივად მიაწოდო ინფორმაცია და Geniosa
შეეცდება გამოარჩიოს ფაქტი.

Geniosa-მ არ უნდა მოიგონოს ინფორმაცია.
თუ ინფორმაცია უცნობია, უნდა თქვას, რომ უცნობია.
"""


def facts_report(chat_id):

    facts = get_facts(
        chat_id
    )

    if not facts:
        return (
            "📋 FACTS\n\n"
            "ფაქტები ცარიელია."
        )

    lines = [
        "📋 GENIOSA FACTS",
        ""
    ]

    for item in facts:

        status = item["status"]

        if status == CONFIRMED:
            icon = "✅"

        elif status == PENDING:
            icon = "🟡"

        elif status == CONFLICT:
            icon = "🔴"

        else:
            icon = "⚪"

        lines.append(
            f"{icon} [{item['id']}] "
            f"{status}: "
            f"{item['content']}"
        )

    return "\n".join(lines)


def tasks_report(chat_id):

    rows = db_execute(
        """
        SELECT
            id,
            task,
            status,
            created_at
        FROM tasks
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT 100
        """,
        (chat_id,),
        fetch=True
    ) or []

    if not rows:
        return (
            "📌 TASKS\n\n"
            "დავალებები არ არის."
        )

    lines = [
        "📌 GENIOSA TASKS",
        ""
    ]

    for row in rows:

        icon = (
            "✅"
            if row["status"] == "DONE"
            else "🟡"
        )

        lines.append(
            f"{icon} [{row['id']}] "
            f"{row['task']} "
            f"({row['status']})"
        )

    return "\n".join(lines)


def projects_report(chat_id):

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
        """,
        (chat_id,),
        fetch=True
    ) or []

    if not rows:
        return (
            "🏗 PROJECTS\n\n"
            "პროექტები ცალკე ჩანაწერად ჯერ არ არის."
        )

    lines = [
        "🏗 GENIOSA PROJECTS",
        ""
    ]

    for row in rows:

        lines.append(
            f"• [{row['id']}] "
            f"{row['name']} "
            f"— {row['status']}"
        )

        if row["description"]:
            lines.append(
                f"  {row['description']}"
            )

    return "\n".join(lines)


def health_report():

    status = configuration_status()

    db_ok = False

    try:

        row = db_execute(
            "SELECT 1 AS ok",
            fetchone=True
        )

        db_ok = bool(
            row and row["ok"] == 1
        )

    except Exception as e:

        log(
            f"HEALTH DB ERROR: "
            f"{repr(e)}"
        )

    telegram_status = "configured"

    if not status["telegram"]:
        telegram_status = "MISSING"

    gemini_status = "configured"

    if not status["gemini"]:
        gemini_status = "MISSING"

    return (
        f"🤖 GENIOSA {APP_VERSION}\n\n"
        f"Telegram: {telegram_status}\n"
        f"Gemini: {gemini_status}\n"
        f"Gemini model: {status['model']}\n"
        f"PostgreSQL: "
        f"{'OK' if db_ok else 'ERROR'}\n"
    )


# ============================================================
# COMMAND ROUTER
# ============================================================

def handle_command(chat_id, text):

    command = text.strip().split()[0].lower()

    if command == "/start":
        return (
            "🤖 გამარჯობა! მე ვარ Geniosa 4.8.\n\n"
            "მე ვინახავ დადასტურებულ ფაქტებს, "
            "მეხსიერებას და პროექტის ინფორმაციას "
            "PostgreSQL-ში.\n\n"
            "გამოიყენე /memory ან /help."
        )

    if command == "/help":
        return help_text()

    if command == "/memory":
        return memory_report(chat_id)

    if command == "/facts":
        return facts_report(chat_id)

    if command == "/tasks":
        return tasks_report(chat_id)

    if command == "/projects":
        return projects_report(chat_id)

    if command == "/health":
        return health_report()

    if command == "/confirm_fact":

        parts = text.strip().split()

        if len(parts) < 2:
            return (
                "გამოყენება:\n"
                "/confirm_fact FACT_ID"
            )

        try:
            fact_id = int(parts[1])
        except ValueError:
            return "FACT_ID უნდა იყოს რიცხვი."

        ok = confirm_fact(
            chat_id,
            fact_id
        )

        if ok:
            return (
                f"✅ Fact #{fact_id} "
                f"დადასტურებულია."
            )

        return (
            f"❌ Fact #{fact_id} ვერ მოიძებნა."
        )

    if command == "/reject_fact":

        parts = text.strip().split()

        if len(parts) < 2:
            return (
                "გამოყენება:\n"
                "/reject_fact FACT_ID"
            )

        try:
            fact_id = int(parts[1])
        except ValueError:
            return "FACT_ID უნდა იყოს რიცხვი."

        ok = reject_fact(
            chat_id,
            fact_id
        )

        if ok:
            return (
                f"⚪ Fact #{fact_id} "
                f"უარყოფილია."
            )

        return (
            f"❌ Fact #{fact_id} ვერ მოიძებნა."
        )

    return None


# ============================================================
# MESSAGE PROCESSING
# ============================================================

def process_user_message(
    chat_id,
    user_text
):

    if not user_text:
        return

    log(
        f"USER MESSAGE "
        f"chat={chat_id}: "
        f"{user_text[:200]}"
    )

    # Save incoming message FIRST
    save_message(
        chat_id,
        "user",
        user_text
    )

    # Ensure system knowledge exists
    try:
        seed_system_facts(
            chat_id
        )
    except Exception as e:
        log(
            f"SEED ERROR: "
            f"{repr(e)}"
        )

    # Capture new user facts/memories
    try:
        capture_memory(
            chat_id,
            user_text
        )
    except Exception as e:
        log(
            f"MEMORY CAPTURE ERROR: "
            f"{repr(e)}"
        )

    # Command
    if user_text.strip().startswith("/"):

        response = handle_command(
            chat_id,
            user_text
        )

        if response is not None:

            save_message(
                chat_id,
                "assistant",
                response
            )

            send_message(
                chat_id,
                response
            )

            return

    # AI response
    response = generate_answer(
        chat_id,
        user_text
    )

    save_message(
        chat_id,
        "assistant",
        response
    )

    send_message(
        chat_id,
        response
    )


# ============================================================
# TELEGRAM UPDATE HANDLING
# ============================================================

def process_update(update):

    if not isinstance(update, dict):
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

    text = message.get(
        "text"
    )

    if not chat_id or not text:
        return

    process_user_message(
        int(chat_id),
        text
    )


# ============================================================
# TELEGRAM POLLING
# ============================================================

polling_started = False
polling_lock = threading.Lock()


def telegram_polling():

    global polling_started

    with polling_lock:

        if polling_started:
            log(
                "Polling already started"
            )
            return

        polling_started = True

    log(
        "TELEGRAM POLLING STARTED"
    )

    offset = None

    while True:

        try:

            payload = {
                "timeout": TELEGRAM_TIMEOUT,
                "allowed_updates": [
                    "message"
                ]
            }

            if offset is not None:
                payload["offset"] = offset

            data = telegram_call(
                "getUpdates",
                payload,
                timeout=TELEGRAM_TIMEOUT + 10
            )

            updates = data.get(
                "result",
                []
            )

            for update in updates:

                try:

                    update_id = update.get(
                        "update_id"
                    )

                    if update_id is not None:
                        offset = (
                            update_id + 1
                        )

                    process_update(
                        update
                    )

                except Exception as e:

                    log(
                        "UPDATE PROCESS ERROR: "
                        f"{repr(e)}"
                    )

                    traceback.print_exc()

            if not updates:
                time.sleep(
                    POLL_SLEEP
                )

        except Exception as e:

            log(
                "POLLING LOOP ERROR: "
                f"{repr(e)}"
            )

            traceback.print_exc()

            time.sleep(
                5
            )


# ============================================================
# STARTUP
# ============================================================

def startup_worker():

    log(
        f"GENIOSA {APP_VERSION} "
        f"STARTUP"
    )

    try:

        log(
            f"CONFIG: "
            f"{configuration_status()}"
        )

        init_db()

        log(
            "DATABASE READY"
        )

        # Existing chat IDs receive system knowledge.
        chat_ids = get_known_chat_ids()

        log(
            f"KNOWN CHAT IDS: "
            f"{list(chat_ids)}"
        )

        for chat_id in chat_ids:

            try:
                seed_system_facts(
                    chat_id
                )
            except Exception as e:
                log(
                    f"SEED EXISTING CHAT ERROR "
                    f"{chat_id}: "
                    f"{repr(e)}"
                )

        if not TELEGRAM_BOT_TOKEN:

            log(
                "STARTUP WARNING: "
                "TELEGRAM_BOT_TOKEN missing"
            )

        else:

            thread = threading.Thread(
                target=telegram_polling,
                daemon=True,
                name="telegram-polling"
            )

            thread.start()

            log(
                "TELEGRAM POLLING THREAD STARTED"
            )

    except Exception as e:

        log(
            f"STARTUP ERROR: "
            f"{repr(e)}"
        )

        traceback.print_exc()


@app.on_event("startup")
def startup():

    thread = threading.Thread(
        target=startup_worker,
        daemon=True,
        name="geniosa-startup"
    )

    thread.start()


# ============================================================
# WEB ENDPOINTS
# ============================================================

@app.get("/")
def root():

    return {
        "status": "online",
        "service": "GENIOSA",
        "version": APP_VERSION,
        "message": "Geniosa is running."
    }


@app.get("/health")
def health():

    db = False

    try:

        row = db_execute(
            "SELECT 1 AS ok",
            fetchone=True
        )

        db = bool(
            row and row["ok"] == 1
        )

    except Exception:
        db = False

    return {
        "status": "ok" if db else "degraded",
        "version": APP_VERSION,
        "database": db,
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "gemini_model": GEMINI_MODEL,
        "polling_started": polling_started
    }


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )
