import os
import re
import ast
import json
import time
import threading
from datetime import datetime

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI


# ============================================================
# GENIOSA 4.4
# Personal Business Advisor / Economist / Developer
# Telegram + Gemini + PostgreSQL + Memory + Facts
# Projects + Decisions + Tasks + Developer Engine
# Debug Engine + Web Research + Bot Factory
# ============================================================


APP_VERSION = "4.4"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
).strip()

GENIOSA_OWNER_ID = os.getenv(
    "GENIOSA_OWNER_ID",
    ""
).strip()


TELEGRAM_API = (
    "https://api.telegram.org/bot"
    + TELEGRAM_BOT_TOKEN
)

GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent?key="
    + GEMINI_API_KEY
)


app = FastAPI(
    title="Geniosa",
    version=APP_VERSION
)


# ============================================================
# BASIC HELPERS
# ============================================================


def now_utc():
    return datetime.utcnow()


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def json_text(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=str
        )
    except Exception:
        return str(value)


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


def init_db():
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                category TEXT,
                content TEXT NOT NULL,
                source TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_events (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                event_type TEXT,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                decision TEXT NOT NULL,
                status TEXT DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                fact_key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fact_history (
                id SERIAL PRIMARY KEY,
                fact_id INTEGER NOT NULL,
                old_value TEXT,
                new_value TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS code_projects (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS code_versions (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL,
                version TEXT NOT NULL,
                filename TEXT,
                code TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_projects (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_files (
                id SERIAL PRIMARY KEY,
                bot_project_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                version TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS research (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                answer TEXT,
                sources TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.commit()
        cur.close()

        print("Database initialized successfully.")

    except Exception as exc:
        print("Database initialization error:", repr(exc))

        if conn:
            conn.rollback()

    finally:
        if conn:
            conn.close()


# ============================================================
# MESSAGE MEMORY
# ============================================================


def save_message(chat_id, role, message):
    message = clean_text(message)

    if not message:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO messages
            (chat_id, role, message)
            VALUES (%s, %s, %s)
            """,
            (
                safe_int(chat_id),
                clean_text(role),
                message
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("save_message error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_recent_messages(chat_id, limit=20):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT role, message, created_at
            FROM messages
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 20)
            )
        )

        rows = cur.fetchall()

        cur.close()

        rows.reverse()

        return rows

    except Exception as exc:
        print("get_recent_messages error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


def search_messages(chat_id, query, limit=20):
    query = clean_text(query)

    if not query:
        return []

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT role, message, created_at
            FROM messages
            WHERE chat_id = %s
              AND message ILIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                "%" + query + "%",
                safe_int(limit, 20)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("search_messages error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


# ============================================================
# LONG TERM MEMORY
# ============================================================


def save_memory(
    chat_id,
    category,
    content,
    source="user",
    status="PENDING"
):
    content = clean_text(content)

    if not content:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO memories
            (chat_id, category, content, source, status)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                safe_int(chat_id),
                clean_text(category),
                content,
                clean_text(source),
                clean_text(status)
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("save_memory error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_memories(chat_id, limit=30):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, category, content, source, status, created_at
            FROM memories
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 30)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("get_memories error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


def search_memories(chat_id, query, limit=20):
    query = clean_text(query)

    if not query:
        return []

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, category, content, source, status, created_at
            FROM memories
            WHERE chat_id = %s
              AND content ILIKE %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                "%" + query + "%",
                safe_int(limit, 20)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("search_memories error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


def save_memory_event(chat_id, event_type, content):
    content = clean_text(content)

    if not content:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO memory_events
            (chat_id, event_type, content)
            VALUES (%s, %s, %s)
            """,
            (
                safe_int(chat_id),
                clean_text(event_type),
                content
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("save_memory_event error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_memory_events(chat_id, limit=30):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, event_type, content, created_at
            FROM memory_events
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 30)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("get_memory_events error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


# ============================================================
# DECISIONS
# ============================================================


def save_decision(chat_id, decision, status="ACTIVE"):
    decision = clean_text(decision)

    if not decision:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO decisions
            (chat_id, decision, status)
            VALUES (%s, %s, %s)
            """,
            (
                safe_int(chat_id),
                decision,
                clean_text(status)
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("save_decision error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_decisions(chat_id, limit=30):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, decision, status, created_at
            FROM decisions
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 30)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("get_decisions error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


# ============================================================
# TASKS
# ============================================================


def save_task(chat_id, task, status="OPEN"):
    task = clean_text(task)

    if not task:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO tasks
            (chat_id, task, status)
            VALUES (%s, %s, %s)
            """,
            (
                safe_int(chat_id),
                task,
                clean_text(status)
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("save_task error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_tasks(chat_id, limit=30):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, task, status, created_at
            FROM tasks
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 30)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("get_tasks error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


# ============================================================
# PROJECTS
# ============================================================


def create_project(chat_id, name, description=""):
    name = clean_text(name)

    if not name:
        return None

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                safe_int(chat_id),
                name,
                clean_text(description)
            )
        )

        project_id = cur.fetchone()[0]

        conn.commit()
        cur.close()

        return project_id

    except Exception as exc:
        print("create_project error:", repr(exc))

        if conn:
            conn.rollback()

        return None

    finally:
        if conn:
            conn.close()


def get_projects(chat_id, limit=30):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, name, description, status, created_at
            FROM projects
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 30)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("get_projects error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


# ============================================================
# FACT ENGINE
# ============================================================


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


def get_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    if not fact_key:
        return None

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
              AND fact_key = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                safe_int(chat_id),
                fact_key
            )
        )

        row = cur.fetchone()

        cur.close()

        return row

    except Exception as exc:
        print("get_fact error:", repr(exc))
        return None

    finally:
        if conn:
            conn.close()


def create_fact(
    chat_id,
    fact_key,
    value,
    source="user",
    status="PENDING"
):
    fact_key = normalize_fact_key(fact_key)
    value = clean_text(value)

    if not fact_key or not value:
        return None

    existing = get_fact(
        chat_id,
        fact_key
    )

    if existing:
        if clean_text(existing["value"]) == value:
            return existing["id"]

        if existing["status"] == "CONFIRMED":
            conn = None

            try:
                conn = get_db()
                cur = conn.cursor()

                cur.execute(
                    """
                    UPDATE facts
                    SET status = 'CONFLICT',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (existing["id"],)
                )

                cur.execute(
                    """
                    INSERT INTO fact_history
                    (fact_id, old_value, new_value, action)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        existing["id"],
                        existing["value"],
                        value,
                        "CONFLICT"
                    )
                )

                conn.commit()
                cur.close()

            except Exception as exc:
                print("fact conflict error:", repr(exc))

                if conn:
                    conn.rollback()

            finally:
                if conn:
                    conn.close()

            return existing["id"]

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO facts
            (chat_id, fact_key, value, source, status)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                safe_int(chat_id),
                fact_key,
                value,
                clean_text(source),
                clean_text(status)
            )
        )

        fact_id = cur.fetchone()[0]

        conn.commit()
        cur.close()

        return fact_id

    except Exception as exc:
        print("create_fact error:", repr(exc))

        if conn:
            conn.rollback()

        return None

    finally:
        if conn:
            conn.close()


def get_facts(chat_id, limit=100):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 100)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print("get_facts error:", repr(exc))
        return []

    finally:
        if conn:
            conn.close()


def confirm_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    if not fact_key:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, value
            FROM facts
            WHERE chat_id = %s
              AND fact_key = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                safe_int(chat_id),
                fact_key
            )
        )

        row = cur.fetchone()

        if not row:
            cur.close()
            return False

        fact_id = row[0]

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
            INSERT INTO fact_history
            (fact_id, old_value, new_value, action)
            VALUES (%s, %s, %s, %s)
            """,
            (
                fact_id,
                row[1],
                row[1],
                "CONFIRMED"
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("confirm_fact error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def reject_fact(chat_id, fact_key):
    fact_key = normalize_fact_key(fact_key)

    if not fact_key:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE facts
            SET status = 'REJECTED',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id
                FROM facts
                WHERE chat_id = %s
                  AND fact_key = %s
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (
                safe_int(chat_id),
                fact_key
            )
        )

        changed = cur.rowcount > 0

        conn.commit()
        cur.close()

        return changed

    except Exception as exc:
        print("reject_fact error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


# ============================================================
# AUTOMATIC MEMORY DETECTION
# ============================================================


def should_save_memory(text):
    text = clean_text(text).lower()

    keywords = [
        "დაიმახსოვრე",
        "გახსოვდეს",
        "შეინახე",
        "დაიმახსოვრეთ",
        "remember",
        "remember this",
        "save this",
        "keep this"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def should_save_decision(text):
    text = clean_text(text).lower()

    keywords = [
        "გადავწყვიტეთ",
        "გადავწყვიტე",
        "გადაწყვეტილება",
        "ჩვენი გადაწყვეტილებაა",
        "we decided",
        "decision is",
        "decided"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def should_save_task(text):
    text = clean_text(text).lower()

    keywords = [
        "უნდა გავაკეთოთ",
        "უნდა გავაკეთო",
        "შემდეგ გავაკეთოთ",
        "დავალება",
        "task",
        "todo",
        "need to do",
        "we need to"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def capture_memory(chat_id, text):
    text = clean_text(text)

    if not text:
        return

    if should_save_memory(text):
        save_memory(
            chat_id,
            "USER_MEMORY",
            text,
            "user",
            "PENDING"
        )

        save_memory_event(
            chat_id,
            "MEMORY_REQUEST",
            text
        )

    if should_save_decision(text):
        save_decision(
            chat_id,
            text,
            "ACTIVE"
        )

        save_memory_event(
            chat_id,
            "DECISION",
            text
        )

    if should_save_task(text):
        save_task(
            chat_id,
            text,
            "OPEN"
        )

        save_memory_event(
            chat_id,
            "TASK",
            text
        )


# ============================================================
# CONTEXT BUILDER
# ============================================================


def build_context(chat_id):
    messages = get_recent_messages(
        chat_id,
        20
    )

    memories = get_memories(
        chat_id,
        30
    )

    facts = get_facts(
        chat_id,
        100
    )

    decisions = get_decisions(
        chat_id,
        20
    )

    tasks = get_tasks(
        chat_id,
        20
    )

    projects = get_projects(
        chat_id,
        20
    )

    context = []

    context.append(
        "GENIOSA LONG-TERM CONTEXT"
    )

    if facts:
        context.append("\nFACTS:")

        for fact in reversed(facts):
            context.append(
                "- key="
                + clean_text(fact.get("fact_key"))
                + " | value="
                + clean_text(fact.get("value"))
                + " | status="
                + clean_text(fact.get("status"))
            )

    if memories:
        context.append("\nMEMORIES:")

        for memory in reversed(memories[:20]):
            context.append(
                "- "
                + clean_text(memory.get("content"))
                + " ["
                + clean_text(memory.get("status"))
                + "]"
            )

    if decisions:
        context.append("\nDECISIONS:")

        for decision in reversed(decisions[:15]):
            context.append(
                "- "
                + clean_text(decision.get("decision"))
                + " ["
                + clean_text(decision.get("status"))
                + "]"
            )

    if tasks:
        context.append("\nTASKS:")

        for task in reversed(tasks[:15]):
            context.append(
                "- "
                + clean_text(task.get("task"))
                + " ["
                + clean_text(task.get("status"))
                + "]"
            )

    if projects:
        context.append("\nPROJECTS:")

        for project in reversed(projects[:15]):
            context.append(
                "- "
                + clean_text(project.get("name"))
                + ": "
                + clean_text(project.get("description"))
            )

    if messages:
        context.append("\nRECENT CONVERSATION:")

        for message in messages[-15:]:
            context.append(
                clean_text(message.get("role"))
                + ": "
                + clean_text(message.get("message"))
            )

    return "\n".join(context)


# ============================================================
# GENIOSA CONSTITUTION
# ============================================================


GENIOSA_SYSTEM = (
    "You are Geniosa, the user's personal business advisor, "
    "economist, developer, researcher and digital assistant.\n\n"

    "IMPORTANT CONSTITUTION:\n"

    "1. Never invent facts, prices, laws, companies, sources, "
    "financial results or technical capabilities.\n"

    "2. If information is unknown, explicitly say that it is unknown "
    "and explain what information is needed.\n"

    "3. Never present an assumption as a confirmed fact.\n"

    "4. User-provided information is context, but it is not automatically "
    "a CONFIRMED fact unless the user confirms it.\n"

    "5. CONFIRMED facts must not be silently replaced.\n"

    "6. If a new value conflicts with a confirmed value, identify the "
    "CONFLICT and ask for clarification.\n"

    "7. Financial calculations must clearly state assumptions.\n"

    "8. For current prices, current laws, current companies, current "
    "markets, current news or other time-sensitive information, use web "
    "research when available and distinguish researched information from "
    "memory.\n"

    "9. Always distinguish facts, assumptions, estimates and opinions.\n"

    "10. Never claim that an external action was completed unless it was "
    "actually completed.\n"

    "11. Never claim that a bot was deployed, a company contacted, a file "
    "sent or an investment transaction completed unless the system actually "
    "performed that action.\n"

    "12. When generating code, produce syntactically valid and practically "
    "usable code.\n"

    "13. When debugging code, distinguish syntax, runtime, dependency, "
    "configuration and logic errors.\n"

    "14. Do not expose API keys, passwords or secrets.\n"

    "15. Preserve working functionality when improving existing systems.\n"

    "16. The user may ask about any business, company, project, technology "
    "or personal task. Do not limit the assistant to SAMTISI or NIKKEA.\n\n"

    "ROLE:\n"
    "Act as a practical senior advisor. Be direct, structured and honest. "
    "When useful, provide calculations, tables, risks, next steps and "
    "decision options.\n\n"

    "LANGUAGE:\n"
    "Answer in the language used by the user unless the user requests "
    "another language."
)


# ============================================================
# GEMINI
# ============================================================


def extract_gemini_text(data):
    try:
        candidates = data.get("candidates", [])

        if not candidates:
            return ""

        candidate = candidates[0]

        content = candidate.get(
            "content",
            {}
        )

        parts = content.get(
            "parts",
            []
        )

        texts = []

        for part in parts:
            if "text" in part:
                texts.append(
                    clean_text(part["text"])
                )

        return "\n".join(
            item for item in texts if item
        ).strip()

    except Exception:
        return ""


def extract_grounding_sources(data):
    sources = []

    try:
        candidates = data.get(
            "candidates",
            []
        )

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
            web_data = chunk.get(
                "web",
                {}
            )

            uri = web_data.get(
                "uri"
            )

            title = web_data.get(
                "title"
            )

            if uri:
                sources.append(
                    {
                        "title": title or uri,
                        "uri": uri
                    }
                )

    except Exception as exc:
        print(
            "extract_grounding_sources error:",
            repr(exc)
        )

    unique = []
    seen = set()

    for source in sources:
        uri = source.get("uri")

        if uri and uri not in seen:
            seen.add(uri)
            unique.append(source)

    return unique[:20]


def call_gemini(
    prompt,
    use_search=False,
    system_instruction=None
):
    if not GEMINI_API_KEY:
        return {
            "text": "",
            "sources": [],
            "error": "GEMINI_API_KEY is not configured."
        }

    instruction = (
        system_instruction
        if system_instruction
        else GENIOSA_SYSTEM
    )

    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text": instruction
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": clean_text(prompt)
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
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
            GEMINI_API,
            json=payload,
            timeout=120
        )

        if response.status_code != 200:
            return {
                "text": "",
                "sources": [],
                "error": (
                    "Gemini HTTP "
                    + str(response.status_code)
                    + ": "
                    + response.text[:1000]
                )
            }

        data = response.json()

        answer = extract_gemini_text(
            data
        )

        sources = extract_grounding_sources(
            data
        )

        if not answer:
            return {
                "text": "",
                "sources": sources,
                "error": "Gemini returned an empty response."
            }

        return {
            "text": answer,
            "sources": sources,
            "error": ""
        }

    except Exception as exc:
        return {
            "text": "",
            "sources": [],
            "error": repr(exc)
        }


# ============================================================
# WEB SEARCH DETECTION
# ============================================================


def should_use_web_search(text):
    text = clean_text(text).lower()

    keywords = [
        "ინტერნეტში",
        "მოიძიე",
        "მოძებნე",
        "მომიძიე",
        "დღეს",
        "ახლა",
        "უახლესი",
        "ბოლო სიახლეები",
        "ფასი",
        "ფასები",
        "ბაზარი",
        "ინვესტორი",
        "ინვესტორები",
        "კომპანია",
        "კანონი",
        "რეგულაცია",
        "news",
        "latest",
        "today",
        "current",
        "search",
        "internet",
        "price",
        "prices",
        "market",
        "investor",
        "investors",
        "company",
        "law",
        "regulation"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


# ============================================================
# RESEARCH DATABASE
# ============================================================


def save_research(
    chat_id,
    query,
    answer,
    sources
):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO research
            (chat_id, query, answer, sources)
            VALUES (%s, %s, %s, %s)
            """,
            (
                safe_int(chat_id),
                clean_text(query),
                clean_text(answer),
                json.dumps(
                    sources,
                    ensure_ascii=False
                )
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print("save_research error:", repr(exc))

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


# ============================================================
# ANSWER ENGINE
# ============================================================


def generate_answer(chat_id, user_text):
    context = build_context(
        chat_id
    )

    use_search = should_use_web_search(
        user_text
    )

    prompt_parts = [
        "USER REQUEST:",
        clean_text(user_text),
        "",
        "PERSONAL CONTEXT:",
        context,
        "",
        "INSTRUCTIONS:",
        "Answer the user's request directly.",
        "Separate confirmed facts from assumptions.",
        "If information is missing, say exactly what is missing.",
        "Do not invent information.",
    ]

    if use_search:
        prompt_parts.extend(
            [
                "",
                "WEB RESEARCH MODE:",
                "Use current web information if needed.",
                "When using web information, cite or identify sources "
                "in a readable way.",
                "Do not confuse web information with confirmed personal facts."
            ]
        )

    prompt = "\n".join(
        prompt_parts
    )

    result = call_gemini(
        prompt,
        use_search=use_search,
        system_instruction=GENIOSA_SYSTEM
    )

    if not result["text"] and use_search:
        fallback_prompt = "\n".join(
            [
                "USER REQUEST:",
                clean_text(user_text),
                "",
                "PERSONAL CONTEXT:",
                context,
                "",
                "The web research request failed.",
                "Answer only from reliable information you already have.",
                "Clearly state if current information cannot be verified."
            ]
        )

        result = call_gemini(
            fallback_prompt,
            use_search=False,
            system_instruction=GENIOSA_SYSTEM
        )

    if result["text"]:
        if use_search:
            save_research(
                chat_id,
                user_text,
                result["text"],
                result["sources"]
            )

        return result["text"]

    return (
        "ამ მომენტში პასუხის გენერირება ვერ შევძელი.\n\n"
        "ტექნიკური დეტალი: "
        + clean_text(result.get("error"))
    )


# ============================================================
# DEVELOPER ENGINE
# ============================================================


def extract_code(text):
    text = clean_text(text)

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

    return text


def check_python_syntax(code):
    code = clean_text(code)

    if not code:
        return {
            "valid": False,
            "error": "No Python code supplied."
        }

    try:
        ast.parse(code)

        return {
            "valid": True,
            "error": ""
        }

    except SyntaxError as exc:
        return {
            "valid": False,
            "error": (
                str(exc)
                + " | line="
                + str(exc.lineno)
                + " | column="
                + str(exc.offset)
            )
        }

    except Exception as exc:
        return {
            "valid": False,
            "error": repr(exc)
        }


def detect_common_python_problems(code):
    problems = []

    if "except:" in code:
        problems.append(
            "Bare except detected. Prefer a specific exception."
        )

    if "eval(" in code:
        problems.append(
            "eval() detected. This can be unsafe."
        )

    if "exec(" in code:
        problems.append(
            "exec() detected. This can be unsafe."
        )

    if "os.system(" in code:
        problems.append(
            "os.system() detected. Review command execution security."
        )

    if "subprocess" in code:
        problems.append(
            "subprocess usage detected. Review external command execution."
        )

    if "password =" in code.lower():
        problems.append(
            "Possible hardcoded password detected."
        )

    if "api_key =" in code.lower():
        problems.append(
            "Possible hardcoded API key detected."
        )

    if "token =" in code.lower():
        problems.append(
            "Possible hardcoded token detected."
        )

    return problems


def analyze_python_code(code):
    syntax = check_python_syntax(
        code
    )

    return {
        "syntax": syntax,
        "common_problems": detect_common_python_problems(
            code
        )
    }


def save_code_project(
    chat_id,
    name,
    description=""
):
    name = clean_text(name)

    if not name:
        return None

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO code_projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                safe_int(chat_id),
                name,
                clean_text(description)
            )
        )

        project_id = cur.fetchone()[0]

        conn.commit()
        cur.close()

        return project_id

    except Exception as exc:
        print(
            "save_code_project error:",
            repr(exc)
        )

        if conn:
            conn.rollback()

        return None

    finally:
        if conn:
            conn.close()


