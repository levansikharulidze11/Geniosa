import os
import re
import json
import time
import threading
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI


# ============================================================
# GENIOSA 4.7
# Persistent Telegram Business Advisor
# PostgreSQL + Gemini + Telegram
# ============================================================

APP_VERSION = "4.7"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Can be changed in Render Environment Variables.
# 2.5-flash is used as the default because it is a broadly
# supported Gemini API model.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash"
).strip()

GENIOSA_OWNER_ID = os.getenv(
    "GENIOSA_OWNER_ID",
    ""
).strip()

TELEGRAM_TIMEOUT = 35
GEMINI_TIMEOUT = 90
POLL_INTERVAL = 1.0

MAX_MEMORIES = 100
MAX_FACTS = 100
MAX_MESSAGES = 25
MAX_MESSAGE_LENGTH = 12000
MAX_TELEGRAM_LENGTH = 3900

polling_connection = None
polling_started = False
polling_thread = None

polling_state_lock = threading.Lock()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Geniosa",
    version=APP_VERSION
)


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = str(value).strip()

    if len(value) > MAX_MESSAGE_LENGTH:
        value = value[:MAX_MESSAGE_LENGTH]

    return value


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def is_owner(chat_id):
    if not GENIOSA_OWNER_ID:
        return True

    return str(chat_id) == str(GENIOSA_OWNER_ID)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10
    )


def db_execute(
    query,
    params=None,
    fetch=False,
    fetchone=False,
    commit=True
):
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

            if commit:
                conn.commit()

            return result

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        print("DB ERROR:", repr(e))
        raise

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    conn = None

    try:
        conn = get_db()

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

            # Legacy compatibility:
            # some older Geniosa versions used "text".
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
                ADD COLUMN IF NOT EXISTS created_at
                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            """)

            # If old "text" column exists, copy it to message.
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'messages'
                        AND column_name = 'text'
                    )
                    THEN
                        UPDATE messages
                        SET message = COALESCE(
                            message,
                            "text"
                        )
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

            # ------------------------------------------------
            # Fact history
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS fact_history (
                    id SERIAL PRIMARY KEY,
                    fact_id INTEGER,
                    chat_id BIGINT,
                    old_status TEXT,
                    new_status TEXT,
                    note TEXT,
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
                    task TEXT,
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
                    chat_id BIGINT,
                    project_id INTEGER,
                    version TEXT,
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Bot files
            # ------------------------------------------------

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_files (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT,
                    bot_project_id INTEGER,
                    filename TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    action TEXT,
                    data JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # ------------------------------------------------
            # Indexes
            # ------------------------------------------------

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_chat
                ON messages(chat_id, id DESC)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_memories_chat
                ON memories(chat_id, id DESC)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_facts_chat
                ON facts(chat_id, id DESC)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_tasks_chat
                ON tasks(chat_id, id DESC)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_projects_chat
                ON projects(chat_id, id DESC)
            """)

        conn.commit()

        print("DATABASE INITIALIZED")

    except Exception as e:
        print("INIT DB ERROR:", repr(e))
        raise

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# MESSAGES
# ============================================================

def save_message(chat_id, role, message):
    message = clean_text(message)

    if not message:
        return None

    try:
        row = db_execute(
            """
            INSERT INTO messages
                (chat_id, role, message, created_at)
            VALUES
                (%s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id
            """,
            (
                chat_id,
                role,
                message
            ),
            fetchone=True
        )

        return row["id"] if row else None

    except Exception as e:
        print("save_message error:", repr(e))
        return None


def get_recent_messages(chat_id, limit=MAX_MESSAGES):
    try:
        rows = db_execute(
            """
            SELECT id, chat_id, role, message, created_at
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

        return list(reversed(rows or []))

    except Exception as e:
        print("get_recent_messages error:", repr(e))
        return []


def search_messages(chat_id, query, limit=20):
    query = clean_text(query)

    if not query:
        return []

    try:
        return db_execute(
            """
            SELECT id, role, message, created_at
            FROM messages
            WHERE chat_id = %s
            AND message ILIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                "%" + query + "%",
                limit
            ),
            fetch=True
        ) or []

    except Exception as e:
        print("search_messages error:", repr(e))
        return []


# ============================================================
# MEMORY
# ============================================================

def save_memory(chat_id, memory):
    memory = clean_text(memory)

    if not memory:
        return False

    try:
        existing = db_execute(
            """
            SELECT id
            FROM memories
            WHERE chat_id = %s
            AND memory = %s
            LIMIT 1
            """,
            (
                chat_id,
                memory
            ),
            fetchone=True
        )

        if existing:
            return True

        db_execute(
            """
            INSERT INTO memories
                (chat_id, memory, created_at)
            VALUES
                (%s, %s, CURRENT_TIMESTAMP)
            """,
            (
                chat_id,
                memory
            )
        )

        return True

    except Exception as e:
        print("save_memory error:", repr(e))
        return False


