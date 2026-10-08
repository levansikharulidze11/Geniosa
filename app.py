import os
import re
import ast
import json
import time
import threading
import hashlib
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI


# ============================================================
# GENIOSA 4.6
# Personal Business Advisor / Economist / Developer
# SAMTISI CONSTRUCTION
# ============================================================

APP_VERSION = "4.6"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

OWNER_ID_RAW = os.getenv("GENIOSA_OWNER_ID", "").strip()

try:
    GENIOSA_OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else None
except Exception:
    GENIOSA_OWNER_ID = None


TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN
    else ""
)

GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/"
    f"v1beta/models/{GEMINI_MODEL}:generateContent"
    f"?key={GEMINI_API_KEY}"
    if GEMINI_API_KEY
    else ""
)


app = FastAPI(title="Geniosa", version=APP_VERSION)


# ============================================================
# GLOBALS
# ============================================================

polling_connection = None
polling_started = False
polling_thread = None
polling_state_lock = threading.Lock()

HTTP_TIMEOUT = 60
GEMINI_TIMEOUT = 90
TELEGRAM_TIMEOUT = 60

MAX_TELEGRAM_MESSAGE = 3900
MAX_CONTEXT_MESSAGES = 20
MAX_MEMORIES = 50
MAX_FACTS = 100
MAX_PROJECTS = 30
MAX_TASKS = 30
MAX_DECISIONS = 30


# ============================================================
# BASIC HELPERS
# ============================================================

def now():
    return datetime.utcnow()


def safe_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "")
            value = value.replace("€", "").replace("₾", "")
            value = value.strip()

        return float(value)
    except Exception:
        return default


def normalize_percent(value):
    """
    Converts:
      80   -> 0.80
      80%  -> 0.80
      0.8  -> 0.80
    """
    value = safe_float(value, 0.0)

    if value > 1:
        return value / 100.0

    return value


def clean_text(text):
    if text is None:
        return ""

    return str(text).strip()


def truncate_text(text, limit=12000):
    text = clean_text(text)

    if len(text) <= limit:
        return text

    return text[:limit] + "\n...[truncated]"


def contains_any(text, words):
    text = (text or "").lower()

    return any(word.lower() in text for word in words)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15
    )


def db_execute(query, params=None, fetch=False, fetchone=False):
    conn = None

    try:
        conn = get_db()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())

            result = None

            if fetchone:
                result = cur.fetchone()

            elif fetch:
                result = cur.fetchall()

        conn.commit()

        return result

    except Exception:
        if conn:
            conn.rollback()
        raise

    finally:
        if conn:
            conn.close()