def save_code_version(
    project_id,
    version,
    filename,
    code,
    description=""
):
    if not project_id:
        return False

    code = clean_text(code)

    if not code:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO code_versions
            (project_id, version, filename, code, description)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                safe_int(project_id),
                clean_text(version),
                clean_text(filename),
                code,
                clean_text(description)
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print(
            "save_code_version error:",
            repr(exc)
        )

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_code_history(chat_id, limit=50):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT
                cp.id AS project_id,
                cp.name,
                cv.version,
                cv.filename,
                cv.description,
                cv.created_at
            FROM code_projects cp
            JOIN code_versions cv
              ON cv.project_id = cp.id
            WHERE cp.chat_id = %s
            ORDER BY cv.id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 50)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print(
            "get_code_history error:",
            repr(exc)
        )
        return []

    finally:
        if conn:
            conn.close()


def developer_engine(chat_id, task):
    task = clean_text(task)

    prompt = "\n".join(
        [
            "You are Geniosa Developer Engine.",
            "",
            "USER REQUEST:",
            task,
            "",
            "RULES:",
            "Generate practical, complete and syntactically valid code.",
            "Do not invent unavailable APIs.",
            "Do not hardcode secrets.",
            "If dependencies are required, list them separately.",
            "Explain how to use the code.",
            "If requirements are unclear, state assumptions.",
            "",
            "Return the answer in a clear structure:",
            "1. Explanation",
            "2. Complete code",
            "3. Dependencies",
            "4. Run instructions",
            "5. Important risks"
        ]
    )

    result = call_gemini(
        prompt,
        use_search=False,
        system_instruction=GENIOSA_SYSTEM
    )

    return result["text"] or (
        "Developer Engine error: "
        + clean_text(result["error"])
    )