def get_memories(chat_id, limit=MAX_MEMORIES):
    try:
        return db_execute(
            """
            SELECT id, memory, created_at
            FROM memories
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        ) or []

    except Exception as e:
        print("get_memories error:", repr(e))
        return []


def save_memory_event(
    chat_id,
    event_type,
    content
):
    try:
        db_execute(
            """
            INSERT INTO memory_events
                (chat_id, event_type, content)
            VALUES
                (%s, %s, %s)
            """,
            (
                chat_id,
                event_type,
                clean_text(content)
            )
        )

    except Exception as e:
        print("save_memory_event error:", repr(e))


# ============================================================
# FACT ENGINE
# ============================================================

VALID_FACT_STATUSES = {
    "PENDING",
    "CONFIRMED",
    "CONFLICT",
    "REJECTED"
}


def normalize_fact_key(key):
    key = clean_text(key).lower()

    key = re.sub(
        r"\s+",
        "_",
        key
    )

    key = re.sub(
        r"[^a-zA-Z0-9ა-ჰ_]+",
        "",
        key
    )

    return key[:200]


def get_confirmed_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    try:
        return db_execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND fact_key = %s
            AND status = 'CONFIRMED'
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                chat_id,
                fact_key
            ),
            fetchone=True
        )

    except Exception as e:
        print("get_confirmed_fact error:", repr(e))
        return None


def get_latest_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    try:
        return db_execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND fact_key = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                chat_id,
                fact_key
            ),
            fetchone=True
        )

    except Exception as e:
        print("get_latest_fact error:", repr(e))
        return None


def create_fact(
    chat_id,
    fact_key,
    fact_value,
    status="PENDING",
    source_message_id=None,
    source="user"
):
    fact_key = normalize_fact_key(fact_key)
    fact_value = clean_text(fact_value)

    if not fact_key or not fact_value:
        return None

    if status not in VALID_FACT_STATUSES:
        status = "PENDING"

    try:
        confirmed = get_confirmed_fact(
            chat_id,
            fact_key
        )

        # Same confirmed fact: no duplicate.
        if confirmed:
            if clean_text(
                confirmed["fact_value"]
            ).lower() == fact_value.lower():
                return confirmed

            # Different value = conflict.
            if status != "CONFIRMED":
                row = db_execute(
                    """
                    INSERT INTO facts
                        (
                            chat_id,
                            fact_key,
                            fact_value,
                            status,
                            source_message_id,
                            source,
                            updated_at
                        )
                    VALUES
                        (
                            %s, %s, %s, 'CONFLICT',
                            %s, %s, CURRENT_TIMESTAMP
                        )
                    RETURNING *
                    """,
                    (
                        chat_id,
                        fact_key,
                        fact_value,
                        source_message_id,
                        source
                    ),
                    fetchone=True
                )

                if row:
                    db_execute(
                        """
                        INSERT INTO fact_history
                            (
                                fact_id,
                                chat_id,
                                old_status,
                                new_status,
                                note
                            )
                        VALUES
                            (
                                %s, %s, %s, %s, %s
                            )
                        """,
                        (
                            row["id"],
                            chat_id,
                            "CONFIRMED",
                            "CONFLICT",
                            "Different value received."
                        )
                    )

                return row

        # Check same pending fact.
        pending = db_execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND fact_key = %s
            AND fact_value = %s
            AND status = 'PENDING'
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                chat_id,
                fact_key,
                fact_value
            ),
            fetchone=True
        )

        if pending and status == "PENDING":
            return pending

        row = db_execute(
            """
            INSERT INTO facts
                (
                    chat_id,
                    fact_key,
                    fact_value,
                    status,
                    source_message_id,
                    source,
                    created_at,
                    updated_at
                )
            VALUES
                (
                    %s, %s, %s, %s,
                    %s, %s,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            RETURNING *
            """,
            (
                chat_id,
                fact_key,
                fact_value,
                status,
                source_message_id,
                source
            ),
            fetchone=True
        )

        if row:
            db_execute(
                """
                INSERT INTO fact_history
                    (
                        fact_id,
                        chat_id,
                        old_status,
                        new_status,
                        note
                    )
                VALUES
                    (
                        %s, %s, %s, %s, %s
                    )
                """,
                (
                    row["id"],
                    chat_id,
                    None,
                    status,
                    "Fact created."
                )
            )

        return row

    except Exception as e:
        print("create_fact error:", repr(e))
        return None


def get_facts(
    chat_id,
    status=None,
    limit=MAX_FACTS
):
    try:
        if status:
            return db_execute(
                """
                SELECT *
                FROM facts
                WHERE chat_id = %s
                AND status = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (
                    chat_id,
                    status,
                    limit
                ),
                fetch=True
            ) or []

        return db_execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            ),
            fetch=True
        ) or []

    except Exception as e:
        print("get_facts error:", repr(e))
        return []