def init_db():
    """
    Creates required tables and performs basic migrations.

    Important:
    This intentionally does NOT delete existing data.
    """

    conn = get_db()

    try:
        with conn.cursor() as cur:

            # ------------------------------------------------
            # Messages
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    role TEXT,
                    message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Legacy schema protection.
            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS chat_id BIGINT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS role TEXT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS message TEXT
            """)

            cur.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP
            """)

            # If an older version had "text" instead of "message",
            # preserve the old content.
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name='messages'
                        AND column_name='text'
                    )
                    THEN
                        UPDATE messages
                        SET message = COALESCE(message, "text")
                        WHERE message IS NULL;
                    END IF;
                END
                $$;
            """)

            # ------------------------------------------------
            # Memories
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    memory TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Memory events
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS memory_events (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    event_type TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Decisions
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    title TEXT,
                    decision TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Tasks
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    title TEXT,
                    description TEXT,
                    status TEXT DEFAULT 'OPEN',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Projects
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    name TEXT,
                    description TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Facts
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS facts (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    fact_key TEXT,
                    fact_value TEXT,
                    status TEXT DEFAULT 'PENDING',
                    source_message_id INTEGER,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                ALTER TABLE facts
                ADD COLUMN IF NOT EXISTS source_message_id INTEGER
            """)

            cur.execute("""
                ALTER TABLE facts
                ADD COLUMN IF NOT EXISTS source TEXT
            """)

            # ------------------------------------------------
            # Fact history
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS fact_history (
                    id SERIAL PRIMARY KEY,
                    fact_id INTEGER,
                    chat_id BIGINT,
                    action TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Code projects
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS code_projects (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    name TEXT,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Code versions
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS code_versions (
                    id SERIAL PRIMARY KEY,
                    code_project_id INTEGER,
                    chat_id BIGINT,
                    version_name TEXT,
                    code TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Bot projects
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_projects (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    name TEXT,
                    description TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Bot files
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_files (
                    id SERIAL PRIMARY KEY,
                    bot_project_id INTEGER,
                    filename TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Research
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS research (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    query TEXT,
                    result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Financial models
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS financial_models (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    name TEXT,
                    data JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Financial history
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS financial_history (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    model_name TEXT,
                    result JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Indexes
            # ------------------------------------------------

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_chat_created
                ON messages(chat_id, created_at)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_chat_key
                ON facts(chat_id, fact_key)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_chat
                ON memories(chat_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_bot_projects_chat
                ON bot_projects(chat_id)
            """)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# MESSAGES
# ============================================================

def save_message(chat_id, role, message):
    message = clean_text(message)

    if not message:
        return None

    conn = None

    try:
        conn = get_db()

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages
                (chat_id, role, message, created_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING id
            """, (
                chat_id,
                role,
                message
            ))

            row = cur.fetchone()

        conn.commit()

        return row[0] if row else None

    except Exception as e:
        if conn:
            conn.rollback()

        print("save_message error:", repr(e))
        return None

    finally:
        if conn:
            conn.close()


def get_recent_messages(chat_id, limit=MAX_CONTEXT_MESSAGES):
    limit = max(1, min(int(limit), 100))

    try:
        rows = db_execute("""
            SELECT id, chat_id, role, message, created_at
            FROM messages
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True)

        return list(reversed(rows or []))

    except Exception as e:
        print("get_recent_messages error:", repr(e))
        return []


def search_messages(chat_id, query, limit=20):
    query = clean_text(query)

    if not query:
        return []

    try:
        return db_execute("""
            SELECT id, role, message, created_at
            FROM messages
            WHERE chat_id = %s
            AND message ILIKE %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            f"%{query}%",
            limit
        ), fetch=True) or []

    except Exception as e:
        print("search_messages error:", repr(e))
        return []


# ============================================================
# MEMORY
# ============================================================

def save_memory(chat_id, memory):
    memory = clean_text(memory)

    if not memory:
        return

    try:
        existing = db_execute("""
            SELECT id
            FROM memories
            WHERE chat_id = %s
            AND memory = %s
            LIMIT 1
        """, (
            chat_id,
            memory
        ), fetchone=True)

        if existing:
            return

        db_execute("""
            INSERT INTO memories
            (chat_id, memory)
            VALUES (%s, %s)
        """, (
            chat_id,
            memory
        ))

    except Exception as e:
        print("save_memory error:", repr(e))


def get_memories(chat_id, limit=MAX_MEMORIES):
    try:
        return db_execute("""
            SELECT id, memory, created_at
            FROM memories
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_memories error:", repr(e))
        return []


def search_memories(chat_id, query, limit=20):
    query = clean_text(query)

    if not query:
        return []

    try:
        return db_execute("""
            SELECT id, memory, created_at
            FROM memories
            WHERE chat_id = %s
            AND memory ILIKE %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            f"%{query}%",
            limit
        ), fetch=True) or []

    except Exception as e:
        print("search_memories error:", repr(e))
        return []


def save_memory_event(chat_id, event_type, content):
    try:
        db_execute("""
            INSERT INTO memory_events
            (chat_id, event_type, content)
            VALUES (%s, %s, %s)
        """, (
            chat_id,
            event_type,
            content
        ))
    except Exception as e:
        print("save_memory_event error:", repr(e))


def get_memory_events(chat_id, limit=30):
    try:
        return db_execute("""
            SELECT id, event_type, content, created_at
            FROM memory_events
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_memory_events error:", repr(e))
        return []


# ============================================================
# DECISIONS
# ============================================================

def save_decision(chat_id, title, decision):
    try:
        db_execute("""
            INSERT INTO decisions
            (chat_id, title, decision)
            VALUES (%s, %s, %s)
        """, (
            chat_id,
            clean_text(title),
            clean_text(decision)
        ))
    except Exception as e:
        print("save_decision error:", repr(e))


def get_decisions(chat_id, limit=MAX_DECISIONS):
    try:
        return db_execute("""
            SELECT id, title, decision, created_at
            FROM decisions
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_decisions error:", repr(e))
        return []


# ============================================================
# TASKS
# ============================================================

def save_task(chat_id, title, description=""):
    try:
        db_execute("""
            INSERT INTO tasks
            (chat_id, title, description)
            VALUES (%s, %s, %s)
        """, (
            chat_id,
            clean_text(title),
            clean_text(description)
        ))
    except Exception as e:
        print("save_task error:", repr(e))


def get_tasks(chat_id, limit=MAX_TASKS):
    try:
        return db_execute("""
            SELECT id, title, description, status, created_at
            FROM tasks
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_tasks error:", repr(e))
        return []


# ============================================================
# PROJECTS
# ============================================================

def save_project(chat_id, name, description=""):
    try:
        db_execute("""
            INSERT INTO projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
        """, (
            chat_id,
            clean_text(name),
            clean_text(description)
        ))
    except Exception as e:
        print("save_project error:", repr(e))


def get_projects(chat_id, limit=MAX_PROJECTS):
    try:
        return db_execute("""
            SELECT id, name, description, status, created_at
            FROM projects
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_projects error:", repr(e))
        return []


# ============================================================
# FACT ENGINE
# ============================================================

def normalize_fact_key(key):
    key = clean_text(key).lower()

    key = re.sub(
        r"[^a-zA-Z0-9ა-ჰ_]+",
        "_",
        key
    )

    key = re.sub(
        r"_+",
        "_",
        key
    )

    return key.strip("_")


def get_confirmed_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    try:
        return db_execute("""
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND fact_key = %s
            AND status = 'CONFIRMED'
            ORDER BY id DESC
            LIMIT 1
        """, (
            chat_id,
            fact_key
        ), fetchone=True)

    except Exception as e:
        print("get_confirmed_fact error:", repr(e))
        return None


def get_latest_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    try:
        return db_execute("""
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND fact_key = %s
            ORDER BY id DESC
            LIMIT 1
        """, (
            chat_id,
            fact_key
        ), fetchone=True)

    except Exception as e:
        print("get_latest_fact error:", repr(e))
        return None


def create_fact(
    chat_id,
    fact_key,
    fact_value,
    source_message_id=None,
    source="user"
):
    fact_key = normalize_fact_key(fact_key)
    fact_value = clean_text(fact_value)

    if not fact_key or not fact_value:
        return None

    confirmed = get_confirmed_fact(
        chat_id,
        fact_key
    )

    # --------------------------------------------------------
    # Existing confirmed fact
    # --------------------------------------------------------

    if confirmed:

        old_value = clean_text(
            confirmed.get("fact_value")
        )

        if old_value.lower() == fact_value.lower():
            return confirmed

        # Conflict candidate.
        try:
            existing_pending = db_execute("""
                SELECT *
                FROM facts
                WHERE chat_id = %s
                AND fact_key = %s
                AND fact_value = %s
                AND status = 'CONFLICT'
                ORDER BY id DESC
                LIMIT 1
            """, (
                chat_id,
                fact_key,
                fact_value
            ), fetchone=True)

            if existing_pending:
                return existing_pending

            row = db_execute("""
                INSERT INTO facts
                (
                    chat_id,
                    fact_key,
                    fact_value,
                    status,
                    source_message_id,
                    source
                )
                VALUES (%s, %s, %s, 'CONFLICT', %s, %s)
                RETURNING *
            """, (
                chat_id,
                fact_key,
                fact_value,
                source_message_id,
                source
            ), fetchone=True)

            db_execute("""
                INSERT INTO fact_history
                (
                    fact_id,
                    chat_id,
                    action,
                    old_value,
                    new_value
                )
                VALUES (%s, %s, 'CONFLICT', %s, %s)
            """, (
                row["id"],
                chat_id,
                old_value,
                fact_value
            ))

            return row

        except Exception as e:
            print("create_fact conflict error:", repr(e))
            return None

    # --------------------------------------------------------
    # Existing pending same value
    # --------------------------------------------------------

    try:
        pending = db_execute("""
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND fact_key = %s
            AND fact_value = %s
            AND status = 'PENDING'
            ORDER BY id DESC
            LIMIT 1
        """, (
            chat_id,
            fact_key,
            fact_value
        ), fetchone=True)

        if pending:
            return pending

        row = db_execute("""
            INSERT INTO facts
            (
                chat_id,
                fact_key,
                fact_value,
                status,
                source_message_id,
                source
            )
            VALUES (%s, %s, %s, 'PENDING', %s, %s)
            RETURNING *
        """, (
            chat_id,
            fact_key,
            fact_value,
            source_message_id,
            source
        ), fetchone=True)

        return row

    except Exception as e:
        print("create_fact error:", repr(e))
        return None


def get_facts(chat_id, limit=MAX_FACTS):
    try:
        return db_execute("""
            SELECT *
            FROM facts
            WHERE chat_id = %s
            ORDER BY
                CASE
                    WHEN status = 'CONFIRMED' THEN 1
                    WHEN status = 'CONFLICT' THEN 2
                    WHEN status = 'PENDING' THEN 3
                    ELSE 4
                END,
                id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_facts error:", repr(e))
        return []


def confirm_fact(chat_id, fact_id):
    conn = None

    try:
        conn = get_db()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute("""
                SELECT *
                FROM facts
                WHERE id = %s
                AND chat_id = %s
            """, (
                fact_id,
                chat_id
            ))

            fact = cur.fetchone()

            if not fact:
                conn.rollback()
                return False

            key = fact["fact_key"]
            value = fact["fact_value"]

            cur.execute("""
                SELECT *
                FROM facts
                WHERE chat_id = %s
                AND fact_key = %s
                AND status = 'CONFIRMED'
                AND id != %s
                ORDER BY id DESC
                LIMIT 1
            """, (
                chat_id,
                key,
                fact_id
            ))

            previous = cur.fetchone()

            if previous:
                cur.execute("""
                    UPDATE facts
                    SET status = 'REJECTED',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (
                    previous["id"],
                ))

            cur.execute("""
                UPDATE facts
                SET status = 'CONFIRMED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (
                fact_id,
            ))

            cur.execute("""
                INSERT INTO fact_history
                (
                    fact_id,
                    chat_id,
                    action,
                    old_value,
                    new_value
                )
                VALUES (%s, %s, 'CONFIRMED', %s, %s)
            """, (
                fact_id,
                chat_id,
                previous["fact_value"] if previous else None,
                value
            ))

        conn.commit()

        return True

    except Exception as e:
        if conn:
            conn.rollback()

        print("confirm_fact error:", repr(e))
        return False

    finally:
        if conn:
            conn.close()


def reject_fact(chat_id, fact_id):
    try:
        result = db_execute("""
            UPDATE facts
            SET status = 'REJECTED',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            AND chat_id = %s
            RETURNING id
        """, (
            fact_id,
            chat_id
        ), fetchone=True)

        if result:
            db_execute("""
                INSERT INTO fact_history
                (
                    fact_id,
                    chat_id,
                    action
                )
                VALUES (%s, %s, 'REJECTED')
            """, (
                fact_id,
                chat_id
            ))

            return True

        return False

    except Exception as e:
        print("reject_fact error:", repr(e))
        return False


def get_fact_history(chat_id, limit=50):
    try:
        return db_execute("""
            SELECT *
            FROM fact_history
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_fact_history error:", repr(e))
        return []


# ============================================================
# MEMORY DETECTION
# ============================================================

def should_save_memory(text):
    text = (text or "").lower()

    keywords = [
        "remember",
        "memorize",
        "save this",
        "keep this",
        "დაიმახსოვრე",
        "დაიმახსოვრეთ",
        "შეინახე",
        "დაიტოვე მეხსიერებაში",
        "მნიშვნელოვანია რომ იცოდე",
        "ეს ინფორმაცია დაიმახსოვრე"
    ]

    return contains_any(text, keywords)


def should_save_decision(text):
    text = (text or "").lower()

    keywords = [
        "გადავწყვიტე",
        "გადავწყვიტეთ",
        "decision",
        "decided",
        "ვწყვეტთ",
        "ჩვენი გადაწყვეტილებაა"
    ]

    return contains_any(text, keywords)


def should_save_task(text):
    text = (text or "").lower()

    keywords = [
        "დავალება",
        "task",
        "უნდა გავაკეთოთ",
        "გასაკეთებელია",
        "შემდეგ გავაკეთოთ",
        "მომავალში გავაკეთოთ",
        "to do"
    ]

    return contains_any(text, keywords)


# ============================================================
# GEMINI
# ============================================================

GENIOSA_SYSTEM = """
You are GENIOSA, the personal business advisor and intelligent assistant
of the founder/operator of SAMTISI CONSTRUCTION.

Your responsibilities include:
- business strategy
- construction and development
- investment analysis
- financial modelling
- economics
- project feasibility
- investor communication
- market research
- legal/business risk awareness
- software development
- debugging
- automation
- Telegram bot development
- project memory and decision support

CORE CONSTITUTION:

1. NEVER invent facts.
2. NEVER present an assumption as a confirmed fact.
3. Clearly distinguish:
   - CONFIRMED FACT
   - USER-PROVIDED INFORMATION
   - ESTIMATE
   - ASSUMPTION
   - CALCULATION
   - OPINION
   - RESEARCH RESULT
4. Existing CONFIRMED facts have priority.
5. If new information conflicts with a confirmed fact, do not silently replace it.
   Explicitly identify the conflict.
6. If information is missing, say what is missing.
7. Never pretend to have performed an external action if you did not actually do it.
8. Never claim that an investor, bank, lawyer, government agency or other party
   has been contacted unless that actually happened through an available tool.
9. For financial calculations show important assumptions and formulas.
10. Never hide a material risk.
11. Never fabricate market prices, laws, regulations, companies, investors,
    documents or statistics.
12. For current information, use available web research/grounding when appropriate.
13. If research was not performed, do not say that current market information
    was verified.
14. Protect API keys, passwords, tokens and private credentials.
15. When working with code, preserve working functionality unless a change is
    intentionally required.
16. Prefer complete practical solutions over vague advice.
17. If code is requested, provide production-oriented code.
18. Answer in the same language as the user unless another language is requested.
19. Be concise when a simple answer is enough.
20. For complex business/financial decisions, structure the answer clearly.

IMPORTANT:
The database context supplied with a message may contain facts with different
statuses. Only CONFIRMED facts should be treated as established facts.
PENDING and CONFLICT facts must be treated as unconfirmed.
"""


def extract_gemini_text(data):
    try:
        candidates = data.get("candidates", [])

        if not candidates:
            return ""

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        texts = []

        for part in parts:
            if "text" in part:
                texts.append(str(part["text"]))

        return "\n".join(texts).strip()

    except Exception:
        return ""


def extract_grounding_sources(data):
    sources = []

    try:
        candidates = data.get("candidates", [])

        if not candidates:
            return sources

        metadata = candidates[0].get(
            "groundingMetadata",
            {}
        )

        chunks = metadata.get(
            "groundingChunks",
            []
        )

        for chunk in chunks:
            web = chunk.get("web", {})

            uri = web.get("uri")
            title = web.get("title")

            if uri:
                sources.append({
                    "title": title or uri,
                    "uri": uri
                })

    except Exception as e:
        print("extract_grounding_sources error:", repr(e))

    return sources


def call_gemini(
    prompt,
    use_search=False,
    temperature=0.2,
    max_output_tokens=12000
):
    if not GEMINI_API_KEY:
        return {
            "ok": False,
            "text": "",
            "error": "GEMINI_API_KEY is not configured",
            "sources": []
        }

    url = GEMINI_API_URL

    if not url:
        return {
            "ok": False,
            "text": "",
            "error": "Gemini URL is not configured",
            "sources": []
        }

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": GENIOSA_SYSTEM
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": truncate_text(prompt, 50000)
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens
        }
    }

    if use_search:
        payload["tools"] = [
            {
                "google_search": {}
            }
        ]

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=GEMINI_TIMEOUT
        )

        try:
            data = response.json()
        except Exception:
            data = {}

        if response.status_code >= 400:
            error_message = (
                data.get("error", {}).get("message")
                or response.text[:1000]
                or f"HTTP {response.status_code}"
            )

            return {
                "ok": False,
                "text": "",
                "error": error_message,
                "sources": []
            }

        text = extract_gemini_text(data)

        if not text:
            return {
                "ok": False,
                "text": "",
                "error": "Gemini returned an empty response",
                "sources": extract_grounding_sources(data)
            }

        return {
            "ok": True,
            "text": text,
            "error": "",
            "sources": extract_grounding_sources(data)
        }

    except requests.RequestException as e:
        return {
            "ok": False,
            "text": "",
            "error": str(e),
            "sources": []
        }

    except Exception as e:
        return {
            "ok": False,
            "text": "",
            "error": repr(e),
            "sources": []
        }


# ============================================================
# FACT EXTRACTION
# ============================================================

def extract_json_object(text):
    text = clean_text(text)

    if not text:
        return None

    # Direct JSON
    try:
        return json.loads(text)
    except Exception:
        pass

    # Markdown JSON block
    match = re.search(
        r"```(?:json)?\s*(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    if match:
        candidate = match.group(1).strip()

        try:
            return json.loads(candidate)
        except Exception:
            pass

    # Find first {...}
    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:

        candidate = text[start:end + 1]

        try:
            return json.loads(candidate)
        except Exception:
            pass

        try:
            return ast.literal_eval(candidate)
        except Exception:
            pass

    return None


def should_extract_facts(text):
    """
    Avoid a Gemini fact-extraction call for ordinary greetings and simple
    questions.
    """

    text = (text or "").lower().strip()

    if not text:
        return False

    if len(text) < 12:
        return False

    indicators = [
        "დაიმახსოვრე",
        "შეინახე",
        "ჩემი კომპანია",
        "ჩვენი კომპანია",
        "ჩემი პროექტი",
        "ჩვენი პროექტი",
        "ჩვენ გვაქვს",
        "ჩვენი არის",
        "კომპანიის id",
        "company id",
        "remember",
        "save this",
        "our company",
        "our project",
        "my company",
        "my project",
        "is ",
        "are "
    ]

    return contains_any(text, indicators)


def extract_facts_from_user_text(
    chat_id,
    text,
    source_message_id=None
):
    if not should_extract_facts(text):
        return []

    prompt = f"""
Extract ONLY explicit factual information stated by the user.

Do NOT infer.
Do NOT calculate.
Do NOT guess.
Do NOT convert opinions into facts.

Return JSON only:

{{
  "facts": [
    {{
      "key": "short_stable_key",
      "value": "explicit value"
    }}
  ]
}}

User message:
{text}
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.0,
        max_output_tokens=2000
    )

    if not result["ok"]:
        return []

    data = extract_json_object(result["text"])

    if not isinstance(data, dict):
        return []

    facts = data.get("facts", [])

    if not isinstance(facts, list):
        return []

    saved = []

    for item in facts:

        if not isinstance(item, dict):
            continue

        key = clean_text(item.get("key"))
        value = clean_text(item.get("value"))

        if not key or not value:
            continue

        row = create_fact(
            chat_id,
            key,
            value,
            source_message_id=source_message_id,
            source="user"
        )

        if row:
            saved.append(row)

    return saved


# ============================================================
# MEMORY CAPTURE
# ============================================================

def capture_memory(chat_id, text):
    if should_save_memory(text):
        save_memory(
            chat_id,
            text
        )

        save_memory_event(
            chat_id,
            "MEMORY_SAVED",
            text
        )

    if should_save_decision(text):
        save_decision(
            chat_id,
            "User decision",
            text
        )

        save_memory_event(
            chat_id,
            "DECISION_SAVED",
            text
        )

    if should_save_task(text):
        save_task(
            chat_id,
            text
        )

        save_memory_event(
            chat_id,
            "TASK_SAVED",
            text
        )


# ============================================================
# CONTEXT
# ============================================================

def format_context(chat_id):
    parts = []

    # --------------------------------------------------------
    # Confirmed / unconfirmed facts
    # --------------------------------------------------------

    facts = get_facts(chat_id)

    if facts:
        parts.append("=== FACT DATABASE ===")

        for fact in facts:

            status = fact.get("status", "UNKNOWN")
            key = fact.get("fact_key", "")
            value = fact.get("fact_value", "")

            if status == "CONFIRMED":
                label = "CONFIRMED FACT"

            elif status == "CONFLICT":
                label = "CONFLICT / UNCONFIRMED"

            elif status == "PENDING":
                label = "PENDING / UNCONFIRMED"

            else:
                label = status

            parts.append(
                f"[{label}] {key} = {value}"
            )

    # --------------------------------------------------------
    # Memories
    # --------------------------------------------------------

    memories = get_memories(
        chat_id,
        MAX_MEMORIES
    )

    if memories:
        parts.append("\n=== MEMORY ===")

        for memory in memories:
            parts.append(
                f"- {memory.get('memory', '')}"
            )

    # --------------------------------------------------------
    # Decisions
    # --------------------------------------------------------

    decisions = get_decisions(
        chat_id,
        MAX_DECISIONS
    )

    if decisions:
        parts.append("\n=== DECISIONS ===")

        for item in decisions:
            parts.append(
                f"- {item.get('title', '')}: "
                f"{item.get('decision', '')}"
            )

    # --------------------------------------------------------
    # Tasks
    # --------------------------------------------------------

    tasks = get_tasks(
        chat_id,
        MAX_TASKS
    )

    if tasks:
        parts.append("\n=== TASKS ===")

        for task in tasks:
            parts.append(
                f"- [{task.get('status', '')}] "
                f"{task.get('title', '')}: "
                f"{task.get('description', '')}"
            )

    # --------------------------------------------------------
    # Projects
    # --------------------------------------------------------

    projects = get_projects(
        chat_id,
        MAX_PROJECTS
    )

    if projects:
        parts.append("\n=== PROJECTS ===")

        for project in projects:
            parts.append(
                f"- [{project.get('status', '')}] "
                f"{project.get('name', '')}: "
                f"{project.get('description', '')}"
            )

    # --------------------------------------------------------
    # Recent messages
    # --------------------------------------------------------

    messages = get_recent_messages(
        chat_id,
        MAX_CONTEXT_MESSAGES
    )

    if messages:
        parts.append("\n=== RECENT CONVERSATION ===")

        for message in messages:
            role = message.get("role", "")
            content = truncate_text(
                message.get("message", ""),
                4000
            )

            parts.append(
                f"{role}: {content}"
            )

    return "\n".join(parts)


# ============================================================
# WEB RESEARCH
# ============================================================

def should_use_web_search(text):
    text = (text or "").lower()

    current_keywords = [
        "დღეს",
        "ახლა",
        "ამჟამად",
        "უახლესი",
        "ბოლო მონაცემები",
        "მიმდინარე",
        "2026",
        "today",
        "now",
        "current",
        "latest",
        "recent",
        "market price",
        "current price",
        "news",
        "სიახლე",
        "კანონი",
        "რეგულაცია",
        "law",
        "regulation",
        "investor",
        "ინვესტორი",
        "company",
        "კომპანია",
        "hotel price",
        "უძრავი ქონების ფასი",
        "ბინის ფასი",
        "construction cost",
        "სამშენებლო ფასი"
    ]

    return contains_any(
        text,
        current_keywords
    )


def save_research(chat_id, query, result):
    try:
        db_execute("""
            INSERT INTO research
            (chat_id, query, result)
            VALUES (%s, %s, %s)
        """, (
            chat_id,
            query,
            result
        ))
    except Exception as e:
        print("save_research error:", repr(e))


# ============================================================
# FINANCIAL ENGINE
# ============================================================

def npv(rate, cash_flows):
    rate = safe_float(rate, 0.0)

    if rate <= -1:
        return None

    total = 0.0

    for period, cash_flow in enumerate(cash_flows):
        try:
            total += (
                safe_float(cash_flow, 0.0)
                / ((1.0 + rate) ** period)
            )
        except Exception:
            return None

    return total


def calculate_irr(cash_flows):
    if not cash_flows or len(cash_flows) < 2:
        return None

    cash_flows = [
        safe_float(x, 0.0)
        for x in cash_flows
    ]

    # Must contain both negative and positive values.
    if not (
        any(x < 0 for x in cash_flows)
        and any(x > 0 for x in cash_flows)
    ):
        return None

    # Scan first so we can find possible sign changes.
    low = -0.9999
    high = 10.0

    try:
        previous_rate = low
        previous_npv = npv(
            previous_rate,
            cash_flows
        )

        intervals = []

        steps = 500

        for i in range(1, steps + 1):
            rate = low + (
                (high - low) * i / steps
            )

            current_npv = npv(
                rate,
                cash_flows
            )

            if (
                previous_npv is not None
                and current_npv is not None
                and previous_npv * current_npv <= 0
            ):
                intervals.append(
                    (
                        previous_rate,
                        rate
                    )
                )

            previous_rate = rate
            previous_npv = current_npv

        if not intervals:
            return None

        a, b = intervals[0]

        for _ in range(120):
            mid = (a + b) / 2.0

            value = npv(
                mid,
                cash_flows
            )

            if value is None:
                return None

            if abs(value) < 1e-7:
                return mid

            va = npv(
                a,
                cash_flows
            )

            if va is None:
                return None

            if va * value <= 0:
                b = mid
            else:
                a = mid

        return (a + b) / 2.0

    except Exception:
        return None


def calculate_payback(cash_flows):
    if not cash_flows:
        return None

    cumulative = 0.0

    for i, value in enumerate(cash_flows):

        value = safe_float(value)

        previous = cumulative
        cumulative += value

        if cumulative >= 0:

            if i == 0:
                return 0.0

            if value == 0:
                return float(i)

            fraction = (
                -previous / value
            )

            return max(
                0.0,
                (i - 1) + fraction
            )

    return None


def calculate_break_even(
    fixed_costs,
    variable_cost_ratio
):
    fixed_costs = safe_float(
        fixed_costs,
        0.0
    )

    ratio = normalize_percent(
        variable_cost_ratio
    )

    if ratio >= 1:
        return None

    return fixed_costs / (
        1.0 - ratio
    )


def calculate_financial_model(data):
    """
    Financial engine.

    IMPORTANT:
    Missing revenue/cost data is NOT silently interpreted as a valid
    profitable project. The result explicitly reports missing inputs.
    """

    data = data or {}

    land_cost = safe_float(
        data.get("land_cost")
    )

    construction_cost = safe_float(
        data.get("construction_cost")
    )

    other_costs = safe_float(
        data.get("other_costs")
    )

    initial_investment = safe_float(
        data.get("initial_investment")
    )

    loan_amount = safe_float(
        data.get("loan_amount")
    )

    loan_rate = safe_float(
        data.get("loan_rate_percent")
    )

    loan_years = safe_float(
        data.get("loan_years")
    )

    sales_revenue = safe_float(
        data.get("sales_revenue")
    )

    rental_revenue = safe_float(
        data.get("rental_revenue")
    )

    other_revenue = safe_float(
        data.get("other_revenue")
    )

    investor_share = normalize_percent(
        data.get("investor_share_percent")
    )

    fixed_costs = safe_float(
        data.get("fixed_costs")
    )

    variable_ratio = normalize_percent(
        data.get("variable_cost_ratio")
    )

    annual_cash_flows = data.get(
        "annual_cash_flows"
    )

    if not isinstance(
        annual_cash_flows,
        list
    ):
        annual_cash_flows = []

    annual_cash_flows = [
        safe_float(x)
        for x in annual_cash_flows
    ]

    base_cost = (
        land_cost
        + construction_cost
        + other_costs
    )

    if initial_investment > 0:
        base_cost = initial_investment

    loan_interest = 0.0

    if (
        loan_amount > 0
        and loan_rate > 0
        and loan_years > 0
    ):
        loan_interest = (
            loan_amount
            * loan_rate
            / 100.0
            * loan_years
        )

    total_cost = (
        base_cost
        + loan_interest
    )

    total_revenue = (
        sales_revenue
        + rental_revenue
        + other_revenue
    )

    net_profit = (
        total_revenue
        - total_cost
    )

    roi = None

    if total_cost > 0:
        roi = (
            net_profit
            / total_cost
        )

    investor_profit = (
        net_profit
        * investor_share
    )

    equity_investment = (
        total_cost
        - loan_amount
    )

    break_even = calculate_break_even(
        fixed_costs,
        variable_ratio
    )

    irr = None

    if annual_cash_flows:
        irr = calculate_irr(
            annual_cash_flows
        )

    payback = None

    if annual_cash_flows:
        payback = calculate_payback(
            annual_cash_flows
        )

    missing = []

    if total_revenue == 0:
        missing.append(
            "revenue"
        )

    if base_cost == 0:
        missing.append(
            "investment/cost"
        )

    return {
        "land_cost": land_cost,
        "construction_cost": construction_cost,
        "other_costs": other_costs,
        "base_cost": base_cost,
        "loan_amount": loan_amount,
        "loan_interest": loan_interest,
        "total_cost": total_cost,
        "sales_revenue": sales_revenue,
        "rental_revenue": rental_revenue,
        "other_revenue": other_revenue,
        "total_revenue": total_revenue,
        "net_profit": net_profit,
        "roi": roi,
        "investor_share": investor_share,
        "investor_profit": investor_profit,
        "equity_investment": equity_investment,
        "break_even": break_even,
        "irr": irr,
        "payback": payback,
        "missing_inputs": missing
    }


def format_money(value):
    if value is None:
        return "N/A"

    return f"${safe_float(value):,.2f}"


def format_financial_result(result):
    lines = []

    lines.append("📊 ფინანსური ანალიზი")
    lines.append("")
    lines.append(
        f"ჯამური ხარჯი: "
        f"{format_money(result['total_cost'])}"
    )

    lines.append(
        f"ჯამური შემოსავალი: "
        f"{format_money(result['total_revenue'])}"
    )

    lines.append(
        f"წმინდა მოგება: "
        f"{format_money(result['net_profit'])}"
    )

    if result["roi"] is not None:
        lines.append(
            f"ROI: "
            f"{result['roi'] * 100:.2f}%"
        )

    if result["irr"] is not None:
        lines.append(
            f"IRR: "
            f"{result['irr'] * 100:.2f}%"
        )

    if result["payback"] is not None:
        lines.append(
            f"Payback: "
            f"{result['payback']:.2f} პერიოდი"
        )

    if result["investor_share"] > 0:
        lines.append(
            f"ინვესტორის წილი: "
            f"{result['investor_share'] * 100:.2f}%"
        )

        lines.append(
            f"ინვესტორის მოგება: "
            f"{format_money(result['investor_profit'])}"
        )

    if result["loan_interest"] > 0:
        lines.append(
            f"სესხის მარტივი დარიცხული პროცენტი: "
            f"{format_money(result['loan_interest'])}"
        )

    if result["break_even"] is not None:
        lines.append(
            f"Break-even revenue: "
            f"{format_money(result['break_even'])}"
        )

    if result["missing_inputs"]:
        lines.append("")
        lines.append(
            "⚠️ არასაკმარისი მონაცემები: "
            + ", ".join(result["missing_inputs"])
        )

        lines.append(
            "ზემოთ მოცემული ნულოვანი მნიშვნელობები "
            "არ ნიშნავს, რომ რეალური ხარჯი/შემოსავალი ნულია."
        )

    return "\n".join(lines)


def should_use_financial_engine(text):
    text = (text or "").lower()

    strong_keywords = [
        "financial model",
        "financial analysis",
        "profit",
        "roi",
        "irr",
        "npv",
        "payback",
        "break even",
        "cash flow",
        "revenue",
        "cost",
        "მოგება",
        "შემოსავალი",
        "ხარჯი",
        "ბიუჯეტი",
        "ფინანსური მოდელი",
        "ფინანსური ანალიზი",
        "ინვესტიცია",
        "ინვესტორის მოგება",
        "roi",
        "irr",
        "npv",
        "payback",
        "break-even",
        "cash flow"
    ]

    return contains_any(
        text,
        strong_keywords
    )


def extract_financial_model_from_text(text):
    prompt = f"""
Extract financial numbers explicitly provided by the user.

Return JSON only:

{{
  "land_cost": null,
  "construction_cost": null,
  "other_costs": null,
  "initial_investment": null,
  "loan_amount": null,
  "loan_rate_percent": null,
  "loan_years": null,
  "sales_revenue": null,
  "rental_revenue": null,
  "other_revenue": null,
  "investor_share_percent": null,
  "annual_cash_flows": [],
  "fixed_costs": null,
  "variable_cost_ratio": null
}}

Rules:
- Do not guess.
- Use null when not explicitly provided.
- Percentages must be numeric.
- Example: 80% -> 80.
- Example: 13.5% -> 13.5.
- Cash flows should only be returned if explicitly provided.

User text:
{text}
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.0,
        max_output_tokens=3000
    )

    if not result["ok"]:
        return None

    data = extract_json_object(
        result["text"]
    )

    if not isinstance(data, dict):
        return None

    return data


def financial_engine_from_text(text):
    data = extract_financial_model_from_text(
        text
    )

    if not data:
        return None

    # Need meaningful financial data.
    meaningful_keys = [
        "land_cost",
        "construction_cost",
        "other_costs",
        "initial_investment",
        "loan_amount",
        "sales_revenue",
        "rental_revenue",
        "other_revenue",
        "annual_cash_flows"
    ]

    meaningful = False

    for key in meaningful_keys:
        value = data.get(key)

        if key == "annual_cash_flows":
            if isinstance(value, list) and value:
                meaningful = True
                break

        elif value is not None:
            if safe_float(value, 0.0) != 0:
                meaningful = True
                break

    if not meaningful:
        return None

    result = calculate_financial_model(
        data
    )

    return format_financial_result(
        result
    )


# ============================================================
# CODE ENGINE
# ============================================================

def extract_code(text):
    text = text or ""

    matches = re.findall(
        r"```(?:python|py)?\s*(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    if matches:
        return max(
            matches,
            key=len
        ).strip()

    return ""


def check_python_syntax(code):
    if not code:
        return {
            "ok": False,
            "error": "No Python code found."
        }

    try:
        ast.parse(code)

        return {
            "ok": True,
            "error": ""
        }

    except SyntaxError as e:
        return {
            "ok": False,
            "error": (
                f"SyntaxError: {e.msg} "
                f"(line {e.lineno}, column {e.offset})"
            )
        }

    except Exception as e:
        return {
            "ok": False,
            "error": repr(e)
        }


def detect_common_python_problems(code):
    problems = []

    if not code:
        return problems

    lines = code.splitlines()

    if "\t" in code:
        has_spaces = any(
            line.startswith("    ")
            for line in lines
            if line.strip()
        )

        if has_spaces:
            problems.append(
                "კოდში აღმოჩნდა tabs და spaces-ის შერევა."
            )

    if re.search(
        r"def\s+\w+\([^)]*\)\s*:\s*\n\s*(?:def|class|$)",
        code
    ):
        problems.append(
            "შესაძლოა ფუნქცია ცარიელი body-ით იყოს."
        )

    if "OPENAI_API_KEY" in code and "openai" in code.lower():
        problems.append(
            "კოდი იყენებს OpenAI credential-ს; "
            "Geniosa-ს მიმდინარე არქიტექტურა Gemini-ზეა."
        )

    if "psycopg2" in code:
        problems.append(
            "Render-ზე საჭიროა psycopg2-binary requirements-ში."
        )

    return problems


def analyze_python_code(code):
    syntax = check_python_syntax(
        code
    )

    problems = detect_common_python_problems(
        code
    )

    return {
        "syntax": syntax,
        "problems": problems
    }


def developer_engine(text):
    code = extract_code(
        text
    )

    if code:
        analysis = analyze_python_code(
            code
        )

        lines = []

        if analysis["syntax"]["ok"]:
            lines.append(
                "✅ Python syntax: OK"
            )
        else:
            lines.append(
                "❌ Python syntax error:"
            )
            lines.append(
                analysis["syntax"]["error"]
            )

        if analysis["problems"]:
            lines.append("")
            lines.append(
                "⚠️ შესაძლო პრობლემები:"
            )

            for problem in analysis["problems"]:
                lines.append(
                    f"- {problem}"
                )

        return "\n".join(lines)

    prompt = f"""
Act as a senior Python developer.

Analyze the user's request.
Do not invent missing files or configuration.
Explain the likely cause.
Then provide a practical fix.

User request:
{text}
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.15,
        max_output_tokens=10000
    )

    if result["ok"]:
        return result["text"]

    return (
        "კოდის ანალიზი ვერ შესრულდა.\n"
        f"Gemini error: {result['error']}"
    )


# ============================================================
# DEBUG ENGINE
# ============================================================

def debug_engine(text):
    code = extract_code(
        text
    )

    prompt = f"""
You are debugging a production Python application.

Identify:
1. exact error/cause
2. why it happens
3. safest fix
4. corrected code where useful

Do not invent information.

User problem:
{text}

Code:
{code if code else '[No code block supplied]'}
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.1,
        max_output_tokens=12000
    )

    if result["ok"]:
        return result["text"]

    return (
        "Debugging ვერ შესრულდა.\n"
        f"Gemini error: {result['error']}"
    )


# ============================================================
# CODE PROJECTS
# ============================================================

def create_code_project(chat_id, name, description=""):
    try:
        row = db_execute("""
            INSERT INTO code_projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING *
        """, (
            chat_id,
            name,
            description
        ), fetchone=True)

        return row

    except Exception as e:
        print("create_code_project error:", repr(e))
        return None


def save_code_version(
    chat_id,
    code_project_id,
    version_name,
    code
):
    try:
        return db_execute("""
            INSERT INTO code_versions
            (
                code_project_id,
                chat_id,
                version_name,
                code
            )
            VALUES (%s, %s, %s, %s)
            RETURNING *
        """, (
            code_project_id,
            chat_id,
            version_name,
            code
        ), fetchone=True)

    except Exception as e:
        print("save_code_version error:", repr(e))
        return None


def get_code_versions(chat_id, limit=20):
    try:
        return db_execute("""
            SELECT *
            FROM code_versions
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
        """, (
            chat_id,
            limit
        ), fetch=True) or []

    except Exception as e:
        print("get_code_versions error:", repr(e))
        return []


# ============================================================
# BOT FACTORY
# ============================================================

def create_bot_project(chat_id, name, description=""):
    try:
        return db_execute("""
            INSERT INTO bot_projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING *
        """, (
            chat_id,
            name,
            description
        ), fetchone=True)

    except Exception as e:
        print("create_bot_project error:", repr(e))
        return None


def save_bot_file(
    bot_project_id,
    filename,
    content
):
    try:
        return db_execute("""
            INSERT INTO bot_files
            (
                bot_project_id,
                filename,
                content
            )
            VALUES (%s, %s, %s)
            RETURNING *
        """, (
            bot_project_id,
            filename,
            content
        ), fetchone=True)

    except Exception as e:
        print("save_bot_file error:", repr(e))
        return None


def get_bot_projects(chat_id):
    try:
        return db_execute("""
            SELECT *
            FROM bot_projects
            WHERE chat_id = %s
            ORDER BY id DESC
        """, (
            chat_id,
        ), fetch=True) or []

    except Exception as e:
        print("get_bot_projects error:", repr(e))
        return []


def get_bot_files(
    bot_project_id,
    chat_id=None
):
    try:

        if chat_id is not None:

            return db_execute("""
                SELECT bf.*
                FROM bot_files bf
                JOIN bot_projects bp
                  ON bp.id = bf.bot_project_id
                WHERE bf.bot_project_id = %s
                AND bp.chat_id = %s
                ORDER BY bf.id
            """, (
                bot_project_id,
                chat_id
            ), fetch=True) or []

        return db_execute("""
            SELECT *
            FROM bot_files
            WHERE bot_project_id = %s
            ORDER BY id
        """, (
            bot_project_id,
        ), fetch=True) or []

    except Exception as e:
        print("get_bot_files error:", repr(e))
        return []


def bot_factory_create(
    chat_id,
    bot_name,
    description
):
    prompt = f"""
Create a production-oriented Telegram bot project.

Bot name:
{bot_name}

Description:
{description}

Return JSON ONLY:

{{
  "files": [
    {{
      "filename": "main.py",
      "content": "..."
    }},
    {{
      "filename": "requirements.txt",
      "content": "..."
    }},
    {{
      "filename": ".env.example",
      "content": "..."
    }},
    {{
      "filename": "README.md",
      "content": "..."
    }}
  ]
}}

Rules:
- Do not invent unavailable API keys.
- Use environment variables for secrets.
- Keep code practical.
- Make requirements complete.
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.1,
        max_output_tokens=20000
    )

    if not result["ok"]:
        return None, (
            "Bot generation failed: "
            + result["error"]
        )

    data = extract_json_object(
        result["text"]
    )

    if not isinstance(data, dict):
        return None, "Gemini returned invalid bot JSON."

    files = data.get("files", [])

    if not isinstance(files, list):
        return None, "Bot files were not returned."

    project = create_bot_project(
        chat_id,
        bot_name,
        description
    )

    if not project:
        return None, "Could not create bot project."

    saved_files = []

    for item in files:

        if not isinstance(item, dict):
            continue

        filename = clean_text(
            item.get("filename")
        )

        content = item.get(
            "content",
            ""
        )

        if not filename:
            continue

        # Do not allow path traversal.
        filename = os.path.basename(
            filename
        )

        saved = save_bot_file(
            project["id"],
            filename,
            str(content)
        )

        if saved:
            saved_files.append(saved)

    return project, saved_files


# ============================================================
# FINANCIAL STORAGE
# ============================================================

def save_financial_model(
    chat_id,
    name,
    data
):
    try:
        return db_execute("""
            INSERT INTO financial_models
            (chat_id, name, data)
            VALUES (%s, %s, %s::jsonb)
            RETURNING *
        """, (
            chat_id,
            name,
            json.dumps(
                data,
                ensure_ascii=False
            )
        ), fetchone=True)

    except Exception as e:
        print("save_financial_model error:", repr(e))
        return None


def get_financial_models(chat_id):
    try:
        return db_execute("""
            SELECT *
            FROM financial_models
            WHERE chat_id = %s
            ORDER BY id DESC
        """, (
            chat_id,
        ), fetch=True) or []

    except Exception as e:
        print("get_financial_models error:", repr(e))
        return []


# ============================================================
# MAIN ANSWER ENGINE
# ============================================================

def generate_answer(chat_id, user_text):
    user_text = clean_text(
        user_text
    )

    if not user_text:
        return "გთხოვ, დამიწერე კითხვა."

    # --------------------------------------------------------
    # Financial engine
    # --------------------------------------------------------

    if should_use_financial_engine(
        user_text
    ):
        try:
            financial_answer = (
                financial_engine_from_text(
                    user_text
                )
            )

            if financial_answer:
                # Let Gemini improve/explain only if needed.
                prompt = f"""
The user asked for a financial/business analysis.

Below is a deterministic calculation produced by Geniosa's
financial engine.

Do NOT change the numbers.
Do NOT invent missing values.

Explain the result clearly and identify important assumptions.

Calculation:
{financial_answer}

User question:
{user_text}
"""

                result = call_gemini(
                    prompt,
                    use_search=False,
                    temperature=0.1,
                    max_output_tokens=6000
                )

                if result["ok"]:
                    return result["text"]

                return financial_answer

        except Exception as e:
            print(
                "financial engine error:",
                repr(e)
            )

    # --------------------------------------------------------
    # Normal Gemini answer
    # --------------------------------------------------------

    context = format_context(
        chat_id
    )

    use_search = should_use_web_search(
        user_text
    )

    prompt = f"""
You are answering the user's latest message.

USER MESSAGE:
{user_text}

DATABASE CONTEXT:
{truncate_text(context, 45000)}

INSTRUCTIONS:
- Use confirmed facts as established facts.
- Treat PENDING and CONFLICT facts as unconfirmed.
- If there is a conflict, explicitly say so.
- Do not invent missing information.
- If the question requires current information and web search is enabled,
  use the available Google Search grounding.
- If research sources are returned, distinguish researched information
  from user-provided facts.
- If calculations are needed, show assumptions.
"""

    result = call_gemini(
        prompt,
        use_search=use_search,
        temperature=0.2,
        max_output_tokens=12000
    )

    if not result["ok"]:
        return (
            "⚠️ Geniosa-ს AI პასუხის მიღება ვერ მოხერხდა.\n\n"
            f"ტექნიკური მიზეზი: {result['error']}"
        )

    if result["sources"]:
        save_research(
            chat_id,
            user_text,
            json.dumps(
                result["sources"],
                ensure_ascii=False
            )
        )

    return result["text"]


# ============================================================
# TELEGRAM
# ============================================================

def telegram_call(
    method,
    payload=None,
    timeout=TELEGRAM_TIMEOUT
):
    if not TELEGRAM_API:
        return {
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN is not configured"
        }

    url = f"{TELEGRAM_API}/{method}"

    try:
        response = requests.post(
            url,
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
            data.setdefault(
                "ok",
                False
            )

        return data

    except requests.RequestException as e:
        return {
            "ok": False,
            "error": str(e)
        }

    except Exception as e:
        return {
            "ok": False,
            "error": repr(e)
        }


def split_telegram_text(text, max_length=MAX_TELEGRAM_MESSAGE):
    text = clean_text(text)

    if len(text) <= max_length:
        return [text]

    chunks = []

    while text:

        if len(text) <= max_length:
            chunks.append(text)
            break

        cut = text.rfind(
            "\n",
            0,
            max_length
        )

        if cut < 1000:
            cut = text.rfind(
                " ",
                0,
                max_length
            )

        if cut < 1000:
            cut = max_length

        chunk = text[:cut].strip()

        if chunk:
            chunks.append(chunk)

        text = text[cut:].strip()

    return chunks


def send_message(chat_id, text):
    text = clean_text(text)

    if not text:
        return False

    chunks = split_telegram_text(
        text
    )

    success = True

    for chunk in chunks:

        result = telegram_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            }
        )

        if not result.get("ok"):
            success = False

            print(
                "Telegram sendMessage error:",
                result
            )

            # Retry once for transient failures.
            time.sleep(1)

            retry = telegram_call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk
                }
            )

            if not retry.get("ok"):
                print(
                    "Telegram retry failed:",
                    retry
                )

    return success


def delete_webhook():
    if not TELEGRAM_API:
        return

    try:
        result = telegram_call(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            }
        )

        print(
            "deleteWebhook:",
            result
        )

    except Exception as e:
        print(
            "deleteWebhook error:",
            repr(e)
        )


# ============================================================
# AUTHORIZATION
# ============================================================

def is_authorized_chat(chat_id):
    """
    If GENIOSA_OWNER_ID is configured, only that Telegram user
    can use the bot.

    If it is not configured, the bot remains usable as before.
    """

    if GENIOSA_OWNER_ID is None:
        return True

    return int(chat_id) == int(
        GENIOSA_OWNER_ID
    )


# ============================================================
# COMMAND HELPERS
# ============================================================

def format_facts(facts):
    if not facts:
        return "ფაქტების ბაზა ცარიელია."

    lines = [
        "🧠 ფაქტების ბაზა:",
        ""
    ]

    for fact in facts:

        status = fact.get(
            "status",
            "UNKNOWN"
        )

        if status == "CONFIRMED":
            icon = "✅"

        elif status == "CONFLICT":
            icon = "⚠️"

        elif status == "PENDING":
            icon = "🟡"

        else:
            icon = "❔"

        lines.append(
            f"{icon} #{fact.get('id')} "
            f"{fact.get('fact_key')} = "
            f"{fact.get('fact_value')} "
            f"[{status}]"
        )

    return "\n".join(lines)


def format_projects(projects):
    if not projects:
        return "პროექტები ჯერ არ არის."

    lines = [
        "🏗 პროექტები:",
        ""
    ]

    for project in projects:
        lines.append(
            f"#{project.get('id')} "
            f"{project.get('name')} "
            f"[{project.get('status')}]"
        )

        if project.get("description"):
            lines.append(
                f"  {project.get('description')}"
            )

    return "\n".join(lines)


def format_tasks(tasks):
    if not tasks:
        return "დავალებები ჯერ არ არის."

    lines = [
        "📋 დავალებები:",
        ""
    ]

    for task in tasks:
        lines.append(
            f"#{task.get('id')} "
            f"[{task.get('status')}] "
            f"{task.get('title')}"
        )

        if task.get("description"):
            lines.append(
                f"  {task.get('description')}"
            )

    return "\n".join(lines)


def format_decisions(decisions):
    if not decisions:
        return "გადაწყვეტილებები ჯერ არ არის."

    lines = [
        "🎯 გადაწყვეტილებები:",
        ""
    ]

    for decision in decisions:
        lines.append(
            f"#{decision.get('id')} "
            f"{decision.get('title')}: "
            f"{decision.get('decision')}"
        )

    return "\n".join(lines)


# ============================================================
# COMMAND HANDLER
# ============================================================

def handle_command(chat_id, text):
    parts = text.strip().split(
        maxsplit=1
    )

    command = parts[0].lower()

    argument = (
        parts[1].strip()
        if len(parts) > 1
        else ""
    )

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if command == "/start":

        return (
            f"🤖 Geniosa {APP_VERSION}\n\n"
            "მე ვარ შენი პირადი ბიზნეს-მრჩეველი და ასისტენტი.\n\n"
            "შემიძლია დაგეხმარო:\n"
            "• ბიზნესში\n"
            "• ინვესტიციებში\n"
            "• ფინანსურ მოდელებში\n"
            "• სამშენებლო პროექტებში\n"
            "• ინვესტორების მოძიებაში\n"
            "• ბაზრის კვლევაში\n"
            "• პროგრამირებაში\n"
            "• debugging-ში\n"
            "• Telegram ბოტების შექმნაში\n"
            "• პროექტებისა და გადაწყვეტილებების შენახვაში\n\n"
            "დაწერე ჩვეულებრივად რა გჭირდება."
        )

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if command == "/help":

        return (
            f"🤖 Geniosa {APP_VERSION}\n\n"
            "/start — დაწყება\n"
            "/help — დახმარება\n"
            "/memory — მეხსიერება\n"
            "/facts — ფაქტები\n"
            "/fact_history — ფაქტების ისტორია\n"
            "/projects — პროექტები\n"
            "/decisions — გადაწყვეტილებები\n"
            "/tasks — დავალებები\n"
            "/finance — ფინანსური ანალიზი\n"
            "/financial_models — ფინანსური მოდელები\n"
            "/code — კოდის ანალიზი\n"
            "/code_history — კოდის ისტორია\n"
            "/debug — debugging\n"
            "/bots — შექმნილი ბოტები\n"
            "/bot_create — ახალი ბოტი\n"
            "/bot_code ID — ბოტის კოდი\n\n"
            "შენ შეგიძლია უბრალოდ ჩვეულებრივადაც მომწერო."
        )

    # --------------------------------------------------------
    # MEMORY
    # --------------------------------------------------------

    if command == "/memory":

        memories = get_memories(
            chat_id,
            100
        )

        if not memories:
            return "🧠 მეხსიერება ცარიელია."

        lines = [
            "🧠 Geniosa-ს მეხსიერება:",
            ""
        ]

        for item in memories:
            lines.append(
                f"- {item.get('memory')}"
            )

        return "\n".join(lines)

    # --------------------------------------------------------
    # FACTS
    # --------------------------------------------------------

    if command == "/facts":

        return format_facts(
            get_facts(
                chat_id,
                100
            )
        )

    # --------------------------------------------------------
    # FACT HISTORY
    # --------------------------------------------------------

    if command == "/fact_history":

        history = get_fact_history(
            chat_id,
            100
        )

        if not history:
            return "ფაქტების ისტორია ცარიელია."

        lines = [
            "📚 ფაქტების ისტორია:",
            ""
        ]

        for item in history:
            lines.append(
                f"#{item.get('id')} "
                f"{item.get('action')} "
                f"{item.get('old_value') or ''}"
                f" → "
                f"{item.get('new_value') or ''}"
            )

        return "\n".join(lines)

    # --------------------------------------------------------
    # CONFIRM FACT
    # --------------------------------------------------------

    if command == "/confirm_fact":

        fact_id = safe_int(
            argument
        )

        if not fact_id:
            return (
                "გამოყენება:\n"
                "/confirm_fact ID"
            )

        if confirm_fact(
            chat_id,
            fact_id
        ):
            return (
                f"✅ ფაქტი #{fact_id} "
                "დადასტურებულია."
            )

        return (
            "❌ ფაქტის დადასტურება ვერ მოხერხდა."
        )

    # --------------------------------------------------------
    # REJECT FACT
    # --------------------------------------------------------

    if command == "/reject_fact":

        fact_id = safe_int(
            argument
        )

        if not fact_id:
            return (
                "გამოყენება:\n"
                "/reject_fact ID"
            )

        if reject_fact(
            chat_id,
            fact_id
        ):
            return (
                f"🗑 ფაქტი #{fact_id} "
                "უარყოფილია."
            )

        return (
            "❌ ფაქტის უარყოფა ვერ მოხერხდა."
        )

    # --------------------------------------------------------
    # PROJECTS
    # --------------------------------------------------------

    if command == "/projects":

        return format_projects(
            get_projects(
                chat_id
            )
        )

    # --------------------------------------------------------
    # DECISIONS
    # --------------------------------------------------------

    if command == "/decisions":

        return format_decisions(
            get_decisions(
                chat_id
            )
        )

    # --------------------------------------------------------
    # TASKS
    # --------------------------------------------------------

    if command == "/tasks":

        return format_tasks(
            get_tasks(
                chat_id
            )
        )

    # --------------------------------------------------------
    # FINANCIAL MODELS
    # --------------------------------------------------------

    if command == "/financial_models":

        models = get_financial_models(
            chat_id
        )

        if not models:
            return (
                "ფინანსური მოდელები ჯერ არ არის."
            )

        lines = [
            "📊 ფინანსური მოდელები:",
            ""
        ]

        for model in models:
            lines.append(
                f"#{model.get('id')} "
                f"{model.get('name')}"
            )

        return "\n".join(lines)

    # --------------------------------------------------------
    # FINANCE
    # --------------------------------------------------------

    if command == "/finance":

        if not argument:
            return (
                "გამოყენება:\n\n"
                "/finance\n"
                "შემდეგ აღწერე პროექტის ხარჯები, "
                "შემოსავლები და ინვესტიცია."
            )

        return generate_answer(
            chat_id,
            argument
        )

    # --------------------------------------------------------
    # CODE
    # --------------------------------------------------------

    if command == "/code":

        if not argument:
            return (
                "გამოყენება:\n"
                "/code შენი კოდი ან კოდის პრობლემა"
            )

        return developer_engine(
            argument
        )

    # --------------------------------------------------------
    # CODE HISTORY
    # --------------------------------------------------------

    if command == "/code_history":

        versions = get_code_versions(
            chat_id
        )

        if not versions:
            return (
                "კოდის ისტორია ცარიელია."
            )

        lines = [
            "💻 კოდის ისტორია:",
            ""
        ]

        for version in versions:
            lines.append(
                f"#{version.get('id')} "
                f"{version.get('version_name')}"
            )

        return "\n".join(lines)

    # --------------------------------------------------------
    # DEBUG
    # --------------------------------------------------------

    if command == "/debug":

        if not argument:
            return (
                "გამოყენება:\n"
                "/debug აღწერე პრობლემა და "
                "ჩასვი კოდი ``` ``` ბლოკში."
            )

        return debug_engine(
            argument
        )

    # --------------------------------------------------------
    # BOTS
    # --------------------------------------------------------

    if command == "/bots":

        bots = get_bot_projects(
            chat_id
        )

        if not bots:
            return (
                "🤖 ბოტების პროექტები ჯერ არ არის."
            )

        lines = [
            "🤖 ბოტების პროექტები:",
            ""
        ]

        for bot in bots:
            lines.append(
                f"#{bot.get('id')} "
                f"{bot.get('name')} "
                f"[{bot.get('status')}]"
            )

        return "\n".join(lines)

    # --------------------------------------------------------
    # BOT CREATE
    # --------------------------------------------------------

    if command == "/bot_create":

        if not argument:
            return (
                "გამოყენება:\n\n"
                "/bot_create\n"
                "ბოტის სახელი და აღწერა"
            )

        # Split first word as name.
        bot_parts = argument.split(
            maxsplit=1
        )

        bot_name = bot_parts[0]

        description = (
            bot_parts[1]
            if len(bot_parts) > 1
            else "Telegram bot"
        )

        project, files = bot_factory_create(
            chat_id,
            bot_name,
            description
        )

        if not project:
            return str(files)

        lines = [
            "✅ ბოტი შეიქმნა.",
            "",
            f"ID: {project['id']}",
            f"სახელი: {project['name']}",
            "",
            "ფაილები:"
        ]

        for file in files:
            lines.append(
                f"- {file['filename']}"
            )

        lines.append("")
        lines.append(
            f"/bot_code {project['id']} "
            "— კოდის სანახავად"
        )

        return "\n".join(lines)

    # --------------------------------------------------------
    # BOT CODE
    # --------------------------------------------------------

    if command == "/bot_code":

        bot_id = safe_int(
            argument
        )

        if not bot_id:
            return (
                "გამოყენება:\n"
                "/bot_code ID"
            )

        # IMPORTANT:
        # Ownership is checked here.
        files = get_bot_files(
            bot_id,
            chat_id=chat_id
        )

        if not files:
            return (
                "❌ ასეთი ბოტი ვერ მოიძებნა "
                "ან შენ არ გაქვს მასზე წვდომა."
            )

        output = []

        for file in files:
            output.append(
                f"===== {file['filename']} ====="
            )
            output.append(
                file.get("content", "")
            )
            output.append("")

        return "\n".join(output)

    return None


# ============================================================
# TELEGRAM UPDATE PROCESSING
# ============================================================

def process_update(update):
    if not isinstance(update, dict):
        return True

    message = update.get(
        "message"
    )

    if not message:
        return True

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    if chat_id is None:
        return True

    # --------------------------------------------------------
    # Authorization
    # --------------------------------------------------------

    if not is_authorized_chat(
        chat_id
    ):
        print(
            "Unauthorized Telegram chat:",
            chat_id
        )

        send_message(
            chat_id,
            "⛔ ამ Geniosa ბოტზე წვდომა შეზღუდულია."
        )

        return True

    text = message.get(
        "text"
    )

    if not text:
        return True

    text = clean_text(
        text
    )

    if not text:
        return True

    # --------------------------------------------------------
    # Save user message
    # --------------------------------------------------------

    message_id = save_message(
        chat_id,
        "user",
        text
    )

    # --------------------------------------------------------
    # Memory
    # --------------------------------------------------------

    try:
        capture_memory(
            chat_id,
            text
        )
    except Exception as e:
        print(
            "capture_memory error:",
            repr(e)
        )

    # --------------------------------------------------------
    # Fact extraction
    # --------------------------------------------------------

    try:
        extract_facts_from_user_text(
            chat_id,
            text,
            source_message_id=message_id
        )
    except Exception as e:
        print(
            "fact extraction error:",
            repr(e)
        )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    if text.startswith("/"):
        try:
            answer = handle_command(
                chat_id,
                text
            )
        except Exception as e:
            print(
                "handle_command error:",
                repr(e)
            )

            answer = (
                "⚠️ ბრძანების შესრულებისას "
                "დაფიქსირდა ტექნიკური შეცდომა."
            )

    else:
        # ----------------------------------------------------
        # Normal AI answer
        # ----------------------------------------------------

        try:
            answer = generate_answer(
                chat_id,
                text
            )

        except Exception as e:
            print(
                "generate_answer error:",
                repr(e)
            )

            answer = (
                "⚠️ პასუხის გენერირებისას "
                "დაფიქსირდა ტექნიკური შეცდომა."
            )

    answer = clean_text(
        answer
    )

    if not answer:
        answer = (
            "ვერ მივიღე პასუხი. "
            "გთხოვ, კითხვა თავიდან გამომიგზავნე."
        )

    # --------------------------------------------------------
    # Save assistant message
    # --------------------------------------------------------

    save_message(
        chat_id,
        "assistant",
        answer
    )

    # --------------------------------------------------------
    # Send
    # --------------------------------------------------------

    send_message(
        chat_id,
        answer
    )

    return True


# ============================================================
# POSTGRES ADVISORY LOCK
# ============================================================

def get_polling_lock_key():
    if not TELEGRAM_BOT_TOKEN:
        return None

    digest = hashlib.sha256(
        TELEGRAM_BOT_TOKEN.encode(
            "utf-8"
        )
    ).digest()

    number = int.from_bytes(
        digest[:8],
        byteorder="big",
        signed=False
    )

    # PostgreSQL bigint range.
    return (
        number
        % 9223372036854775807
    )


def acquire_polling_lock():
    global polling_connection

    if not DATABASE_URL:
        print(
            "Polling lock disabled: "
            "DATABASE_URL missing."
        )
        return True

    lock_key = get_polling_lock_key()

    if lock_key is None:
        return False

    try:
        conn = get_db()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (lock_key,)
            )

            row = cur.fetchone()

            if not row or not row[0]:
                conn.close()

                print(
                    "Another Geniosa instance already "
                    "holds the Telegram polling lock."
                )

                return False

        polling_connection = conn

        print(
            "PostgreSQL Telegram polling lock acquired."
        )

        return True

    except Exception as e:
        print(
            "acquire_polling_lock error:",
            repr(e)
        )

        try:
            if conn:
                conn.close()
        except Exception:
            pass

        return False


def release_polling_lock():
    global polling_connection

    if not polling_connection:
        return

    try:
        lock_key = get_polling_lock_key()

        with polling_connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)",
                (lock_key,)
            )

        polling_connection.commit()

    except Exception as e:
        print(
            "release_polling_lock error:",
            repr(e)
        )

    finally:
        try:
            polling_connection.close()
        except Exception:
            pass

        polling_connection = None


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():
    global polling_started

    if not TELEGRAM_BOT_TOKEN:
        print(
            "Telegram polling disabled: "
            "TELEGRAM_BOT_TOKEN missing."
        )
        return

    if not acquire_polling_lock():
        return

    delete_webhook()

    offset = None

    print(
        f"Geniosa {APP_VERSION} Telegram polling started."
    )

    while True:

        try:

            payload = {
                "timeout": 30,
                "allowed_updates": [
                    "message"
                ]
            }

            if offset is not None:
                payload["offset"] = offset

            response = telegram_call(
                "getUpdates",
                payload,
                timeout=40
            )

            if not response.get("ok"):

                description = (
                    response.get("description")
                    or response.get("error")
                    or "Unknown Telegram error"
                )

                print(
                    "Telegram getUpdates error:",
                    description
                )

                # Telegram conflict.
                if (
                    "409" in str(description)
                    or "Conflict" in str(description)
                ):
                    time.sleep(5)

                else:
                    time.sleep(3)

                continue

            updates = response.get(
                "result",
                []
            )

            for update in updates:

                update_id = update.get(
                    "update_id"
                )

                try:
                    process_update(
                        update
                    )

                except Exception as e:
                    print(
                        "process_update fatal error:",
                        repr(e)
                    )

                # Advance offset AFTER attempting processing.
                if update_id is not None:
                    offset = (
                        update_id + 1
                    )

        except Exception as e:
            print(
                "telegram_polling error:",
                repr(e)
            )

            time.sleep(5)


def start_polling_once():
    global polling_started
    global polling_thread

    with polling_state_lock:

        if polling_started:
            return

        polling_started = True

        polling_thread = threading.Thread(
            target=telegram_polling,
            name="geniosa-telegram-polling",
            daemon=True
        )

        polling_thread.start()


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
def root():
    return {
        "service": "Geniosa",
        "version": APP_VERSION,
        "status": "online",
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "database_configured": bool(
            DATABASE_URL
        ),
        "model": GEMINI_MODEL
    }


@app.get("/health")
def health():
    db_ok = False
    db_error = ""

    try:
        conn = get_db()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1"
            )
            cur.fetchone()

        conn.close()

        db_ok = True

    except Exception as e:
        db_error = str(e)

    return {
        "service": "Geniosa",
        "version": APP_VERSION,
        "status": "healthy" if db_ok else "degraded",
        "database": db_ok,
        "database_error": db_error,
        "telegram": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini": bool(
            GEMINI_API_KEY
        ),
        "model": GEMINI_MODEL
    }


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

@app.on_event("startup")
def startup_event():

    print("=" * 60)
    print(
        f"GENIOSA {APP_VERSION} STARTING"
    )
    print("=" * 60)

    # --------------------------------------------------------
    # Database
    # --------------------------------------------------------

    if DATABASE_URL:

        try:
            init_db()

            print(
                "Database initialization: OK"
            )

        except Exception as e:

            print(
                "Database initialization FAILED:",
                repr(e)
            )

    else:
        print(
            "WARNING: DATABASE_URL is missing."
        )

    # --------------------------------------------------------
    # Gemini
    # --------------------------------------------------------

    if GEMINI_API_KEY:
        print(
            f"Gemini: configured "
            f"({GEMINI_MODEL})"
        )
    else:
        print(
            "WARNING: GEMINI_API_KEY is missing."
        )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    if TELEGRAM_BOT_TOKEN:
        print(
            "Telegram: configured"
        )

        start_polling_once()

    else:
        print(
            "WARNING: TELEGRAM_BOT_TOKEN is missing."
        )

    print("=" * 60)


@app.on_event("shutdown")
def shutdown_event():

    global polling_started

    print(
        "Geniosa shutting down..."
    )

    release_polling_lock()

    polling_started = False

    print(
        "Geniosa shutdown complete."
    )


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8000"
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