# ============================================================
# DEBUG ENGINE
# ============================================================


def debug_engine(chat_id, task, code):
    task = clean_text(task)
    code = clean_text(code)

    before = analyze_python_code(
        code
    )

    prompt_parts = [
        "You are Geniosa Debug Engine.",
        "",
        "USER DEBUG REQUEST:",
        task,
        "",
        "CURRENT CODE:",
        "[CODE START]",
        code,
        "[CODE END]",
        "",
        "CURRENT STATIC ANALYSIS:",
        json.dumps(
            before,
            ensure_ascii=False,
            indent=2
        ),
        "",
        "TASK:",
        "Find the real problem.",
        "Distinguish syntax, runtime, dependency, configuration and logic errors.",
        "Return corrected complete code when possible.",
        "Do not use triple-backtick Markdown fences.",
        "Do not invent missing environment variables.",
        "Never expose secrets.",
        "",
        "OUTPUT:",
        "1. Root cause",
        "2. Corrected code",
        "3. Required dependencies",
        "4. Required configuration",
        "5. Test steps"
    ]

    prompt = "\n".join(
        prompt_parts
    )

    result = call_gemini(
        prompt,
        use_search=False,
        system_instruction=GENIOSA_SYSTEM
    )

    answer = result["text"]

    if not answer:
        answer = (
            "Debug Engine error: "
            + clean_text(result["error"])
        )

    return answer