def confirm_fact(chat_id, fact_id):
    try:
        row = db_execute(
            """
            SELECT *
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
            return None

        db_execute(
            """
            UPDATE facts
            SET status = 'CONFIRMED',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            AND chat_id = %s
            """,
            (
                fact_id,
                chat_id
            )
        )

        db_execute(
            """
            INSERT INTO fact_history
                (
                    fact_id,
                    chat_id,
                    old_status,
                    new_status,
                    note
                )
            VALUES
                (
                    %s, %s, %s, %s, %s
                )
            """,
            (
                fact_id,
                chat_id,
                row["status"],
                "CONFIRMED",
                "Confirmed by user."
            )
        )

        return get_latest_fact(
            chat_id,
            row["fact_key"]
        )

    except Exception as e:
        print("confirm_fact error:", repr(e))
        return None


def reject_fact(chat_id, fact_id):
    try:
        row = db_execute(
            """
            SELECT *
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
            return None

        db_execute(
            """
            UPDATE facts
            SET status = 'REJECTED',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            AND chat_id = %s
            """,
            (
                fact_id,
                chat_id
            )
        )

        db_execute(
            """
            INSERT INTO fact_history
                (
                    fact_id,
                    chat_id,
                    old_status,
                    new_status,
                    note
                )
            VALUES
                (
                    %s, %s, %s, %s, %s
                )
            """,
            (
                fact_id,
                chat_id,
                row["status"],
                "REJECTED",
                "Rejected by user."
            )
        )

        return True

    except Exception as e:
        print("reject_fact error:", repr(e))
        return False


# ============================================================
# COMMAND / MEMORY DETECTION
# ============================================================

def should_save_memory(text):
    text = clean_text(text).lower()

    keywords = [
        "remember",
        "memorize",
        "save this",
        "keep this",
        "remember this",
        "დაიმახსოვრე",
        "დამიმახსოვრე",
        "შეინახე",
        "დაიტოვე მეხსიერებაში",
        "მნიშვნელოვანია რომ იცოდე",
        "ეს ინფორმაცია დაიმახსოვრე",
        "მომავალში გახსოვდეს",
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def is_confirmed_fact_request(text):
    text_lower = clean_text(text).lower()

    indicators = [
        "confirmed fact",
        "confirm this fact",
        "confirmed",
        "დადასტურებული ფაქტი",
        "დადასტურებულ ფაქტად",
        "დაადასტურე როგორც ფაქტი",
        "როგორც confirmed fact"
    ]

    return any(
        item in text_lower
        for item in indicators
    )


def should_extract_facts(text):
    text_lower = clean_text(text).lower()

    indicators = [
        "დაიმახსოვრე",
        "დამიმახსოვრე",
        "შეინახე",
        "ჩემი კომპანია",
        "ჩვენი კომპანია",
        "ჩემი პროექტი",
        "ჩვენი პროექტი",
        "კომპანიის id",
        "company id",
        "my company",
        "our company",
        "my project",
        "our project",
        "confirmed fact",
        "დადასტურებული ფაქტი"
    ]

    return (
        len(text_lower) >= 10
        and any(
            item in text_lower
            for item in indicators
        )
    )


# ============================================================
# DIRECT CONFIRMED FACT PARSER
# ============================================================

def capture_direct_confirmed_company_fact(
    chat_id,
    text,
    message_id
):
    """
    Handles the important case:

    "ჩემი კომპანიის სახელია SAMTISI CONSTRUCTION LLC.
     დაიმახსოვრე ეს როგორც CONFIRMED FACT."

    We do this directly instead of relying only on Gemini.
    """

    pattern = re.search(
        r"(?:ჩემი|ჩვენი)\s+კომპანიის\s+სახელია\s+(.+?)(?:\.|$)",
        text,
        re.IGNORECASE
    )

    if not pattern:
        return None

    company_name = clean_text(
        pattern.group(1)
    )

    if not company_name:
        return None

    status = (
        "CONFIRMED"
        if is_confirmed_fact_request(text)
        else "PENDING"
    )

    return create_fact(
        chat_id=chat_id,
        fact_key="company_name",
        fact_value=company_name,
        status=status,
        source_message_id=message_id,
        source="direct_user_statement"
    )


# ============================================================
# GEMINI
# ============================================================

GENIOSA_SYSTEM = """
You are Geniosa, a persistent personal business advisor and
economic/project assistant.

CORE CONSTITUTION:

1. Never invent facts.
2. Never present an assumption as a confirmed fact.
3. Clearly distinguish:
   - CONFIRMED FACT
   - USER INFORMATION
   - ESTIMATE
   - ASSUMPTION
   - CALCULATION
   - OPINION
   - RESEARCH
4. Confirmed facts stored in the database have priority.
5. If two confirmed facts conflict, explicitly say there is a conflict.
6. Never silently replace a confirmed fact.
7. If information is missing, say it is missing.
8. Do not pretend that an unavailable calculation has been made.
9. When current information is required, use web research if available.
10. Financial calculations must show assumptions and formulas where useful.
11. Do not fabricate prices, laws, investors, companies, regulations,
    construction costs, market data or financial results.
12. Geniosa is the user's business advisor and assistant.
13. Be practical and decision-oriented.
14. Answer in the user's language unless another language is requested.
15. Georgian is the default language.
16. When asked about stored memory, rely on the supplied database context,
    not imagination.
17. If the database does not contain something, explicitly say:
    "ეს ინფორმაცია ჩემს შენახულ მეხსიერებაში არ არის."
18. Do not claim to remember something unless it is actually present
    in the supplied persistent context.
19. If the user explicitly asks to remember something, the application
    handles storage separately. Your task is to use the resulting context.
"""


def gemini_url(model):
    return (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        f"{model}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )


def call_gemini(
    contents,
    system_instruction=None,
    use_search=False
):
    if not GEMINI_API_KEY:
        return {
            "ok": False,
            "text": "",
            "error": "GEMINI_API_KEY is not configured.",
            "sources": []
        }

    if not contents:
        return {
            "ok": False,
            "text": "",
            "error": "No Gemini contents.",
            "sources": []
        }

    system_instruction = (
        system_instruction
        or GENIOSA_SYSTEM
    )

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": system_instruction
                }
            ]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 4096
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
            gemini_url(GEMINI_MODEL),
            json=payload,
            timeout=GEMINI_TIMEOUT
        )

        if response.status_code != 200:
            error_text = response.text[:3000]

            return {
                "ok": False,
                "text": "",
                "error": (
                    f"Gemini HTTP {response.status_code}: "
                    f"{error_text}"
                ),
                "sources": []
            }

        data = response.json()

        candidates = data.get(
            "candidates",
            []
        )

        if not candidates:
            return {
                "ok": False,
                "text": "",
                "error": "Gemini returned no candidates.",
                "sources": []
            }

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        texts = []

        for part in parts:
            if "text" in part:
                texts.append(
                    part["text"]
                )

        answer = "\n".join(
            texts
        ).strip()

        if not answer:
            return {
                "ok": False,
                "text": "",
                "error": "Gemini returned an empty answer.",
                "sources": []
            }

        return {
            "ok": True,
            "text": answer,
            "error": "",
            "sources": []
        }

    except requests.RequestException as e:
        return {
            "ok": False,
            "text": "",
            "error": f"Gemini request error: {repr(e)}",
            "sources": []
        }

    except Exception as e:
        return {
            "ok": False,
            "text": "",
            "error": f"Gemini exception: {repr(e)}",
            "sources": []
        }


# ============================================================
# FACT EXTRACTION
# ============================================================

def extract_json_object(text):
    text = clean_text(text)

    if not text:
        return None

    # Remove markdown code fences.
    text = re.sub(
        r"```json",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"```",
        "",
        text
    )

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end > start:
        candidate = text[start:end + 1]

        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None


