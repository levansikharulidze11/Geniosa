# ============================================================
# GENIOSA 4.3
# Personal Business Advisor
# Fact Engine 2.0 + Developer Engine + Debug/Fix Engine
# Telegram + Gemini + PostgreSQL
# ============================================================

import os
import re
import ast
import time
import tempfile
import threading
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI


# ============================================================
# CONFIGURATION
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

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

OWNER_ID = os.getenv(
    "GENIOSA_OWNER_ID",
    ""
).strip()


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="Geniosa",
    version="4.3"
)


# ============================================================
# CONFIGURATION STATUS
# ============================================================

def configuration_status():
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append(
            "TELEGRAM_BOT_TOKEN"
        )

    if not GEMINI_API_KEY:
        missing.append(
            "GEMINI_API_KEY"
        )

    if not DATABASE_URL:
        missing.append(
            "DATABASE_URL"
        )

    return missing


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured"
        )

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_database():
    conn = get_db()
    cur = conn.cursor()

    # --------------------------------------------------------
    # MESSAGES
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # MEMORIES
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, memory_key)
        )
        """
    )

    # --------------------------------------------------------
    # PROJECTS
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, name)
        )
        """
    )

    # --------------------------------------------------------
    # LEGACY FACTS TABLE
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT DEFAULT 'USER',
            confidence TEXT DEFAULT 'pending',
            status TEXT DEFAULT 'PENDING',
            source_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # FACT ENGINE 2.0 MIGRATION
    # --------------------------------------------------------

    # Existing 4.2 installations may already have
    # UNIQUE(chat_id, fact_key).
    #
    # We intentionally remove that constraint because
    # Fact Engine 2.0 must preserve conflicting/history data
    # instead of silently replacing it.
    #
    # PostgreSQL allows us to inspect constraints safely.

    cur.execute(
        """
        DO $$
        DECLARE
            constraint_record RECORD;
        BEGIN
            FOR constraint_record IN
                SELECT
                    conname
                FROM pg_constraint
                WHERE conrelid = 'facts'::regclass
                  AND contype = 'u'
            LOOP
                EXECUTE
                    'ALTER TABLE facts DROP CONSTRAINT IF EXISTS '
                    || quote_ident(constraint_record.conname);
            END LOOP;
        END
        $$;
        """
    )

    # Add missing columns to old 4.2 database.

    cur.execute(
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS status TEXT
        """
    )

    cur.execute(
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS source_message_id INTEGER
        """
    )

    cur.execute(
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP
        """
    )

    cur.execute(
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMP
        """
    )

    cur.execute(
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS rejection_reason TEXT
        """
    )

    # IMPORTANT:
    # Existing 4.2 facts were automatically created.
    # User has now explicitly said they are NOT confirmed.
    #
    # Therefore any old record that has not been explicitly
    # confirmed through Fact Engine 2.0 becomes PENDING.

    cur.execute(
        """
        UPDATE facts
        SET status = 'PENDING'
        WHERE status IS NULL
           OR status = ''
           OR (
                confidence = 'confirmed'
                AND confirmed_at IS NULL
              )
        """
    )

    cur.execute(
        """
        UPDATE facts
        SET source = 'LEGACY_4_2_UNCONFIRMED'
        WHERE source = 'USER_CONFIRMED'
          AND confirmed_at IS NULL
        """
    )

    cur.execute(
        """
        UPDATE facts
        SET confidence = 'pending'
        WHERE status = 'PENDING'
          AND confirmed_at IS NULL
        """
    )

    # --------------------------------------------------------
    # FACT HISTORY
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id SERIAL PRIMARY KEY,
            fact_id INTEGER,
            chat_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            source_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --------------------------------------------------------
    # CODE PROJECTS
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS code_projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            language TEXT DEFAULT 'python',
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, name)
        )
        """
    )

    # --------------------------------------------------------
    # CODE VERSIONS
    # --------------------------------------------------------

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS code_versions (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            project_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            language TEXT DEFAULT 'python',
            code TEXT NOT NULL,
            version_number INTEGER DEFAULT 1,
            parent_version_id INTEGER,
            change_reason TEXT,
            syntax_ok BOOLEAN,
            syntax_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()

    cur.close()
    conn.close()


# ============================================================
# MESSAGE STORAGE
# ============================================================

def save_message(
    chat_id,
    role,
    text
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO messages
            (chat_id, role, text)
        VALUES
            (%s, %s, %s)
        RETURNING id
        """,
        (
            chat_id,
            role,
            text
        )
    )

    row = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    return row[0] if row else None


def get_recent_messages(
    chat_id,
    limit=20
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT
            id,
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
            limit
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    rows.reverse()

    return rows


# ============================================================
# MEMORY
# ============================================================

def save_memory(
    chat_id,
    memory_key,
    memory_value
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO memories
            (
                chat_id,
                memory_key,
                memory_value
            )
        VALUES
            (%s, %s, %s)
        ON CONFLICT (chat_id, memory_key)
        DO UPDATE SET
            memory_value = EXCLUDED.memory_value,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            chat_id,
            memory_key,
            memory_value
        )
    )

    conn.commit()

    cur.close()
    conn.close()


def get_memories(chat_id):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT
            memory_key,
            memory_value
        FROM memories
        WHERE chat_id = %s
        ORDER BY id
        """,
        (chat_id,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ============================================================
# PROJECTS
# ============================================================

def save_project(
    chat_id,
    name,
    description
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO projects
            (
                chat_id,
                name,
                description
            )
        VALUES
            (%s, %s, %s)
        ON CONFLICT (chat_id, name)
        DO UPDATE SET
            description = EXCLUDED.description,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            chat_id,
            name,
            description
        )
    )

    conn.commit()

    cur.close()
    conn.close()


def get_projects(chat_id):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT
            name,
            description,
            created_at,
            updated_at
        FROM projects
        WHERE chat_id = %s
        ORDER BY id
        """,
        (chat_id,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ============================================================
# FACT ENGINE 2.0
# ============================================================

FACT_STATUS_PENDING = "PENDING"
FACT_STATUS_CONFIRMED = "CONFIRMED"
FACT_STATUS_REJECTED = "REJECTED"
FACT_STATUS_CONFLICT = "CONFLICT"

FACT_TYPE_FACT = "FACT"
FACT_TYPE_ASSUMPTION = "ASSUMPTION"
FACT_TYPE_CALCULATED = "CALCULATED"
FACT_TYPE_SOURCE = "SOURCE"


def normalize_fact_key(value):
    value = str(value or "").strip().lower()

    value = re.sub(
        r"\s+",
        "_",
        value
    )

    value = re.sub(
        r"[^a-zA-Z0-9ა-ჰ_]+",
        "_",
        value
    )

    value = re.sub(
        r"_+",
        "_",
        value
    )

    return value.strip("_")


def save_fact_candidate(
    chat_id,
    fact_key,
    fact_value,
    source="USER_STATED",
    confidence="pending",
    status=FACT_STATUS_PENDING,
    source_message_id=None,
    reason="User-stated information awaiting confirmation."
):
    fact_key = normalize_fact_key(
        fact_key
    )

    if not fact_key:
        return None

    fact_value = str(
        fact_value
    ).strip()

    if not fact_value:
        return None

    conn = get_db()
    cur = conn.cursor()

    # --------------------------------------------------------
    # Check for an identical active record.
    # --------------------------------------------------------

    cur.execute(
        """
        SELECT
            id,
            status
        FROM facts
        WHERE chat_id = %s
          AND fact_key = %s
          AND fact_value = %s
          AND status IN ('PENDING', 'CONFIRMED')
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            chat_id,
            fact_key,
            fact_value
        )
    )

    existing = cur.fetchone()

    if existing:
        cur.close()
        conn.close()

        return {
            "id": existing[0],
            "status": existing[1],
            "existing": True
        }

    # --------------------------------------------------------
    # If another CONFIRMED value exists for same key,
    # do NOT overwrite it.
    # --------------------------------------------------------

    cur.execute(
        """
        SELECT
            id,
            fact_value
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
        )
    )

    confirmed = cur.fetchone()

    if confirmed:
        status = FACT_STATUS_CONFLICT

    cur.execute(
        """
        INSERT INTO facts
            (
                chat_id,
                fact_key,
                fact_value,
                source,
                confidence,
                status,
                source_message_id
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            chat_id,
            fact_key,
            fact_value,
            source,
            confidence,
            status,
            source_message_id
        )
    )

    row = cur.fetchone()

    fact_id = row[0] if row else None

    cur.execute(
        """
        INSERT INTO fact_history
            (
                fact_id,
                chat_id,
                action,
                old_value,
                new_value,
                reason,
                source_message_id
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fact_id,
            chat_id,
            "CREATED",
            None,
            fact_value,
            reason,
            source_message_id
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return {
        "id": fact_id,
        "status": status,
        "existing": False
    }


def get_facts(
    chat_id,
    status=FACT_STATUS_CONFIRMED
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT
            id,
            fact_key,
            fact_value,
            source,
            confidence,
            status,
            source_message_id,
            created_at,
            updated_at,
            confirmed_at,
            rejected_at,
            rejection_reason
        FROM facts
        WHERE chat_id = %s
          AND status = %s
        ORDER BY id
        """,
        (
            chat_id,
            status
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def get_pending_facts(chat_id):
    return get_facts(
        chat_id,
        FACT_STATUS_PENDING
    )


def get_conflict_facts(chat_id):
    return get_facts(
        chat_id,
        FACT_STATUS_CONFLICT
    )


def get_fact_by_id(
    chat_id,
    fact_id
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM facts
        WHERE chat_id = %s
          AND id = %s
        LIMIT 1
        """,
        (
            chat_id,
            fact_id
        )
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row


def confirm_fact(
    chat_id,
    fact_id
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM facts
        WHERE chat_id = %s
          AND id = %s
        FOR UPDATE
        """,
        (
            chat_id,
            fact_id
        )
    )

    fact = cur.fetchone()

    if not fact:
        conn.rollback()
        cur.close()
        conn.close()

        return {
            "ok": False,
            "message": "ფაქტი ვერ მოიძებნა."
        }

    if fact["status"] == FACT_STATUS_CONFIRMED:
        conn.rollback()
        cur.close()
        conn.close()

        return {
            "ok": True,
            "message": "ეს ფაქტი უკვე დადასტურებულია."
        }

    if fact["status"] == FACT_STATUS_REJECTED:
        conn.rollback()
        cur.close()
        conn.close()

        return {
            "ok": False,
            "message": "ეს ფაქტი უკვე უარყოფილია."
        }

    # --------------------------------------------------------
    # Check whether a different confirmed value exists.
    # --------------------------------------------------------

    cur.execute(
        """
        SELECT
            id,
            fact_value
        FROM facts
        WHERE chat_id = %s
          AND fact_key = %s
          AND status = 'CONFIRMED'
          AND id <> %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            chat_id,
            fact["fact_key"],
            fact_id
        )
    )

    existing_confirmed = cur.fetchone()

    if existing_confirmed:
        cur.execute(
            """
            UPDATE facts
            SET
                status = 'CONFLICT',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (fact_id,)
        )

        conn.commit()

        cur.close()
        conn.close()

        return {
            "ok": False,
            "conflict": True,
            "message": (
                "ამ ფაქტისთვის უკვე არსებობს "
                "სხვა დადასტურებული მნიშვნელობა."
            ),
            "existing_value": existing_confirmed[
                "fact_value"
            ],
            "new_value": fact[
                "fact_value"
            ]
        }

    cur.execute(
        """
        UPDATE facts
        SET
            status = 'CONFIRMED',
            confidence = 'confirmed',
            source = 'USER_CONFIRMED',
            confirmed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (fact_id,)
    )

    cur.execute(
        """
        INSERT INTO fact_history
            (
                fact_id,
                chat_id,
                action,
                old_value,
                new_value,
                reason,
                source_message_id
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fact_id,
            chat_id,
            "CONFIRMED",
            fact["fact_value"],
            fact["fact_value"],
            "Confirmed explicitly by user.",
            fact["source_message_id"]
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return {
        "ok": True,
        "fact": fact
    }


def reject_fact(
    chat_id,
    fact_id,
    reason="Rejected by user."
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM facts
        WHERE chat_id = %s
          AND id = %s
        FOR UPDATE
        """,
        (
            chat_id,
            fact_id
        )
    )

    fact = cur.fetchone()

    if not fact:
        conn.rollback()
        cur.close()
        conn.close()

        return {
            "ok": False,
            "message": "ფაქტი ვერ მოიძებნა."
        }

    if fact["status"] == FACT_STATUS_CONFIRMED:
        conn.rollback()
        cur.close()
        conn.close()

        return {
            "ok": False,
            "message": (
                "დადასტურებული ფაქტის უარყოფა "
                "ამ ბრძანებით შეუძლებელია."
            )
        }

    cur.execute(
        """
        UPDATE facts
        SET
            status = 'REJECTED',
            confidence = 'rejected',
            rejected_at = CURRENT_TIMESTAMP,
            rejection_reason = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (
            reason,
            fact_id
        )
    )

    cur.execute(
        """
        INSERT INTO fact_history
            (
                fact_id,
                chat_id,
                action,
                old_value,
                new_value,
                reason,
                source_message_id
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fact_id,
            chat_id,
            "REJECTED",
            fact["fact_value"],
            fact["fact_value"],
            reason,
            fact["source_message_id"]
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return {
        "ok": True,
        "fact": fact
    }


def build_fact_context(chat_id):
    facts = get_facts(
        chat_id,
        FACT_STATUS_CONFIRMED
    )

    if not facts:
        return (
            "FACT DATABASE:\n"
            "No confirmed facts stored yet.\n"
            "Do not present any project/company "
            "number as confirmed."
        )

    lines = [
        "FACT DATABASE:",
        "The following are CONFIRMED facts only."
    ]

    for fact in facts:
        lines.append(
            "- "
            + str(fact["fact_key"])
            + " = "
            + str(fact["fact_value"])
            + " | source="
            + str(fact["source"])
            + " | status="
            + str(fact["status"])
        )

    return "\n".join(lines)


# ============================================================
# EXPLICIT FACT DETECTION
# ============================================================

def contains_explicit_statement(text):
    lower = text.lower()

    statement_words = [
        "არის",
        "შეადგენს",
        "გვაქვს",
        "აქვს",
        "უდრის",
        "ტოლია",
        "იქნება",
        "is",
        "equals",
        "has",
        "amounts to",
        "we have"
    ]

    return any(
        word in lower
        for word in statement_words
    )


def detect_explicit_facts(
    chat_id,
    user_text,
    source_message_id
):
    """
    IMPORTANT:
    Merely mentioning NIKKEA, Samgori or SAMTISI
    does NOT create facts.

    A candidate is created only when the user's text
    appears to contain an explicit factual statement.
    """

    text = user_text.strip()

    if not text:
        return []

    if not contains_explicit_statement(
        text
    ):
        return []

    detected = []

    lower = text.lower()

    # --------------------------------------------------------
    # SAMTISI
    # --------------------------------------------------------

    company_patterns = [
        (
            r"(?:samtisi\s+construction\s+llc)"
        ),
        (
            r"(?:სამტისი\s+კონსტრაქშენ)"
        ),
        (
            r"(?:სამთისი\s+კონსტრაქშენ)"
        )
    ]

    if any(
        re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )
        for pattern in company_patterns
    ):
        result = save_fact_candidate(
            chat_id,
            "company_name",
            "SAMTISI CONSTRUCTION LLC",
            source="USER_STATED",
            confidence="pending",
            status=FACT_STATUS_PENDING,
            source_message_id=source_message_id,
            reason=(
                "Company name explicitly stated by user."
            )
        )

        if result and not result.get(
            "existing"
        ):
            detected.append(result)

    # --------------------------------------------------------
    # NIKKEA 12
    # --------------------------------------------------------

    if (
        "nikkea 12" in lower
        or "ნიკეა 12" in lower
    ):

        # Land area
        land_match = re.search(
            r"(?:
                მიწის\s*(?:ფართობი|ფართი)
                |
                land\s*(?:area|size)
            )
            \D{0,80}
            ([0-9][0-9,\.\s]*)
            \s*(?:m2|m²|მ2|მ²|კვ\.?\s*მ)",
            text,
            flags=re.IGNORECASE | re.VERBOSE
        )

        if land_match:
            value = (
                land_match.group(1)
                .strip()
                + " m²"
            )

            result = save_fact_candidate(
                chat_id,
                "nikkea_12_land_area",
                value,
                source="USER_STATED",
                confidence="pending",
                status=FACT_STATUS_PENDING,
                source_message_id=source_message_id,
                reason=(
                    "NIKKEA 12 land area explicitly "
                    "stated by user."
                )
            )

            if result and not result.get(
                "existing"
            ):
                detected.append(result)

        # Saleable area
        saleable_match = re.search(
            r"(?:
                გასაყიდი\s*(?:ფართობი|ფართი)
                |
                saleable\s*area
            )
            \D{0,80}
            ([0-9][0-9,\.\s]*)
            \s*(?:m2|m²|მ2|მ²|კვ\.?\s*მ)",
            text,
            flags=re.IGNORECASE | re.VERBOSE
        )

        if saleable_match:
            value = (
                saleable_match.group(1)
                .strip()
                + " m²"
            )

            result = save_fact_candidate(
                chat_id,
                "nikkea_12_total_saleable_area",
                value,
                source="USER_STATED",
                confidence="pending",
                status=FACT_STATUS_PENDING,
                source_message_id=source_message_id,
                reason=(
                    "NIKKEA 12 saleable area explicitly "
                    "stated by user."
                )
            )

            if result and not result.get(
                "existing"
            ):
                detected.append(result)

        # Hotel rooms area
        hotel_match = re.search(
            r"(?:
                სასტუმროს\s*(?:ნომრების\s*)?(?:ფართობი|ფართი)
                |
                hotel\s*(?:rooms\s*)?area
            )
            \D{0,80}
            ([0-9][0-9,\.\s]*)
            \s*(?:m2|m²|მ2|მ²|კვ\.?\s*მ)",
            text,
            flags=re.IGNORECASE | re.VERBOSE
        )

        if hotel_match:
            value = (
                hotel_match.group(1)
                .strip()
                + " m²"
            )

            result = save_fact_candidate(
                chat_id,
                "nikkea_12_hotel_rooms_area",
                value,
                source="USER_STATED",
                confidence="pending",
                status=FACT_STATUS_PENDING,
                source_message_id=source_message_id,
                reason=(
                    "NIKKEA 12 hotel rooms area "
                    "explicitly stated by user."
                )
            )

            if result and not result.get(
                "existing"
            ):
                detected.append(result)

    return detected


# ============================================================
# LEGACY MEMORY DETECTION
# ============================================================

def detect_memories(
    chat_id,
    user_text
):
    """
    Legacy memory is intentionally kept separate
    from the confirmed Fact Engine.

    Mentioning a project may update legacy memory,
    but it does NOT create a confirmed fact.
    """

    text = user_text.lower()

    if (
        "samtisi" in text
        or "სამთისი" in text
        or "სამტისი" in text
    ):
        save_memory(
            chat_id,
            "company",
            "SAMTISI CONSTRUCTION LLC"
        )

    if (
        "nikkea 12" in text
        or "ნიკეა 12" in text
    ):
        save_memory(
            chat_id,
            "project_nikkea_12",
            (
                "Kutaisi, Nikkea 12. "
                "Project information exists in "
                "conversation history, but numerical "
                "data is not automatically treated as "
                "confirmed facts."
            )
        )

    if (
        "samgori" in text
        or "სამგორი" in text
    ):
        save_memory(
            chat_id,
            "project_samgori",
            (
                "Tbilisi, Samgori, "
                "Giorgi Naderishvili Street. "
                "Project information exists in "
                "conversation history, but numerical "
                "data is not automatically treated as "
                "confirmed facts."
            )
        )

    if (
        "golden lake" in text
        or "oqri lake" in text
        or "ოქროს ტბა" in text
    ):
        save_memory(
            chat_id,
            "project_golden_lake",
            (
                "Golden Lake / Oqri Lake "
                "development concept. "
                "Numerical information is not "
                "automatically treated as confirmed facts."
            )
        )


# ============================================================
# CODE PROJECTS
# ============================================================

def save_code_project(
    chat_id,
    name,
    language="python",
    description=""
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO code_projects
            (
                chat_id,
                name,
                language,
                description
            )
        VALUES
            (%s, %s, %s, %s)
        ON CONFLICT (chat_id, name)
        DO UPDATE SET
            language = EXCLUDED.language,
            description = EXCLUDED.description,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            chat_id,
            name,
            language,
            description
        )
    )

    conn.commit()

    cur.close()
    conn.close()


# ============================================================
# CODE VERSION MANAGEMENT
# ============================================================

def get_next_code_version(
    chat_id,
    project_name
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            COALESCE(MAX(version_number), 0)
        FROM code_versions
        WHERE chat_id = %s
          AND project_name = %s
        """,
        (
            chat_id,
            project_name
        )
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    current = (
        row[0]
        if row
        else 0
    )

    return int(current) + 1


def get_latest_code_version(
    chat_id,
    project_name
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM code_versions
        WHERE chat_id = %s
          AND project_name = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            chat_id,
            project_name
        )
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row


def save_code_version(
    chat_id,
    project_name,
    filename,
    language,
    code,
    syntax_ok,
    syntax_error="",
    change_reason="initial generation",
    parent_version_id=None
):
    version_number = get_next_code_version(
        chat_id,
        project_name
    )

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO code_versions
            (
                chat_id,
                project_name,
                filename,
                language,
                code,
                version_number,
                parent_version_id,
                change_reason,
                syntax_ok,
                syntax_error
            )
        VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
        RETURNING id
        """,
        (
            chat_id,
            project_name,
            filename,
            language,
            code,
            version_number,
            parent_version_id,
            change_reason,
            syntax_ok,
            syntax_error
        )
    )

    row = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    return {
        "id": row[0],
        "version_number": version_number
    }


def get_code_versions(
    chat_id,
    project_name=None,
    limit=20
):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    if project_name:
        cur.execute(
            """
            SELECT
                id,
                project_name,
                filename,
                language,
                version_number,
                parent_version_id,
                change_reason,
                syntax_ok,
                syntax_error,
                created_at
            FROM code_versions
            WHERE chat_id = %s
              AND project_name = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                project_name,
                limit
            )
        )

    else:
        cur.execute(
            """
            SELECT
                id,
                project_name,
                filename,
                language,
                version_number,
                parent_version_id,
                change_reason,
                syntax_ok,
                syntax_error,
                created_at
            FROM code_versions
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                chat_id,
                limit
            )
        )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ============================================================
# TELEGRAM
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
    files=None,
    timeout=60
):
    if not TELEGRAM_BOT_TOKEN:
        return None

    try:
        if files:
            response = requests.post(
                telegram_url(method),
                data=payload or {},
                files=files,
                timeout=timeout
            )

        else:
            response = requests.post(
                telegram_url(method),
                json=payload or {},
                timeout=timeout
            )

        if response.status_code == 409:
            print(
                "TELEGRAM 409 CONFLICT: "
                "another polling process may be active."
            )

        if response.status_code >= 400:
            print(
                "Telegram HTTP error:",
                response.status_code,
                response.text[:1000]
            )

        try:
            return response.json()

        except Exception:
            return {
                "ok": False,
                "status_code": response.status_code,
                "text": response.text
            }

    except Exception as exc:
        print(
            "Telegram request error:",
            repr(exc)
        )

        return None


def delete_webhook():
    result = telegram_request(
        "deleteWebhook",
        {
            "drop_pending_updates": False
        },
        timeout=30
    )

    print(
        "Telegram deleteWebhook:",
        result
    )


def send_message(
    chat_id,
    text
):
    if not text:
        text = "მიღებულია."

    max_length = 4000

    if len(text) <= max_length:
        return telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text
            }
        )

    parts = []
    current = ""

    for paragraph in text.split("\n"):

        if (
            len(current)
            + len(paragraph)
            + 1
            > max_length
        ):
            if current:
                parts.append(
                    current
                )

            current = paragraph

        else:

            if current:
                current += "\n"

            current += paragraph

    if current:
        parts.append(
            current
        )

    results = []

    for part in parts:
        results.append(
            telegram_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": part
                }
            )
        )

    return results


def send_document_to_chat(
    chat_id,
    filepath,
    caption=""
):
    if not os.path.exists(
        filepath
    ):
        return None

    try:
        with open(
            filepath,
            "rb"
        ) as document:

            return telegram_request(
                "sendDocument",
                payload={
                    "chat_id": chat_id,
                    "caption": caption
                },
                files={
                    "document": document
                },
                timeout=120
            )

    except Exception as exc:
        print(
            "send_document_to_chat error:",
            repr(exc)
        )

        return None


# ============================================================
# GEMINI
# ============================================================

def gemini_url():
    return (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        + GEMINI_MODEL
        + ":generateContent?key="
        + GEMINI_API_KEY
    )


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return (
            "Gemini API key არ არის დაყენებული."
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
        ]
    }

    try:
        response = requests.post(
            gemini_url(),
            json=payload,
            timeout=120
        )

        if response.status_code != 200:
            print(
                "Gemini error:",
                response.status_code,
                response.text[:2000]
            )

            return (
                "AI მოდელთან დაკავშირებისას მოხდა "
                "შეცდომა.\n\nHTTP status: "
                + str(response.status_code)
                + "\n\n"
                + response.text[:1000]
            )

        data = response.json()

        candidates = data.get(
            "candidates",
            []
        )

        if not candidates:
            return (
                "Gemini-მ პასუხი ვერ დააბრუნა."
            )

        content = candidates[0].get(
            "content",
            {}
        )

        parts = content.get(
            "parts",
            []
        )

        answer_parts = []

        for part in parts:

            text = part.get(
                "text"
            )

            if text:
                answer_parts.append(
                    text
                )

        answer = "\n".join(
            answer_parts
        ).strip()

        if not answer:
            return (
                "Gemini-მ ცარიელი პასუხი დააბრუნა."
            )

        return answer

    except Exception as exc:
        print(
            "Gemini request exception:",
            repr(exc)
        )

        return (
            "Gemini-სთან დაკავშირებისას "
            "ტექნიკური შეცდომა მოხდა:\n"
            + str(exc)
        )


# ============================================================
# CONSTITUTION
# ============================================================

def constitution():
    return (
        "GENIOSA CONSTITUTION\n"
        "====================\n"
        "\n"
        "Geniosa არის მომხმარებლის პირადი ბიზნეს "
        "მრჩეველი, ეკონომისტი, ანალიტიკოსი და "
        "პროგრამული ასისტენტი.\n"
        "\n"
        "ძირითადი წესები:\n"
        "1. არ მოიგონო ფაქტები.\n"
        "2. მომხმარებლის მიერ დადასტურებული ფაქტი "
        "არ უნდა შეიცვალოს ვარაუდით.\n"
        "3. ვარაუდი არასოდეს წარმოადგინო როგორც ფაქტი.\n"
        "4. თუ ინფორმაცია უცნობია, თქვი რომ უცნობია.\n"
        "5. წინააღმდეგობრივი მონაცემებისას მომხმარებელს "
        "დაუსვი დამაზუსტებელი კითხვა.\n"
        "6. ფინანსურ პროგნოზში მიუთითე დაშვებები.\n"
        "7. მნიშვნელოვანი ცვლილება მომხმარებლის "
        "დადასტურების გარეშე არ განახორციელო.\n"
        "8. კოდის გენერირებისას ეცადე შექმნა "
        "რეალურად გამოსაყენებელი კოდი.\n"
        "9. კოდის შეცდომის პოვნისას განასხვავე "
        "syntax, runtime, dependency და logic შეცდომები.\n"
        "10. კოდის ავტომატური შესრულება production "
        "გარემოში დაუშვებელია.\n"
        "11. production deployment მომხმარებლის "
        "დადასტურების გარეშე დაუშვებელია.\n"
        "12. საიდუმლო API keys და პაროლები კოდში "
        "არ ჩაწერო.\n"
        "13. მხოლოდ CONFIRMED სტატუსის მქონე მონაცემი "
        "შეიძლება წარმოდგენილი იყოს როგორც დადასტურებული "
        "ფაქტი.\n"
        "14. მომხმარებლის მიერ ნათქვამი ინფორმაცია "
        "ავტომატურად არ არის დადასტურებული ფაქტი.\n"
        "15. PENDING მონაცემი უნდა გამოცხადდეს "
        "დასადასტურებელ ინფორმაციად.\n"
        "16. CONFIRMED ფაქტი არ გადაიწეროს ახალი "
        "მნიშვნელობით ავტომატურად.\n"
        "17. განსხვავებული ახალი მნიშვნელობის შემთხვევაში "
        "შექმენი CONFLICT და აცნობე მომხმარებელს.\n"
    )


# ============================================================
# CONTEXT
# ============================================================

def build_memory_context(chat_id):
    memories = get_memories(
        chat_id
    )

    if not memories:
        return (
            "LEGACY MEMORY: empty."
        )

    lines = [
        "LEGACY MEMORY:"
    ]

    for item in memories:
        lines.append(
            "- "
            + str(item["memory_key"])
            + ": "
            + str(item["memory_value"])
        )

    return "\n".join(lines)


def build_project_context(chat_id):
    projects = get_projects(
        chat_id
    )

    if not projects:
        return (
            "PROJECT DATABASE: empty."
        )

    lines = [
        "PROJECT DATABASE:"
    ]

    for project in projects:
        lines.append(
            "- "
            + str(project["name"])
            + ": "
            + str(
                project.get(
                    "description"
                )
                or ""
            )
        )

    return "\n".join(lines)


def build_history_context(chat_id):
    rows = get_recent_messages(
        chat_id,
        12
    )

    if not rows:
        return (
            "CONVERSATION HISTORY: empty."
        )

    lines = [
        "RECENT CONVERSATION:"
    ]

    for row in rows:

        role = str(
            row.get(
                "role",
                ""
            )
        )

        text = str(
            row.get(
                "text",
                ""
            )
        )

        if len(text) > 1200:
            text = (
                text[:1200]
                + "..."
            )

        lines.append(
            role
            + ": "
            + text
        )

    return "\n".join(
        lines
    )


# ============================================================
# PYTHON ANALYSIS
# ============================================================

def extract_code(text):
    if not text:
        return ""

    matches = re.findall(
        r"```(?:python|py)?\s*(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL
    )

    if matches:
        return matches[-1].strip()

    return text.strip()


def check_python_syntax(code):
    try:
        ast.parse(
            code
        )

        return {
            "ok": True,
            "error": ""
        }

    except SyntaxError as exc:

        message = (
            "SyntaxError: "
            + str(exc)
        )

        if exc.lineno:
            message += (
                " | line="
                + str(exc.lineno)
            )

        if exc.offset:
            message += (
                " | column="
                + str(exc.offset)
            )

        return {
            "ok": False,
            "error": message
        }

    except Exception as exc:

        return {
            "ok": False,
            "error": str(exc)
        }


def detect_common_python_problems(
    code
):
    problems = []

    if "inplace-True" in code:
        problems.append(
            {
                "type": "runtime",
                "message": (
                    "'inplace-True' არ არის Syntax Error. "
                    "Python ამას გამოხატულებად აღიქვამს. "
                    "სავარაუდოდ სწორი ფორმაა 'inplace=True'."
                )
            }
        )

    if "inplace-False" in code:
        problems.append(
            {
                "type": "runtime",
                "message": (
                    "'inplace-False' არ არის Syntax Error. "
                    "სავარაუდოდ სწორი ფორმაა 'inplace=False'."
                )
            }
        )

    if "pd.ExcelWriter" in code:

        if (
            "openpyxl" not in code
            and "engine=" not in code
        ):
            problems.append(
                {
                    "type": "dependency",
                    "message": (
                        "ExcelWriter-ისთვის შეიძლება "
                        "საჭირო იყოს შესაბამისი Excel "
                        "engine, მაგალითად openpyxl."
                    )
                }
            )

    return problems


def analyze_python_code(
    code
):
    syntax = check_python_syntax(
        code
    )

    common = detect_common_python_problems(
        code
    )

    return {
        "syntax_ok": syntax["ok"],
        "syntax_error": syntax["error"],
        "common_problems": common
    }


# ============================================================
# CODE FILE
# ============================================================

def create_code_file(
    code,
    filename
):
    safe_name = os.path.basename(
        filename
    )

    if not safe_name:
        safe_name = (
            "geniosa_code.py"
        )

    if not safe_name.endswith(
        ".py"
    ):
        safe_name += ".py"

    filepath = os.path.join(
        tempfile.gettempdir(),
        safe_name
    )

    with open(
        filepath,
        "w",
        encoding="utf-8"
    ) as file:

        file.write(
            code
        )

    return filepath


# ============================================================
# DEVELOPER ACCESS
# ============================================================

def developer_access_allowed(
    chat_id
):
    if not OWNER_ID:
        return True

    return (
        str(chat_id)
        == str(OWNER_ID)
    )


# ============================================================
# DEVELOPER PROMPT
# ============================================================

def build_developer_prompt(
    chat_id,
    task,
    mode="generate"
):
    parts = []

    parts.append(
        constitution()
    )

    parts.append(
        "DEVELOPER ENGINE\n"
        "================\n"
        "Mode: "
        + mode
        + "\n"
        "\n"
        "You are a senior Python developer "
        "and software architect.\n"
        "\n"
        "FACT RULE:\n"
        "Only confirmed user facts may be presented "
        "as confirmed facts.\n"
        "PENDING facts are NOT confirmed facts.\n"
        "Do not invent missing project information.\n"
        "\n"
        "CODE RULE:\n"
        "If code is requested, provide complete "
        "usable code.\n"
        "Put Python code inside a python code block.\n"
        "\n"
        "DEBUG RULE:\n"
        "Distinguish between:\n"
        "- syntax error\n"
        "- runtime error\n"
        "- API error\n"
        "- dependency error\n"
        "- logic error\n"
        "- configuration error\n"
        "\n"
        "If a code correction is requested, explain "
        "what was wrong and provide the corrected "
        "complete version.\n"
    )

    parts.append(
        build_fact_context(
            chat_id
        )
    )

    parts.append(
        build_memory_context(
            chat_id
        )
    )

    parts.append(
        build_project_context(
            chat_id
        )
    )

    parts.append(
        build_history_context(
            chat_id
        )
    )

    parts.append(
        "USER TASK:\n"
        + task
    )

    return "\n\n".join(
        parts
    )


# ============================================================
# DEVELOPER GENERATION
# ============================================================

def developer_generate(
    chat_id,
    task,
    project_name="Geniosa"
):
    prompt = build_developer_prompt(
        chat_id,
        task,
        mode="generate"
    )

    answer = call_gemini(
        prompt
    )

    code = extract_code(
        answer
    )

    analysis = analyze_python_code(
        code
    )

    filename = (
        "geniosa_generated.py"
    )

    lowered = task.lower()

    if "app.py" in lowered:
        filename = "app.py"

    elif "requirements" in lowered:
        filename = (
            "requirements.txt"
        )

    elif "telegram" in lowered:
        filename = (
            "telegram_bot.py"
        )

    save_code_project(
        chat_id,
        project_name,
        "python",
        task[:1000]
    )

    saved = None

    if code:
        saved = save_code_version(
            chat_id,
            project_name,
            filename,
            "python",
            code,
            analysis[
                "syntax_ok"
            ],
            analysis[
                "syntax_error"
            ],
            "initial generation"
        )

    return {
        "answer": answer,
        "code": code,
        "filename": filename,
        "analysis": analysis,
        "saved": saved
    }


# ============================================================
# DEBUG / FIX ENGINE
# ============================================================

def developer_debug(
    chat_id,
    task,
    code,
    project_name="Geniosa"
):
    before = analyze_python_code(
        code
    )

    prompt = build_developer_prompt(
        chat_id,
        (
            "DEBUG AND FIX THIS PYTHON CODE.\n\n"
            "USER'S DEBUG REQUEST:\n"
            + task
            + "\n\n"
            "CURRENT CODE:\n"
            "```python\n"
            + code
            + "\n```\n\n"
            "STATIC ANALYSIS BEFORE FIX:\n"
            + str(before)
            + "\n\n"
            "Find the actual problem(s), "
            "distinguish their type, explain them, "
            "and return the complete corrected code "
            "as a single Python code block."
        ),
        mode="debug_and_fix"
    )

    answer = call_gemini(
        prompt
    )

    fixed_code = extract_code(
        answer
    )

    after = analyze_python_code(
        fixed_code
    )

    latest = get_latest_code_version(
        chat_id,
        project_name
    )

    parent_id = None

    if latest:
        parent_id = latest["id"]

    saved = None

    if fixed_code:
        saved = save_code_version(
            chat_id,
            project_name,
            "geniosa_fixed.py",
            "python",
            fixed_code,
            after[
                "syntax_ok"
            ],
            after[
                "syntax_error"
            ],
            "debug/fix",
            parent_id
        )

    return {
        "answer": answer,
        "before": before,
        "after": after,
        "fixed_code": fixed_code,
        "saved": saved
    }


# ============================================================
# GENERAL RESPONSE
# ============================================================

def generate_general_response(
    chat_id,
    user_text
):
    parts = []

    parts.append(
        constitution()
    )

    parts.append(
        build_fact_context(
            chat_id
        )
    )

    parts.append(
        build_memory_context(
            chat_id
        )
    )

    parts.append(
        build_project_context(
            chat_id
        )
    )

    parts.append(
        build_history_context(
            chat_id
        )
    )

    parts.append(
        "IMPORTANT FACT POLICY:\n"
        "Only CONFIRMED facts may be stated as facts.\n"
        "PENDING information must be described as "
        "unconfirmed.\n"
        "If no confirmed fact exists, say so.\n"
        "Do not convert legacy memory into confirmed facts."
    )

    parts.append(
        "USER MESSAGE:\n"
        + user_text
    )

    parts.append(
        "Answer in Georgian unless the user "
        "clearly requests another language.\n"
        "Never invent facts."
    )

    return call_gemini(
        "\n\n".join(
            parts
        )
    )


# ============================================================
# COMMANDS
# ============================================================

def command_start():
    return (
        "გამარჯობა. მე ვარ Geniosa 4.3. 🤖\n\n"
        "მე შემიძლია დაგეხმარო:\n"
        "• ბიზნესის ანალიზში\n"
        "• საინვესტიციო პროექტებში\n"
        "• ფინანსურ მოდელებში\n"
        "• პროექტების მართვაში\n"
        "• კოდის წერაში\n"
        "• debugging-ში\n"
        "• კოდის ვერსიების მართვაში\n"
        "• ფაქტების უსაფრთხო მართვაში\n\n"
        "Fact Engine 2.0:\n"
        "/facts — დადასტურებული ფაქტები\n"
        "/pending_facts — დასადასტურებელი ფაქტები\n"
        "/confirm_fact <ID> — ფაქტის დადასტურება\n"
        "/reject_fact <ID> — ფაქტის უარყოფა\n"
        "/fact_conflicts — კონფლიქტური ფაქტები\n\n"
        "Developer:\n"
        "/developer\n"
        "/code <დავალება>\n"
        "/debug <დავალება>\n"
        "/code_history\n\n"
        "მეხსიერება:\n"
        "/memory\n\n"
        "პროექტები:\n"
        "/projects\n\n"
        "/help"
    )


def command_help():
    return (
        "GENIOSA 4.3\n\n"
        "FACT ENGINE 2.0\n"
        "/facts — დადასტურებული ფაქტები\n"
        "/pending_facts — დასადასტურებელი ფაქტები\n"
        "/confirm_fact <ID> — დადასტურება\n"
        "/reject_fact <ID> — უარყოფა\n"
        "/fact_conflicts — კონფლიქტები\n\n"
        "GENERAL\n"
        "/start — დაწყება\n"
        "/help — დახმარება\n"
        "/memory — მეხსიერება\n"
        "/projects — პროექტები\n\n"
        "DEVELOPER\n"
        "/developer — Developer რეჟიმი\n"
        "/code <task> — კოდის შექმნა\n"
        "/debug <task> — debugging\n"
        "/code_history — კოდის ვერსიები"
    )


def command_facts(chat_id):
    facts = get_facts(
        chat_id,
        FACT_STATUS_CONFIRMED
    )

    if not facts:
        return (
            "დადასტურებული ფაქტები ჯერ არ არის.\n\n"
            "ეს სწორია — Geniosa ახლა ინფორმაციას "
            "ავტომატურად დადასტურებულ ფაქტად აღარ "
            "ჩათვლის."
        )

    lines = [
        "დადასტურებული ფაქტები:"
    ]

    for fact in facts:
        lines.append(
            "\nID: "
            + str(fact["id"])
            + "\n"
            + str(fact["fact_key"])
            + "\n"
            + str(fact["fact_value"])
            + "\nsource: "
            + str(fact["source"])
            + "\nstatus: "
            + str(fact["status"])
        )

    return "\n".join(
        lines
    )


def command_pending_facts(
    chat_id
):
    facts = get_pending_facts(
        chat_id
    )

    if not facts:
        return (
            "დასადასტურებელი ფაქტები არ არის."
        )

    lines = [
        "დასადასტურებელი ფაქტები:",
        "",
        "თუ მონაცემი სწორია, გამოიყენე:",
        "/confirm_fact <ID>",
        "",
        "თუ არასწორია:",
        "/reject_fact <ID>"
    ]

    for fact in facts:
        lines.append(
            "\nID: "
            + str(fact["id"])
            + "\n"
            + str(fact["fact_key"])
            + " = "
            + str(fact["fact_value"])
            + "\nsource: "
            + str(fact["source"])
        )

    return "\n".join(
        lines
    )


def command_fact_conflicts(
    chat_id
):
    facts = get_conflict_facts(
        chat_id
    )

    if not facts:
        return (
            "კონფლიქტური ფაქტები არ არის."
        )

    lines = [
        "კონფლიქტური ფაქტები:"
    ]

    for fact in facts:
        lines.append(
            "\nID: "
            + str(fact["id"])
            + "\n"
            + str(fact["fact_key"])
            + " = "
            + str(fact["fact_value"])
            + "\nstatus: CONFLICT"
        )

    return "\n".join(
        lines
    )


def command_confirm_fact(
    chat_id,
    fact_id
):
    result = confirm_fact(
        chat_id,
        fact_id
    )

    if not result["ok"]:

        if result.get(
            "conflict"
        ):
            return (
                "⚠️ კონფლიქტი აღმოჩნდა.\n\n"
                "უკვე დადასტურებული მნიშვნელობა:\n"
                + str(
                    result.get(
                        "existing_value"
                    )
                )
                + "\n\n"
                "ახალი მნიშვნელობა:\n"
                + str(
                    result.get(
                        "new_value"
                    )
                )
                + "\n\n"
                "ძველი ფაქტი ავტომატურად არ შეიცვალა."
            )

        return (
            "❌ "
            + str(
                result.get(
                    "message"
                )
            )
        )

    fact = result["fact"]

    return (
        "✅ ფაქტი დადასტურდა.\n\n"
        "ID: "
        + str(
            fact["id"]
        )
        + "\n"
        + str(
            fact["fact_key"]
        )
        + " = "
        + str(
            fact["fact_value"]
        )
        + "\n\n"
        "ამიერიდან Geniosa-სთვის ეს მონაცემი "
        "CONFIRMED ფაქტია."
    )


def command_reject_fact(
    chat_id,
    fact_id
):
    result = reject_fact(
        chat_id,
        fact_id
    )

    if not result["ok"]:
        return (
            "❌ "
            + str(
                result.get(
                    "message"
                )
            )
        )

    fact = result["fact"]

    return (
        "❌ ფაქტი უარყოფილია და აღარ გამოიყენება "
        "დადასტურებულ მონაცემად.\n\n"
        "ID: "
        + str(
            fact["id"]
        )
        + "\n"
        + str(
            fact["fact_key"]
        )
        + " = "
        + str(
            fact["fact_value"]
        )
    )


def command_memory(
    chat_id
):
    memories = get_memories(
        chat_id
    )

    if not memories:
        return (
            "მეხსიერება ჯერ ცარიელია."
        )

    lines = [
        "შენახული მეხსიერება:"
    ]

    for item in memories:
        lines.append(
            "\n"
            + str(
                item["memory_key"]
            )
            + "\n"
            + str(
                item["memory_value"]
            )
        )

    return "\n".join(
        lines
    )


def command_projects(
    chat_id
):
    projects = get_projects(
        chat_id
    )

    if not projects:
        return (
            "პროექტები ჯერ არ არის შენახული."
        )

    lines = [
        "შენახული პროექტები:"
    ]

    for project in projects:
        lines.append(
            "\n"
            + str(
                project["name"]
            )
            + "\n"
            + str(
                project.get(
                    "description"
                )
                or ""
            )
        )

    return "\n".join(
        lines
    )


def command_code_history(
    chat_id
):
    rows = get_code_versions(
        chat_id,
        limit=20
    )

    if not rows:
        return (
            "კოდის ვერსიები ჯერ არ არის."
        )

    lines = [
        "კოდის ბოლო ვერსიები:"
    ]

    for row in rows:

        status = (
            "OK"
            if row["syntax_ok"]
            else "ERROR"
        )

        lines.append(
            "\nV"
            + str(
                row["version_number"]
            )
            + " | "
            + str(
                row["project_name"]
            )
            + " | "
            + str(
                row["filename"]
            )
            + " | "
            + status
            + "\nChange: "
            + str(
                row.get(
                    "change_reason"
                )
                or ""
            )
            + "\n"
            + str(
                row["created_at"]
            )
        )

    return "\n".join(
        lines
    )


# ============================================================
# FACT COMMAND PARSER
# ============================================================

def parse_integer_argument(
    command_text
):
    parts = command_text.split()

    if len(parts) < 2:
        return None

    try:
        return int(
            parts[1]
        )

    except Exception:
        return None


# ============================================================
# UPDATE PROCESSING
# ============================================================

def process_update(update):
    if not update:
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

    user_text = message.get(
        "text"
    )

    if not user_text:
        return

    user_text = user_text.strip()

    print(
        "Telegram message:",
        chat_id,
        user_text[:500]
    )

    # --------------------------------------------------------
    # SAVE USER MESSAGE AND KEEP MESSAGE ID
    # --------------------------------------------------------

    user_message_id = None

    try:
        user_message_id = save_message(
            chat_id,
            "user",
            user_text
        )

    except Exception as exc:
        print(
            "save_message user error:",
            repr(exc)
        )

    # --------------------------------------------------------
    # LEGACY MEMORY
    # --------------------------------------------------------

    try:
        detect_memories(
            chat_id,
            user_text
        )

    except Exception as exc:
        print(
            "memory detection error:",
            repr(exc)
        )

    # --------------------------------------------------------
    # FACT DETECTION
    # --------------------------------------------------------

    detected_facts = []

    try:
        detected_facts = detect_explicit_facts(
            chat_id,
            user_text,
            user_message_id
        )

    except Exception as exc:
        print(
            "fact detection error:",
            repr(exc)
        )

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if user_text.startswith(
        "/start"
    ):
        answer = command_start()

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    elif user_text.startswith(
        "/help"
    ):
        answer = command_help()

    # --------------------------------------------------------
    # FACTS
    # --------------------------------------------------------

    elif user_text.startswith(
        "/facts"
    ):
        try:
            answer = command_facts(
                chat_id
            )

        except Exception as exc:
            answer = (
                "ფაქტების წაკითხვა ვერ მოხერხდა:\n"
                + str(exc)
            )

    # --------------------------------------------------------
    # PENDING FACTS
    # --------------------------------------------------------

    elif user_text.startswith(
        "/pending_facts"
    ):
        try:
            answer = command_pending_facts(
                chat_id
            )

        except Exception as exc:
            answer = (
                "დასადასტურებელი ფაქტების "
                "წაკითხვა ვერ მოხერხდა:\n"
                + str(exc)
            )

    # --------------------------------------------------------
    # CONFIRM FACT
    # --------------------------------------------------------

    elif user_text.startswith(
        "/confirm_fact"
    ):
        fact_id = parse_integer_argument(
            user_text
        )

        if fact_id is None:
            answer = (
                "გამოიყენე:\n"
                "/confirm_fact <ID>"
            )

        else:
            try:
                answer = command_confirm_fact(
                    chat_id,
                    fact_id
                )

            except Exception as exc:
                answer = (
                    "ფაქტის დადასტურება ვერ მოხერხდა:\n"
                    + str(exc)
                )

    # --------------------------------------------------------
    # REJECT FACT
    # --------------------------------------------------------

    elif user_text.startswith(
        "/reject_fact"
    ):
        fact_id = parse_integer_argument(
            user_text
        )

        if fact_id is None:
            answer = (
                "გამოიყენე:\n"
                "/reject_fact <ID>"
            )

        else:
            try:
                answer = command_reject_fact(
                    chat_id,
                    fact_id
                )

            except Exception as exc:
                answer = (
                    "ფაქტის უარყოფა ვერ მოხერხდა:\n"
                    + str(exc)
                )

    # --------------------------------------------------------
    # FACT CONFLICTS
    # --------------------------------------------------------

    elif user_text.startswith(
        "/fact_conflicts"
    ):
        try:
            answer = command_fact_conflicts(
                chat_id
            )

        except Exception as exc:
            answer = (
                "კონფლიქტების წაკითხვა ვერ მოხერხდა:\n"
                + str(exc)
            )

    # --------------------------------------------------------
    # MEMORY
    # --------------------------------------------------------

    elif user_text.startswith(
        "/memory"
    ):
        try:
            answer = command_memory(
                chat_id
            )

        except Exception as exc:
            answer = (
                "მეხსიერების წაკითხვა ვერ მოხერხდა:\n"
                + str(exc)
            )

    # --------------------------------------------------------
    # PROJECTS
    # --------------------------------------------------------

    elif user_text.startswith(
        "/projects"
    ):
        try:
            answer = command_projects(
                chat_id
            )

        except Exception as exc:
            answer = (
                "პროექტების წაკითხვა ვერ მოხერხდა:\n"
                + str(exc)
            )

    # --------------------------------------------------------
    # DEVELOPER
    # --------------------------------------------------------

    elif user_text.startswith(
        "/developer"
    ):

        if not developer_access_allowed(
            chat_id
        ):
            answer = (
                "Developer რეჟიმზე წვდომა "
                "დაშვებული არ არის."
            )

        else:
            answer = (
                "Developer რეჟიმი ჩართულია. 🧑‍💻\n\n"
                "/code <დავალება>\n"
                "/debug <დავალება>\n"
                "/code_history"
            )

    # --------------------------------------------------------
    # CODE HISTORY
    # --------------------------------------------------------

    elif user_text.startswith(
        "/code_history"
    ):

        if not developer_access_allowed(
            chat_id
        ):
            answer = (
                "Developer რეჟიმზე წვდომა "
                "დაშვებული არ არის."
            )

        else:
            try:
                answer = command_code_history(
                    chat_id
                )

            except Exception as exc:
                answer = (
                    "კოდის ისტორიის წაკითხვა "
                    "ვერ მოხერხდა:\n"
                    + str(exc)
                )

    # --------------------------------------------------------
    # DEBUG
    # --------------------------------------------------------

    elif user_text.startswith(
        "/debug"
    ):

        if not developer_access_allowed(
            chat_id
        ):
            answer = (
                "Developer რეჟიმზე წვდომა "
                "დაშვებული არ არის."
            )

        else:

            task = user_text[
                len("/debug"):
            ].strip()

            if not task:
                answer = (
                    "მომწერე debugging-ის დავალება "
                    "და კოდი."
                )

            else:

                send_message(
                    chat_id,
                    "Debug Engine მუშაობს... 🔍"
                )

                try:

                    code = extract_code(
                        task
                    )

                    result = developer_debug(
                        chat_id,
                        task,
                        code
                    )

                    answer = result[
                        "answer"
                    ]

                    fixed_code = result[
                        "fixed_code"
                    ]

                    if fixed_code:

                        after = result[
                            "after"
                        ]

                        caption = (
                            "Geniosa 4.3 Debug Engine\n"
                            "Fixed version\n"
                            "Python syntax: "
                            + (
                                "OK"
                                if after[
                                    "syntax_ok"
                                ]
                                else "ERROR"
                            )
                        )

                        try:

                            filepath = create_code_file(
                                fixed_code,
                                "geniosa_fixed.py"
                            )

                            send_document_to_chat(
                                chat_id,
                                filepath,
                                caption
                            )

                        except Exception as exc:

                            answer += (
                                "\n\nფაილის შექმნისას "
                                "მოხდა შეცდომა:\n"
                                + str(exc)
                            )

                        if after[
                            "syntax_ok"
                        ]:

                            answer += (
                                "\n\n✅ გამოსწორებული "
                                "კოდის Python syntax OK."
                            )

                        else:

                            answer += (
                                "\n\n⚠️ გამოსწორებულ "
                                "კოდშიც დარჩა syntax "
                                "პრობლემა:\n"
                                + after[
                                    "syntax_error"
                                ]
                            )

                except Exception as exc:

                    answer = (
                        "Debug Engine-ში მოხდა შეცდომა:\n"
                        + str(exc)
                    )

    # --------------------------------------------------------
    # CODE
    # --------------------------------------------------------

    elif user_text.startswith(
        "/code"
    ):

        if not developer_access_allowed(
            chat_id
        ):
            answer = (
                "Developer რეჟიმზე წვდომა "
                "დაშვებული არ არის."
            )

        else:

            task = user_text[
                len("/code"):
            ].strip()

            if not task:

                answer = (
                    "მომწერე კოდის დავალება."
                )

            else:

                send_message(
                    chat_id,
                    "კოდს ვწერ და ვამოწმებ... ⏳"
                )

                try:

                    result = developer_generate(
                        chat_id,
                        task
                    )

                    answer = result[
                        "answer"
                    ]

                    code = result[
                        "code"
                    ]

                    if code:

                        analysis = result[
                            "analysis"
                        ]

                        caption = (
                            "Geniosa Developer Engine\n"
                            "Python syntax: "
                            + (
                                "OK"
                                if analysis[
                                    "syntax_ok"
                                ]
                                else "ERROR"
                            )
                        )

                        common = analysis[
                            "common_problems"
                        ]

                        if common:

                            caption += (
                                "\nStatic issues: "
                                + str(
                                    len(
                                        common
                                    )
                                )
                            )

                        try:

                            filepath = create_code_file(
                                code,
                                result[
                                    "filename"
                                ]
                            )

                            send_document_to_chat(
                                chat_id,
                                filepath,
                                caption
                            )

                        except Exception as exc:

                            answer += (
                                "\n\nფაილის შექმნისას "
                                "მოხდა შეცდომა:\n"
                                + str(exc)
                            )

                        if analysis[
                            "syntax_ok"
                        ]:

                            answer += (
                                "\n\n✅ Python syntax OK."
                            )

                        else:

                            answer += (
                                "\n\n⚠️ Syntax error:\n"
                                + analysis[
                                    "syntax_error"
                                ]
                            )

                        if common:

                            answer += (
                                "\n\n⚠️ დამატებითი "
                                "პრობლემები:"
                            )

                            for problem in common:

                                answer += (
                                    "\n- "
                                    + problem[
                                        "type"
                                    ]
                                    + ": "
                                    + problem[
                                        "message"
                                    ]
                                )

                except Exception as exc:

                    answer = (
                        "Developer Engine-ში "
                        "მოხდა შეცდომა:\n"
                        + str(exc)
                    )

    # --------------------------------------------------------
    # GENERAL AI
    # --------------------------------------------------------

    else:

        try:

            answer = generate_general_response(
                chat_id,
                user_text
            )

        except Exception as exc:

            print(
                "general response error:",
                repr(exc)
            )

            answer = (
                "პასუხის გენერირებისას მოხდა "
                "ტექნიკური შეცდომა:\n"
                + str(exc)
            )

    # --------------------------------------------------------
    # ADD FACT CONFIRMATION PROMPT
    # --------------------------------------------------------

    if detected_facts:

        answer += (
            "\n\n"
            "📌 Geniosa-მ აღმოაჩინა ახალი ინფორმაცია, "
            "რომელიც ჯერ დადასტურებული ფაქტი არ არის."
            "\n\n"
            "დასადასტურებლად გამოიყენე:"
        )

        for item in detected_facts:

            answer += (
                "\n/confirm_fact "
                + str(
                    item["id"]
                )
            )

        answer += (
            "\n\n"
            "თუ ინფორმაცია არასწორია:"
        )

        for item in detected_facts:

            answer += (
                "\n/reject_fact "
                + str(
                    item["id"]
                )
            )

    # --------------------------------------------------------
    # SEND FINAL ANSWER
    # --------------------------------------------------------

    try:

        send_message(
            chat_id,
            answer
        )

    except Exception as exc:

        print(
            "final send error:",
            repr(exc)
        )

    # --------------------------------------------------------
    # SAVE ASSISTANT MESSAGE
    # --------------------------------------------------------

    try:

        save_message(
            chat_id,
            "assistant",
            answer
        )

    except Exception as exc:

        print(
            "assistant message save error:",
            repr(exc)
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():
    print(
        "Telegram polling thread started."
    )

    offset = None

    while True:

        try:

            params = {
                "timeout": 30
            }

            if offset is not None:
                params[
                    "offset"
                ] = offset

            response = requests.get(
                telegram_url(
                    "getUpdates"
                ),
                params=params,
                timeout=40
            )

            if response.status_code == 409:

                print(
                    "Telegram 409 Conflict. "
                    "Another polling process may be active."
                )

                time.sleep(
                    10
                )

                continue

            if response.status_code != 200:

                print(
                    "getUpdates error:",
                    response.status_code,
                    response.text[:1000]
                )

                time.sleep(
                    5
                )

                continue

            data = response.json()

            if not data.get(
                "ok"
            ):

                print(
                    "getUpdates not ok:",
                    data
                )

                time.sleep(
                    5
                )

                continue

            updates = data.get(
                "result",
                []
            )

            for update in updates:

                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:

                    offset = (
                        update_id
                        + 1
                    )

                try:

                    process_update(
                        update
                    )

                except Exception as exc:

                    print(
                        "process_update error:",
                        repr(exc)
                    )

        except requests.exceptions.Timeout:

            continue

        except Exception as exc:

            print(
                "Polling exception:",
                repr(exc)
            )

            time.sleep(
                5
            )


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
def root():
    return {
        "service": "Geniosa",
        "version": "4.3",
        "status": "online"
    }


@app.get("/health")
def health():
    missing = configuration_status()

    database = "unknown"

    if DATABASE_URL:

        try:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                "SELECT 1"
            )

            cur.fetchone()

            cur.close()
            conn.close()

            database = "ok"

        except Exception as exc:

            database = (
                "error: "
                + str(exc)
            )

    return {
        "service": "Geniosa",
        "version": "4.3",
        "configuration_missing": missing,
        "database": database,
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        ),
        "gemini_model": GEMINI_MODEL
    }


# ============================================================
# STARTUP
# ============================================================

@app.on_event(
    "startup"
)
def startup_event():

    print(
        "=" * 60
    )

    print(
        "GENIOSA 4.3 STARTING"
    )

    print(
        "=" * 60
    )

    missing = configuration_status()

    if missing:

        print(
            "Missing environment variables:",
            missing
        )

    try:

        init_database()

        print(
            "Database initialized successfully."
        )

    except Exception as exc:

        print(
            "Database initialization error:",
            repr(exc)
        )

    if TELEGRAM_BOT_TOKEN:

        try:

            delete_webhook()

        except Exception as exc:

            print(
                "deleteWebhook error:",
                repr(exc)
            )

        thread = threading.Thread(
            target=telegram_polling,
            daemon=True,
            name="telegram-polling"
        )

        thread.start()

    else:

        print(
            "Telegram token missing. "
            "Polling not started."
        )

    print(
        "Geniosa 4.3 startup complete."
    )


# ============================================================
# END
# ============================================================