# ============================================================
# BOT FACTORY
# ============================================================


def create_bot_project(
    chat_id,
    name,
    description=""
):
    name = clean_text(name)

    if not name:
        return None

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO bot_projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                safe_int(chat_id),
                name,
                clean_text(description)
            )
        )

        bot_id = cur.fetchone()[0]

        conn.commit()
        cur.close()

        return bot_id

    except Exception as exc:
        print(
            "create_bot_project error:",
            repr(exc)
        )

        if conn:
            conn.rollback()

        return None

    finally:
        if conn:
            conn.close()


def save_bot_file(
    chat_id,
    bot_project_id,
    filename,
    content,
    version="1.0"
):
    filename = clean_text(filename)
    content = clean_text(content)

    if not filename or not content:
        return False

    conn = None

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id
            FROM bot_projects
            WHERE id = %s
              AND chat_id = %s
            """,
            (
                safe_int(bot_project_id),
                safe_int(chat_id)
            )
        )

        if not cur.fetchone():
            cur.close()
            return False

        cur.execute(
            """
            INSERT INTO bot_files
            (bot_project_id, filename, content, version)
            VALUES (%s, %s, %s, %s)
            """,
            (
                safe_int(bot_project_id),
                filename,
                content,
                clean_text(version)
            )
        )

        conn.commit()
        cur.close()

        return True

    except Exception as exc:
        print(
            "save_bot_file error:",
            repr(exc)
        )

        if conn:
            conn.rollback()

        return False

    finally:
        if conn:
            conn.close()


def get_bot_projects(chat_id, limit=30):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT id, name, description, status, created_at
            FROM bot_projects
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                safe_int(chat_id),
                safe_int(limit, 30)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print(
            "get_bot_projects error:",
            repr(exc)
        )
        return []

    finally:
        if conn:
            conn.close()


def get_bot_files(
    chat_id,
    bot_project_id,
    limit=100
):
    conn = None

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT
                bf.id,
                bf.filename,
                bf.content,
                bf.version,
                bf.created_at
            FROM bot_files bf
            JOIN bot_projects bp
              ON bp.id = bf.bot_project_id
            WHERE bp.id = %s
              AND bp.chat_id = %s
            ORDER BY bf.id DESC
            LIMIT %s
            """,
            (
                safe_int(bot_project_id),
                safe_int(chat_id),
                safe_int(limit, 100)
            )
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    except Exception as exc:
        print(
            "get_bot_files error:",
            repr(exc)
        )
        return []

    finally:
        if conn:
            conn.close()