def extract_facts_from_user_text(
    chat_id,
    text,
    message_id
):
    if not should_extract_facts(text):
        return []

    prompt = f"""
Extract only explicit factual information stated by the user.

Do NOT infer.
Do NOT calculate.
Do NOT invent.
Do NOT transform estimates into facts.

Return ONLY valid JSON in this exact form:

{{
  "facts": [
    {{
      "key": "short_stable_key",
      "value": "exact factual value"
    }}
  ]
}}

User text:
{text}
"""

    result = call_gemini(
        contents=[
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        system_instruction=(
            "Extract explicit facts only. "
            "Return JSON only. Never infer."
        ),
        use_search=False
    )

    if not result["ok"]:
        print(
            "FACT EXTRACTION GEMINI ERROR:",
            result["error"]
        )
        return []

    data = extract_json_object(
        result["text"]
    )

    if not isinstance(data, dict):
        return []

    facts = data.get(
        "facts",
        []
    )

    if not isinstance(facts, list):
        return []

    saved = []

    force_confirmed = is_confirmed_fact_request(
        text
    )

    for item in facts:
        if not isinstance(item, dict):
            continue

        key = clean_text(
            item.get("key", "")
        )

        value = clean_text(
            item.get("value", "")
        )

        if not key or not value:
            continue

        status = (
            "CONFIRMED"
            if force_confirmed
            else "PENDING"
        )

        row = create_fact(
            chat_id=chat_id,
            fact_key=key,
            fact_value=value,
            status=status,
            source_message_id=message_id,
            source="gemini_fact_extraction"
        )

        if row:
            saved.append(row)

    return saved


# ============================================================
# MEMORY CAPTURE
# ============================================================

def capture_memory(
    chat_id,
    text,
    message_id=None
):
    text = clean_text(text)

    if not text:
        return {
            "memory_saved": False,
            "facts_saved": []
        }

    memory_saved = False

    # Explicit memory request.
    if should_save_memory(text):
        memory_saved = save_memory(
            chat_id,
            text
        )

        if memory_saved:
            save_memory_event(
                chat_id,
                "MEMORY_SAVED",
                text
            )

    # Directly handle the most important company case.
    direct_fact = (
        capture_direct_confirmed_company_fact(
            chat_id,
            text,
            message_id
        )
    )

    facts_saved = []

    if direct_fact:
        facts_saved.append(
            direct_fact
        )

    # Generic fact extraction.
    extracted = extract_facts_from_user_text(
        chat_id,
        text,
        message_id
    )

    existing_ids = {
        row["id"]
        for row in facts_saved
        if row and row.get("id")
    }

    for row in extracted:
        if row and row.get("id") not in existing_ids:
            facts_saved.append(row)

    return {
        "memory_saved": memory_saved,
        "facts_saved": facts_saved
    }


# ============================================================
# CONTEXT
# ============================================================

def format_context(chat_id):
    sections = []

    # --------------------------------------------------------
    # Confirmed facts
    # --------------------------------------------------------

    confirmed = get_facts(
        chat_id,
        status="CONFIRMED",
        limit=MAX_FACTS
    )

    if confirmed:
        lines = [
            "CONFIRMED FACTS:"
        ]

        for fact in reversed(confirmed):
            lines.append(
                f"- {fact['fact_key']}: "
                f"{fact['fact_value']}"
            )

        sections.append(
            "\n".join(lines)
        )

    # --------------------------------------------------------
    # Pending/conflict facts
    # --------------------------------------------------------

    non_confirmed = get_facts(
        chat_id,
        status=None,
        limit=MAX_FACTS
    )

    non_confirmed = [
        f
        for f in non_confirmed
        if f["status"] != "CONFIRMED"
    ]

    if non_confirmed:
        lines = [
            "NON-CONFIRMED FACT RECORDS "
            "(DO NOT PRESENT AS CONFIRMED):"
        ]

        for fact in reversed(non_confirmed):
            lines.append(
                f"- [{fact['status']}] "
                f"{fact['fact_key']}: "
                f"{fact['fact_value']}"
            )

        sections.append(
            "\n".join(lines)
        )

    # --------------------------------------------------------
    # Memories
    # --------------------------------------------------------

    memories = get_memories(
        chat_id,
        limit=MAX_MEMORIES
    )

    if memories:
        lines = [
            "PERSISTENT MEMORIES:"
        ]

        for memory in reversed(memories):
            lines.append(
                f"- {memory['memory']}"
            )

        sections.append(
            "\n".join(lines)
        )

    # --------------------------------------------------------
    # Recent conversation
    # --------------------------------------------------------

    messages = get_recent_messages(
        chat_id,
        limit=MAX_MESSAGES
    )

    if messages:
        lines = [
            "RECENT CONVERSATION:"
        ]

        for msg in messages:
            role = msg.get(
                "role",
                "unknown"
            )

            message = clean_text(
                msg.get("message", "")
            )

            if message:
                lines.append(
                    f"{role}: {message}"
                )

        sections.append(
            "\n".join(lines)
        )

    if not sections:
        return (
            "No persistent context is currently stored "
            "for this chat."
        )

    return "\n\n".join(
        sections
    )


# ============================================================
# NIKKEA 12 / SAMTISI SEED DATA
# ============================================================

def seed_confirmed_fact(
    chat_id,
    fact_key,
    fact_value,
    source="system_seed"
):
    existing = get_confirmed_fact(
        chat_id,
        fact_key
    )

    if existing:
        return

    create_fact(
        chat_id=chat_id,
        fact_key=fact_key,
        fact_value=fact_value,
        status="CONFIRMED",
        source_message_id=None,
        source=source
    )


def seed_known_project_data(chat_id):
    """
    These are the core project facts previously established
    for Geniosa. They are stored as CONFIRMED so that Geniosa
    does not depend on the model merely 'remembering' them.
    """

    # --------------------------------------------------------
    # Company
    # --------------------------------------------------------

    seed_confirmed_fact(
        chat_id,
        "company_name",
        "SAMTISI CONSTRUCTION LLC"
    )

    seed_confirmed_fact(
        chat_id,
        "company_id",
        "406391202"
    )

    seed_confirmed_fact(
        chat_id,
        "company_type",
        "Construction & Development Company"
    )

    seed_confirmed_fact(
        chat_id,
        "company_head_office",
        "Tbilisi, Georgia"
    )

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_location",
        "Kutaisi, Nikkea 12"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_land_area_m2",
        "3070 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_total_saleable_area_m2",
        "10854 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_hotel_rooms_area_m2",
        "7212 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_monolith_area_m2",
        "21500 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_architecture_area_m2",
        "16000 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_first_floor_commercial_m2",
        "836 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_second_floor_commercial_m2",
        "1006 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_apartments_hotel_rooms_floors",
        "Floors 3–14, total 7212 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_landowner_requirement",
        "1800 m² apartments + 25 parking spaces + $300,000 cash"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_construction_price_monolith",
        "$170/m²"
    )

    # --------------------------------------------------------
    # JV structure
    # --------------------------------------------------------

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_development_jv",
        "Investor 80% / SAMTISI operator 20%"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_investor_contribution_target",
        "35% of total project cost + $300,000"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_bank_financing_assumption",
        "Remaining financing may be provided by bank"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_bank_interest_assumption",
        "11.5%–13.5% maximum assumption"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_property_service_company",
        "Separate property/service company, 50% investor / 50% SAMTISI"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_property_company_rental_split",
        "30% property/service company / 70% owner"
    )

    # --------------------------------------------------------
    # Sales / casino
    # --------------------------------------------------------

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_apartment_price_assumption",
        "$1500–$1800/m² minimum"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_commercial_price_assumption",
        "$2500/m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_fitout_cost_assumption",
        "$450/m² ceiling assumption for remaining 16000 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_casino_area",
        "1006 m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_casino_sale_status",
        "Casino area of 1006 m² is intended to be retained, not sold"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_casino_value_target",
        "At least $2000/m²"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_casino_rent_target",
        "At least $50/m²/month target"
    )

    seed_confirmed_fact(
        chat_id,
        "nikkea_12_apartment_daily_rent_target",
        "$150–$300 per apartment per day"
    )