def bot_factory_create(
    chat_id,
    name,
    description
):
    name = clean_text(name)
    description = clean_text(description)

    if not name:
        return (
            "მომეცი ბოტის სახელი.\n"
            "მაგალითად: /bot_create InvestorBot ინვესტორების ბოტი"
        )

    bot_id = create_bot_project(
        chat_id,
        name,
        description
    )

    if not bot_id:
        return "ბოტის პროექტის შექმნა ვერ მოხერხდა."

    main_py = (
        "import os\n"
        "import requests\n\n"
        "TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')\n"
        "API = 'https://api.telegram.org/bot' + TOKEN\n\n"
        "def send_message(chat_id, text):\n"
        "    requests.post(\n"
        "        API + '/sendMessage',\n"
        "        json={'chat_id': chat_id, 'text': text},\n"
        "        timeout=30\n"
        "    )\n"
    )

    requirements = (
        "requests\n"
    )

    env_example = (
        "TELEGRAM_BOT_TOKEN=\n"
    )

    readme = (
        "Bot project: "
        + name
        + "\n\n"
        + description
        + "\n\n"
        + "This project is generated by Geniosa Bot Factory.\n"
        + "BotFather creation and deployment are not automatic in this version.\n"
    )

    save_bot_file(
        chat_id,
        bot_id,
        "main.py",
        main_py,
        "1.0"
    )

    save_bot_file(
        chat_id,
        bot_id,
        "requirements.txt",
        requirements,
        "1.0"
    )

    save_bot_file(
        chat_id,
        bot_id,
        ".env.example",
        env_example,
        "1.0"
    )

    save_bot_file(
        chat_id,
        bot_id,
        "README.md",
        readme,
        "1.0"
    )

    return (
        "Bot Factory: პროექტი შეიქმნა.\n\n"
        "ID: "
        + str(bot_id)
        + "\n"
        "სახელი: "
        + name
        + "\n\n"
        "შექმნილია:\n"
        "- main.py\n"
        "- requirements.txt\n"
        "- .env.example\n"
        "- README.md\n\n"
        "შემდეგ შეგვიძლია ამ ბოტის ფუნქციები დავაპროგრამოთ."
    )