# ============================================================
# MEMORY DISPLAY
# ============================================================

def format_memory_for_telegram(chat_id):
    confirmed = get_facts(
        chat_id,
        status="CONFIRMED",
        limit=MAX_FACTS
    )

    memories = get_memories(
        chat_id,
        limit=MAX_MEMORIES
    )

    pending = get_facts(
        chat_id,
        status="PENDING",
        limit=MAX_FACTS
    )

    conflicts = get_facts(
        chat_id,
        status="CONFLICT",
        limit=MAX_FACTS
    )

    lines = [
        "🧠 GENIOSA MEMORY",
        ""
    ]

    if confirmed:
        lines.append(
            "✅ CONFIRMED FACTS:"
        )

        for fact in reversed(confirmed):
            lines.append(
                f"• {fact['fact_key']} = "
                f"{fact['fact_value']}"
            )

        lines.append("")

    if memories:
        lines.append(
            "💾 MEMORIES:"
        )

        for memory in reversed(memories):
            lines.append(
                f"• {memory['memory']}"
            )

        lines.append("")

    if pending:
        lines.append(
            "🟡 PENDING FACTS:"
        )

        for fact in reversed(pending):
            lines.append(
                f"• #{fact['id']} "
                f"{fact['fact_key']} = "
                f"{fact['fact_value']}"
            )

        lines.append(
            "გამოიყენე /confirm_fact ID "
            "დასადასტურებლად."
        )

        lines.append("")

    if conflicts:
        lines.append(
            "⚠️ CONFLICTS:"
        )

        for fact in reversed(conflicts):
            lines.append(
                f"• #{fact['id']} "
                f"{fact['fact_key']} = "
                f"{fact['fact_value']}"
            )

        lines.append("")

    if (
        not confirmed
        and not memories
        and not pending
        and not conflicts
    ):
        lines.append(
            "მეხსიერება ამ chat-ისთვის ცარიელია."
        )

    return "\n".join(
        lines
    )


# ============================================================
# DECISIONS
# ============================================================