def bot_factory_update(
    chat_id,
    bot_id,
    request_text
):
    bot_id = safe_int(
        bot_id
    )

    if bot_id <= 0:
        return "ბოტის ID არასწორია."

    files = get_bot_files(
        chat_id,
        bot_id
    )

    if not files:
        return (
            "ამ ID-ით ბოტის პროექტი ვერ მოიძებნა "
            "ან თქვენ არ გაქვთ მასზე წვდომა."
        )

    existing_code = []

    for item in files:
        existing_code.append(
            "FILE: "
            + clean_text(item["filename"])
            + "\n"
            + "[FILE START]\n"
            + clean_text(item["content"])
            + "\n[FILE END]\n"
        )

    prompt = "\n".join(
        [
            "You are Geniosa Bot Factory Update Engine.",
            "",
            "USER REQUEST:",
            clean_text(request_text),
            "",
            "EXISTING BOT FILES:",
            "\n".join(existing_code),
            "",
            "TASK:",
            "Update the bot according to the user's request.",
            "Preserve working functionality.",
            "Return complete replacement files when necessary.",
            "Do not use Markdown triple-backtick fences.",
            "Do not invent secrets.",
            "Clearly name every file that must be changed."
        ]
    )

    result = call_gemini(
        prompt,
        use_search=False,
        system_instruction=GENIOSA_SYSTEM
    )

    return result["text"] or (
        "Bot Factory update error: "
        + clean_text(result["error"])
    )


# ============================================================
# TELEGRAM API
# ============================================================


def telegram_call(
    method,
    payload=None,
    timeout=30
):
    if not TELEGRAM_BOT_TOKEN:
        return {
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN is not configured."
        }

    url = (
        TELEGRAM_API
        + "/"
        + clean_text(method)
    )

    try:
        response = requests.post(
            url,
            json=payload or {},
            timeout=timeout
        )

        try:
            return response.json()

        except Exception:
            return {
                "ok": False,
                "error": response.text[:1000]
            }

    except Exception as exc:
        return {
            "ok": False,
            "error": repr(exc)
        }


def send_message(
    chat_id,
    text
):
    text = clean_text(text)

    if not text:
        return False

    max_length = 3900

    parts = []

    while len(text) > max_length:
        split_at = text.rfind(
            "\n",
            0,
            max_length
        )

        if split_at < 1000:
            split_at = max_length

        parts.append(
            text[:split_at]
        )

        text = text[split_at:].lstrip()

    if text:
        parts.append(text)

    success = True

    for part in parts:
        result = telegram_call(
            "sendMessage",
            {
                "chat_id": safe_int(chat_id),
                "text": part
            },
            timeout=30
        )

        if not result.get("ok"):
            print(
                "send_message error:",
                result
            )

            success = False

    return success


def delete_webhook():
    result = telegram_call(
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

    return result


# ============================================================
# TELEGRAM COMMAND HELPERS
# ============================================================


def command_parts(text):
    text = clean_text(text)

    if not text.startswith("/"):
        return "", []

    parts = text.split()

    command = parts[0]

    if "@" in command:
        command = command.split(
            "@",
            1
        )[0]

    return (
        command.lower(),
        parts[1:]
    )


def command_help():
    return (
        "Geniosa 4.4\n\n"
        "ძირითადი ბრძანებები:\n"
        "/start - დაწყება\n"
        "/help - დახმარება\n"
        "/memory - მეხსიერება\n"
        "/facts - ფაქტები\n"
        "/projects - პროექტები\n"
        "/decisions - გადაწყვეტილებები\n"
        "/tasks - დავალებები\n"
        "/code - Developer Engine\n"
        "/code_history - კოდის ისტორია\n"
        "/bots - Bot Factory პროექტები\n"
        "/bot_create - ახალი ბოტის პროექტი\n"
        "/bot_code - ბოტის ფაილების ნახვა\n"
        "/bot_update - ბოტის განახლება\n"
        "/bot_history - ბოტის ფაილების ისტორია\n"
        "/debug - კოდის დიაგნოსტიკა\n\n"
        "ჩვეულებრივ ტექსტზე უბრალოდ მომწერე კითხვა."
    )


def format_rows(rows, title, fields):
    if not rows:
        return title + "\n\nცარიელია."

    lines = [
        title,
        ""
    ]

    for index, row in enumerate(rows, start=1):
        lines.append(
            str(index)
            + ". "
            + " | ".join(
                clean_text(
                    row.get(field, "")
                )
                for field in fields
            )
        )

    return "\n".join(lines)


# ============================================================
# COMMAND HANDLERS
# ============================================================


def handle_command(
    chat_id,
    text
):
    command, args = command_parts(
        text
    )

    if command == "/start":
        return (
            "გამარჯობა. მე ვარ Geniosa 4.4.\n\n"
            "მე ვარ შენი პირადი ბიზნეს-მრჩეველი, "
            "ეკონომისტი, დეველოპერი, მკვლევარი და "
            "ციფრული ასისტენტი.\n\n"
            "შემიძლია დაგეხმარო ბიზნესში, ფინანსებში, "
            "ინვესტიციებში, მშენებლობაში, უძრავ ქონებაში, "
            "ტექნოლოგიაში, კოდში, კვლევასა და პროექტების მართვაში.\n\n"
            "დაწერე /help ბრძანებების სანახავად."
        )

    if command == "/help":
        return command_help()

    if command == "/memory":
        rows = get_memories(
            chat_id,
            30
        )

        return format_rows(
            rows,
            "Geniosa Memory",
            [
                "category",
                "content",
                "status"
            ]
        )

    if command == "/facts":
        rows = get_facts(
            chat_id,
            100
        )

        return format_rows(
            rows,
            "Geniosa Facts",
            [
                "fact_key",
                "value",
                "status"
            ]
        )

    if command == "/projects":
        rows = get_projects(
            chat_id,
            30
        )

        return format_rows(
            rows,
            "Projects",
            [
                "id",
                "name",
                "status"
            ]
        )

    if command == "/decisions":
        rows = get_decisions(
            chat_id,
            30
        )

        return format_rows(
            rows,
            "Decisions",
            [
                "id",
                "decision",
                "status"
            ]
        )

    if command == "/tasks":
        rows = get_tasks(
            chat_id,
            30
        )

        return format_rows(
            rows,
            "Tasks",
            [
                "id",
                "task",
                "status"
            ]
        )

    if command == "/code_history":
        rows = get_code_history(
            chat_id,
            50
        )

        return format_rows(
            rows,
            "Code History",
            [
                "project_id",
                "name",
                "version",
                "filename"
            ]
        )

    if command == "/code":
        request_text = " ".join(args).strip()

        if not request_text:
            return (
                "Developer Engine-ის გამოსაყენებლად დაწერე:\n\n"
                "/code Telegram ბოტის შექმნა Python-ში"
            )

        return developer_engine(
            chat_id,
            request_text
        )

    if command == "/debug":
        request_text = " ".join(args).strip()

        return (
            "Debug Engine-ისთვის მომწერე მოთხოვნა და შემდეგ "
            "კოდის ბლოკი.\n\n"
            "მაგალითად:\n"
            "/debug ეს კოდი არ ეშვება\n"
            "შემდეგ გამოაგზავნე Python კოდი."
        )

    if command == "/bots":
        rows = get_bot_projects(
            chat_id,
            30
        )

        return format_rows(
            rows,
            "Bot Factory Projects",
            [
                "id",
                "name",
                "status"
            ]
        )

    if command == "/bot_create":
        if not args:
            return (
                "ფორმატი:\n"
                "/bot_create BotName აღწერა\n\n"
                "მაგალითად:\n"
                "/bot_create InvestorBot ინვესტორებისთვის"
            )

        name = args[0]

        description = " ".join(
            args[1:]
        )

        return bot_factory_create(
            chat_id,
            name,
            description
        )

    if command == "/bot_code":
        if not args:
            return (
                "ფორმატი:\n"
                "/bot_code BOT_ID"
            )

        bot_id = safe_int(
            args[0]
        )

        if bot_id <= 0:
            return "BOT_ID არასწორია."

        files = get_bot_files(
            chat_id,
            bot_id
        )

        if not files:
            return (
                "ბოტი ვერ მოიძებნა ან "
                "თქვენ არ გაქვთ მასზე წვდომა."
            )

        result = [
            "Bot files:",
            ""
        ]

        for item in files:
            result.append(
                "FILE: "
                + clean_text(item["filename"])
                + " | VERSION: "
                + clean_text(item["version"])
            )

        return "\n".join(
            result
        )

    if command == "/bot_update":
        if len(args) < 2:
            return (
                "ფორმატი:\n"
                "/bot_update BOT_ID რა უნდა შეიცვალოს"
            )

        bot_id = safe_int(
            args[0]
        )

        request_text = " ".join(
            args[1:]
        )

        return bot_factory_update(
            chat_id,
            bot_id,
            request_text
        )

    if command == "/bot_history":
        if not args:
            return (
                "ფორმატი:\n"
                "/bot_history BOT_ID"
            )

        bot_id = safe_int(
            args[0]
        )

        files = get_bot_files(
            chat_id,
            bot_id,
            100
        )

        if not files:
            return "ბოტის ისტორია ვერ მოიძებნა."

        lines = [
            "Bot History",
            ""
        ]

        for item in files:
            lines.append(
                str(item["id"])
                + " | "
                + clean_text(item["filename"])
                + " | "
                + clean_text(item["version"])
                + " | "
                + clean_text(item["created_at"])
            )

        return "\n".join(
            lines
        )

    return None


# ============================================================
# DEBUG MESSAGE PARSER
# ============================================================


def parse_debug_message(text):
    text = clean_text(text)

    matches = re.findall(
        r"```(?:python|py)?\s*(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    if not matches:
        return None, None

    code = max(
        matches,
        key=len
    ).strip()

    task_text = re.sub(
        r"```(?:python|py)?\s*.*?```",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE
    ).strip()

    return (
        task_text,
        code
    )


# ============================================================
# TELEGRAM UPDATE PROCESSING
# ============================================================


def process_update(update):
    chat_id = None

    try:
        message = update.get(
            "message"
        )

        if not message:
            return

        chat = message.get(
            "chat",
            {}
        )

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

        text = clean_text(text)

        if not text:
            return

        save_message(
            chat_id,
            "user",
            text
        )

        capture_memory(
            chat_id,
            text
        )

        command, args = command_parts(
            text
        )

        if command == "/debug":
            task_text, code = parse_debug_message(
                text
            )

            if code:
                result = debug_engine(
                    chat_id,
                    task_text or "გაასწორე ეს კოდი.",
                    code
                )

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

        command_answer = handle_command(
            chat_id,
            text
        )

        if command_answer is not None:
            save_message(
                chat_id,
                "assistant",
                command_answer
            )

            send_message(
                chat_id,
                command_answer
            )

            return

        answer = generate_answer(
            chat_id,
            text
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

    except Exception as exc:
        print(
            "process_update error:",
            repr(exc)
        )

        if chat_id:
            error_text = (
                "Geniosa-ს შიდა შეცდომა დაფიქსირდა.\n\n"
                "ტექნიკური დეტალი: "
                + repr(exc)
            )

            send_message(
                chat_id,
                error_text
            )


# ============================================================
# TELEGRAM POLLING
# ============================================================


_polling_started = False
_polling_lock = threading.Lock()


def telegram_polling():
    global _polling_started

    with _polling_lock:
        if _polling_started:
            return

        _polling_started = True

    print(
        "Telegram polling thread started."
    )

    offset = None

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

                time.sleep(5)

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:
                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:
                    offset = (
                        safe_int(update_id)
                        + 1
                    )

                process_update(
                    update
                )

        except Exception as exc:
            print(
                "telegram_polling error:",
                repr(exc)
            )

            time.sleep(5)


# ============================================================
# FASTAPI
# ============================================================


@app.get("/")
def root():
    return {
        "service": "Geniosa",
        "version": APP_VERSION,
        "status": "running"
    }


@app.get("/health")
def health():
    database_ok = False

    try:
        conn = get_db()
        conn.close()

        database_ok = True

    except Exception:
        database_ok = False

    return {
        "service": "Geniosa",
        "version": APP_VERSION,
        "database": database_ok,
        "telegram_configured": bool(
            TELEGRAM_BOT_TOKEN
        ),
        "gemini_configured": bool(
            GEMINI_API_KEY
        )
    }


# ============================================================
# STARTUP
# ============================================================


@app.on_event("startup")
def startup_event():
    print(
        "============================================================"
    )

    print(
        "GENIOSA "
        + APP_VERSION
        + " STARTING"
    )

    print(
        "============================================================"
    )

    init_db()

    if TELEGRAM_BOT_TOKEN:
        delete_webhook()

        thread = threading.Thread(
            target=telegram_polling,
            daemon=True
        )

        thread.start()

    else:
        print(
            "WARNING: TELEGRAM_BOT_TOKEN is not configured."
        )

    if not GEMINI_API_KEY:
        print(
            "WARNING: GEMINI_API_KEY is not configured."
        )

    if not DATABASE_URL:
        print(
            "WARNING: DATABASE_URL is not configured."
        )

    print(
        "Geniosa "
        + APP_VERSION
        + " startup complete."
    )


# ============================================================
# LOCAL START
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