def should_save_decision(text):
    text = clean_text(text).lower()

    keywords = [
        "გადავწყვიტეთ",
        "გადავწყვიტე",
        "ჩვენი გადაწყვეტილებაა",
        "decision:",
        "we decided",
        "i decided"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def save_decision(chat_id, text):
    try:
        db_execute(
            """
            INSERT INTO decisions
                (chat_id, decision)
            VALUES
                (%s, %s)
            """,
            (
                chat_id,
                clean_text(text)
            )
        )

        return True

    except Exception as e:
        print("save_decision error:", repr(e))
        return False


# ============================================================
# TASKS
# ============================================================

def should_save_task(text):
    text = clean_text(text).lower()

    keywords = [
        "დავალება",
        "დავალებად",
        "უნდა გავაკეთოთ",
        "უნდა გავაკეთო",
        "task:",
        "todo:",
        "we need to do",
        "i need to do"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def save_task(chat_id, text):
    try:
        db_execute(
            """
            INSERT INTO tasks
                (chat_id, task, status)
            VALUES
                (%s, %s, 'OPEN')
            """,
            (
                chat_id,
                clean_text(text)
            )
        )

        return True

    except Exception as e:
        print("save_task error:", repr(e))
        return False


def get_tasks(chat_id, status=None):
    try:
        if status:
            return db_execute(
                """
                SELECT *
                FROM tasks
                WHERE chat_id = %s
                AND status = %s
                ORDER BY id DESC
                """,
                (
                    chat_id,
                    status
                ),
                fetch=True
            ) or []

        return db_execute(
            """
            SELECT *
            FROM tasks
            WHERE chat_id = %s
            ORDER BY id DESC
            """,
            (
                chat_id,
            ),
            fetch=True
        ) or []

    except Exception as e:
        print("get_tasks error:", repr(e))
        return []


# ============================================================
# PROJECTS
# ============================================================

def save_project(
    chat_id,
    name,
    description=""
):
    try:
        row = db_execute(
            """
            INSERT INTO projects
                (chat_id, name, description)
            VALUES
                (%s, %s, %s)
            RETURNING *
            """,
            (
                chat_id,
                clean_text(name),
                clean_text(description)
            ),
            fetchone=True
        )

        return row

    except Exception as e:
        print("save_project error:", repr(e))
        return None


def get_projects(chat_id):
    try:
        return db_execute(
            """
            SELECT *
            FROM projects
            WHERE chat_id = %s
            ORDER BY id DESC
            """,
            (
                chat_id,
            ),
            fetch=True
        ) or []

    except Exception as e:
        print("get_projects error:", repr(e))
        return []


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )


def telegram_call(
    method,
    payload=None,
    timeout=TELEGRAM_TIMEOUT
):
    if not TELEGRAM_BOT_TOKEN:
        return {
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN is not configured."
        }

    try:
        response = requests.post(
            telegram_url(method),
            json=payload or {},
            timeout=timeout
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "ok": False,
                "description": response.text[:1000]
            }

        if response.status_code != 200:
            return {
                "ok": False,
                "error": (
                    f"Telegram HTTP "
                    f"{response.status_code}: "
                    f"{data}"
                )
            }

        return data

    except Exception as e:
        return {
            "ok": False,
            "error": repr(e)
        }


def send_message(
    chat_id,
    text,
    reply_to_message_id=None
):
    text = clean_text(text)

    if not text:
        return False

    chunks = []

    while len(text) > MAX_TELEGRAM_LENGTH:
        split_at = text.rfind(
            "\n",
            0,
            MAX_TELEGRAM_LENGTH
        )

        if split_at < 1000:
            split_at = MAX_TELEGRAM_LENGTH

        chunks.append(
            text[:split_at]
        )

        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    success = True

    for index, chunk in enumerate(chunks):

        payload = {
            "chat_id": chat_id,
            "text": chunk
        }

        if (
            reply_to_message_id
            and index == 0
        ):
            payload[
                "reply_parameters"
            ] = {
                "message_id": reply_to_message_id
            }

        result = telegram_call(
            "sendMessage",
            payload
        )

        if not result.get("ok"):
            print(
                "send_message error:",
                result
            )
            success = False

            # Retry once.
            time.sleep(1)

            retry = telegram_call(
                "sendMessage",
                payload
            )

            if not retry.get("ok"):
                success = False

    return success


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def command_help(chat_id):
    text = """
🤖 GENIOSA COMMANDS

/memory
აჩვენებს შენახულ მეხსიერებას და CONFIRMED FACT-ებს.

/facts
აჩვენებს ყველა ფაქტს სტატუსით.

/confirm_fact ID
ადასტურებს კონკრეტულ ფაქტს.

/reject_fact ID
უარყოფს კონკრეტულ ფაქტს.

/tasks
აჩვენებს დავალებებს.

/projects
აჩვენებს პროექტებს.

/health
აჩვენებს Geniosa-ს ტექნიკურ მდგომარეობას.

/start
იწყებს Geniosa-ს.

უბრალოდ მომწერე ჩვეულებრივი ტექსტიც.
მე ვიყენებ PostgreSQL მეხსიერებას და Gemini-ს.
""".strip()

    send_message(
        chat_id,
        text
    )


def command_facts(chat_id):
    facts = get_facts(
        chat_id,
        status=None,
        limit=MAX_FACTS
    )

    if not facts:
        send_message(
            chat_id,
            "📋 ფაქტები ჯერ არ არის შენახული."
        )
        return

    lines = [
        "📋 FACTS",
        ""
    ]

    for fact in reversed(facts):
        lines.append(
            f"#{fact['id']} "
            f"[{fact['status']}] "
            f"{fact['fact_key']} = "
            f"{fact['fact_value']}"
        )

    send_message(
        chat_id,
        "\n".join(lines)
    )


def command_confirm_fact(
    chat_id,
    command_text
):
    parts = command_text.split()

    if len(parts) < 2:
        send_message(
            chat_id,
            "ფორმატი:\n/confirm_fact ID"
        )
        return

    fact_id = safe_int(
        parts[1],
        0
    )

    if not fact_id:
        send_message(
            chat_id,
            "❌ ID არასწორია."
        )
        return

    row = confirm_fact(
        chat_id,
        fact_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ ასეთი ფაქტი ვერ მოიძებნა."
        )
        return

    send_message(
        chat_id,
        (
            "✅ FACT CONFIRMED\n\n"
            f"{row['fact_key']} = "
            f"{row['fact_value']}"
        )
    )


def command_reject_fact(
    chat_id,
    command_text
):
    parts = command_text.split()

    if len(parts) < 2:
        send_message(
            chat_id,
            "ფორმატი:\n/reject_fact ID"
        )
        return

    fact_id = safe_int(
        parts[1],
        0
    )

    if not fact_id:
        send_message(
            chat_id,
            "❌ ID არასწორია."
        )
        return

    ok = reject_fact(
        chat_id,
        fact_id
    )

    if not ok:
        send_message(
            chat_id,
            "❌ ფაქტის უარყოფა ვერ მოხერხდა."
        )
        return

    send_message(
        chat_id,
        f"🗑 FACT #{fact_id} უარყოფილია."
    )


def command_tasks(chat_id):
    tasks = get_tasks(
        chat_id
    )

    if not tasks:
        send_message(
            chat_id,
            "📋 დავალებები არ არის."
        )
        return

    lines = [
        "📋 TASKS",
        ""
    ]

    for task in reversed(tasks):
        lines.append(
            f"#{task['id']} "
            f"[{task['status']}] "
            f"{task['task']}"
        )

    send_message(
        chat_id,
        "\n".join(lines)
    )


def command_projects(chat_id):
    projects = get_projects(
        chat_id
    )

    if not projects:
        send_message(
            chat_id,
            "📁 პროექტები არ არის."
        )
        return

    lines = [
        "📁 PROJECTS",
        ""
    ]

    for project in reversed(projects):
        lines.append(
            f"#{project['id']} "
            f"{project['name']}"
        )

        if project["description"]:
            lines.append(
                f"   {project['description']}"
            )

    send_message(
        chat_id,
        "\n".join(lines)
    )


def command_health(chat_id):
    db_ok = False
    db_error = ""

    try:
        db_execute(
            "SELECT 1",
            fetchone=True
        )

        db_ok = True

    except Exception as e:
        db_error = repr(e)

    telegram_ok = bool(
        TELEGRAM_BOT_TOKEN
    )

    gemini_ok = bool(
        GEMINI_API_KEY
    )

    text = (
        "🩺 GENIOSA HEALTH\n\n"
        f"Version: {APP_VERSION}\n"
        f"Database: "
        f"{'OK' if db_ok else 'ERROR'}\n"
        f"Telegram token: "
        f"{'SET' if telegram_ok else 'MISSING'}\n"
        f"Gemini key: "
        f"{'SET' if gemini_ok else 'MISSING'}\n"
        f"Gemini model: {GEMINI_MODEL}\n"
        f"Polling started: {polling_started}"
    )

    if db_error:
        text += (
            "\n\nDB error:\n"
            + db_error[:1000]
        )

    send_message(
        chat_id,
        text
    )


# ============================================================
# COMMAND ROUTER
# ============================================================

def handle_command(
    chat_id,
    text
):
    command = text.split()[0].lower()

    if command == "/start":
        send_message(
            chat_id,
            (
                "🤖 გამარჯობა. მე ვარ Geniosa.\n\n"
                "მე ვარ შენი პირადი ბიზნეს-მრჩეველი "
                "და პროექტების ასისტენტი.\n\n"
                "გამოიყენე /help "
                "ხელმისაწვდომი ბრძანებების სანახავად."
            )
        )
        return True

    if command == "/help":
        command_help(chat_id)
        return True

    if command == "/memory":
        send_message(
            chat_id,
            format_memory_for_telegram(
                chat_id
            )
        )
        return True

    if command == "/facts":
        command_facts(chat_id)
        return True

    if command == "/confirm_fact":
        command_confirm_fact(
            chat_id,
            text
        )
        return True

    if command == "/reject_fact":
        command_reject_fact(
            chat_id,
            text
        )
        return True

    if command == "/tasks":
        command_tasks(chat_id)
        return True

    if command == "/projects":
        command_projects(chat_id)
        return True

    if command == "/health":
        command_health(chat_id)
        return True

    return False


# ============================================================
# WEB SEARCH DETECTION
# ============================================================

def needs_web_search(text):
    text_lower = clean_text(
        text
    ).lower()

    keywords = [
        "დღეს",
        "ახლა",
        "უახლესი",
        "ბოლო ინფორმაცია",
        "current",
        "latest",
        "today",
        "now",
        "price today",
        "market price",
        "კანონი",
        "კანონმდებლობა",
        "რეგულაცია",
        "law",
        "regulation",
        "ინვესტორი",
        "investor",
        "company",
        "კომპანია",
        "ფასი",
        "ფასები",
        "cost",
        "construction cost",
        "სამშენებლო ღირებულება"
    ]

    return any(
        item in text_lower
        for item in keywords
    )


# ============================================================
# MAIN MESSAGE PROCESSOR
# ============================================================

def process_user_message(
    chat_id,
    text,
    telegram_message_id=None
):
    text = clean_text(text)

    if not text:
        return

    print(
        f"PROCESS MESSAGE chat={chat_id}: "
        f"{text[:200]}"
    )

    # --------------------------------------------------------
    # Save incoming user message FIRST.
    # --------------------------------------------------------

    message_id = save_message(
        chat_id,
        "user",
        text
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Persistent memory capture happens on every message.
    # --------------------------------------------------------

    capture_result = capture_memory(
        chat_id,
        text,
        message_id
    )

    if should_save_decision(text):
        save_decision(
            chat_id,
            text
        )

    if should_save_task(text):
        save_task(
            chat_id,
            text
        )

    # --------------------------------------------------------
    # Seed core known data for this chat.
    # This guarantees NIKKEA/SAMTISI context exists.
    # --------------------------------------------------------

    try:
        seed_known_project_data(
            chat_id
        )
    except Exception as e:
        print(
            "seed_known_project_data error:",
            repr(e)
        )

    # --------------------------------------------------------
    # Build persistent context.
    # --------------------------------------------------------

    context = format_context(
        chat_id
    )

    # --------------------------------------------------------
    # User request.
    # --------------------------------------------------------

    user_prompt = f"""
PERSISTENT DATABASE CONTEXT
============================

{context}

============================
CURRENT USER MESSAGE
============================

{text}

============================

Answer the current user message.

Important:
- Use the persistent context above.
- Confirmed facts are authoritative.
- Never invent missing information.
- If something is only an estimate, label it.
- If information is absent from memory, say so.
"""

    result = call_gemini(
        contents=[
            {
                "role": "user",
                "parts": [
                    {
                        "text": user_prompt
                    }
                ]
            }
        ],
        system_instruction=GENIOSA_SYSTEM,
        use_search=needs_web_search(text)
    )

    if not result["ok"]:
        print(
            "GEMINI ERROR:",
            result["error"]
        )

        answer = (
            "⚠️ ტექნიკური პრობლემა მაქვს Gemini-სთან "
            "დაკავშირებისას.\n\n"
            "მონაცემი არ უნდა მოვიგონო, ამიტომ პასუხს "
            "ამ ეტაპზე ვერ გაგცემ.\n\n"
            f"Technical detail: "
            f"{result['error'][:500]}"
        )

    else:
        answer = result["text"]

    # --------------------------------------------------------
    # Save assistant response.
    # --------------------------------------------------------

    save_message(
        chat_id,
        "assistant",
        answer
    )

    # --------------------------------------------------------
    # Send response.
    # --------------------------------------------------------

    send_message(
        chat_id,
        answer,
        reply_to_message_id=telegram_message_id
    )


# ============================================================
# TELEGRAM UPDATE PROCESSING
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

    if chat_id is None:
        return

    # Only text messages for now.
    text = message.get(
        "text"
    )

    if text is None:
        return

    text = clean_text(
        text
    )

    if not text:
        return

    # --------------------------------------------------------
    # Optional owner protection.
    # If GENIOSA_OWNER_ID is set, only owner can use bot.
    # --------------------------------------------------------

    if not is_owner(chat_id):
        send_message(
            chat_id,
            "⛔ ეს Geniosa-ს პირადი ბიზნეს ასისტენტია."
        )
        return

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    if text.startswith("/"):
        handled = handle_command(
            chat_id,
            text
        )

        if handled:
            return

    # --------------------------------------------------------
    # Normal message
    # --------------------------------------------------------

    process_user_message(
        chat_id,
        text,
        telegram_message_id=message.get(
            "message_id"
        )
    )


# ============================================================
# TELEGRAM POLLING LOCK
# ============================================================

def acquire_polling_lock():
    global polling_connection

    try:
        polling_connection = get_db()

        with polling_connection.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (
                    847291347,
                )
            )

            row = cur.fetchone()

        if not row or not row[0]:
            print(
                "Another Geniosa polling instance "
                "already owns the lock."
            )

            polling_connection.close()
            polling_connection = None

            return False

        print(
            "Telegram polling advisory lock acquired."
        )

        return True

    except Exception as e:
        print(
            "Polling lock error:",
            repr(e)
        )

        if polling_connection:
            try:
                polling_connection.close()
            except Exception:
                pass

        polling_connection = None

        return False


def release_polling_lock():
    global polling_connection

    if not polling_connection:
        return

    try:
        with polling_connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)",
                (
                    847291347,
                )
            )

        polling_connection.commit()

    except Exception as e:
        print(
            "Release polling lock error:",
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

def polling_loop():
    global polling_started

    print(
        f"GENIOSA {APP_VERSION} "
        "polling loop starting..."
    )

    if not TELEGRAM_BOT_TOKEN:
        print(
            "ERROR: TELEGRAM_BOT_TOKEN missing."
        )
        return

    if not GEMINI_API_KEY:
        print(
            "WARNING: GEMINI_API_KEY missing."
        )

    if not DATABASE_URL:
        print(
            "ERROR: DATABASE_URL missing."
        )
        return

    if not acquire_polling_lock():
        return

    polling_started = True

    offset = None

    # --------------------------------------------------------
    # Recover the latest offset from Telegram by using the
    # standard getUpdates flow.
    # --------------------------------------------------------

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

            result = telegram_call(
                "getUpdates",
                payload,
                timeout=40
            )

            if not result.get("ok"):
                print(
                    "getUpdates error:",
                    result
                )

                time.sleep(3)
                continue

            updates = result.get(
                "result",
                []
            )

            if not updates:
                continue

            for update in updates:

                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:
                    # Advance offset after this update is
                    # handed to processing.
                    offset = (
                        update_id + 1
                    )

                try:
                    process_update(
                        update
                    )

                except Exception as e:
                    print(
                        "process_update error:",
                        repr(e)
                    )

        except Exception as e:
            print(
                "Polling loop error:",
                repr(e)
            )

            time.sleep(
                POLL_INTERVAL + 2
            )


# ============================================================
# STARTUP
# ============================================================

def start_background_services():
    global polling_thread

    print(
        "============================================"
    )
    print(
        f"GENIOSA {APP_VERSION} STARTING"
    )
    print(
        "============================================"
    )

    print(
        "Telegram token:",
        "SET" if TELEGRAM_BOT_TOKEN else "MISSING"
    )

    print(
        "Gemini key:",
        "SET" if GEMINI_API_KEY else "MISSING"
    )

    print(
        "Gemini model:",
        GEMINI_MODEL
    )

    print(
        "Database:",
        "SET" if DATABASE_URL else "MISSING"
    )

    init_db()

    # Start only one local thread.
    with polling_state_lock:

        if (
            polling_thread
            and polling_thread.is_alive()
        ):
            print(
                "Polling thread already running."
            )
            return

        polling_thread = threading.Thread(
            target=polling_loop,
            name="geniosa-telegram-polling",
            daemon=True
        )

        polling_thread.start()

        print(
            "Telegram polling thread started."
        )


# ============================================================
# FASTAPI EVENTS
# ============================================================

@app.on_event("startup")
def startup_event():
    try:
        start_background_services()
    except Exception as e:
        print(
            "STARTUP ERROR:",
            repr(e)
        )


@app.on_event("shutdown")
def shutdown_event():
    global polling_started

    polling_started = False

    release_polling_lock()

    print(
        "GENIOSA shutdown complete."
    )


# ============================================================
# HTTP ENDPOINTS
# ============================================================

@app.get("/")
def root():
    return {
        "app": "Geniosa",
        "version": APP_VERSION,
        "status": "online",
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "gemini_model": GEMINI_MODEL,
        "database_configured": bool(
            DATABASE_URL
        ),
        "polling_started": polling_started
    }


@app.get("/health")
def health():
    db_ok = False
    db_error = None

    try:
        db_execute(
            "SELECT 1",
            fetchone=True
        )

        db_ok = True

    except Exception as e:
        db_error = repr(e)

    return {
        "app": "Geniosa",
        "version": APP_VERSION,
        "database": db_ok,
        "telegram": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini": bool(
            GEMINI_API_KEY
        ),
        "gemini_model": GEMINI_MODEL,
        "polling_started": polling_started,
        "database_error": db_error
    }


# ============================================================
# LOCAL ENTRY POINT
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
