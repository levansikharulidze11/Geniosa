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
# GENIOSA 4.5
# Personal Business Advisor / Economist / Developer
# ============================================================

APP_VERSION = "4.5"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GENIOSA_OWNER_ID = os.getenv("GENIOSA_OWNER_ID", "").strip()

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    if TELEGRAM_BOT_TOKEN
    else ""
)

GEMINI_API = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    if GEMINI_API_KEY
    else ""
)

app = FastAPI(
    title="Geniosa",
    version=APP_VERSION
)


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
    conn = get_db()
    cur = conn.cursor()

    statements = [

        """
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            memory TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS decisions (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            decision TEXT NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            task TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS facts (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            source TEXT DEFAULT 'user',
            source_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS fact_history (
            id SERIAL PRIMARY KEY,
            fact_id INTEGER,
            chat_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS code_projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS code_versions (
            id SERIAL PRIMARY KEY,
            project_id INTEGER,
            chat_id BIGINT NOT NULL,
            version TEXT,
            code TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS bot_projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS bot_files (
            id SERIAL PRIMARY KEY,
            bot_project_id INTEGER,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS research (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            query TEXT NOT NULL,
            answer TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS financial_models (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            project_name TEXT NOT NULL,
            currency TEXT DEFAULT 'USD',
            model_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS financial_history (
            id SERIAL PRIMARY KEY,
            model_id INTEGER,
            chat_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            model_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]

    for statement in statements:
        cur.execute(statement)

    # Safe migrations for existing installations
    migrations = [
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS source_message_id INTEGER
        """,
        """
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'user'
        """,
    ]

    for statement in migrations:
        cur.execute(statement)

    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# SAFE DATABASE HELPERS
# ============================================================

def db_fetchall(query, params=()):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(query, params)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def db_fetchone(query, params=()):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(query, params)
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


# ============================================================
# MESSAGES
# ============================================================

def save_message(chat_id, role, message):
    if message is None:
        message = ""

    message = str(message).strip()

    if not message:
        return None

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO messages
            (chat_id, role, message)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (chat_id, role, message)
        )

        message_id = cur.fetchone()[0]
        conn.commit()
        return message_id

    finally:
        cur.close()
        conn.close()


def get_recent_messages(chat_id, limit=20):
    rows = db_fetchall(
        """
        SELECT role, message, created_at
        FROM messages
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )

    rows.reverse()
    return rows


def search_messages(chat_id, query, limit=20):
    return db_fetchall(
        """
        SELECT role, message, created_at
        FROM messages
        WHERE chat_id = %s
          AND message ILIKE %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, f"%{query}%", limit)
    )


# ============================================================
# MEMORY
# ============================================================

def save_memory(chat_id, memory):
    memory = str(memory).strip()

    if not memory:
        return

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO memories
            (chat_id, memory)
            VALUES (%s, %s)
            """,
            (chat_id, memory)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_memories(chat_id, limit=50):
    return db_fetchall(
        """
        SELECT id, memory, created_at
        FROM memories
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


def search_memories(chat_id, query, limit=20):
    return db_fetchall(
        """
        SELECT id, memory, created_at
        FROM memories
        WHERE chat_id = %s
          AND memory ILIKE %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, f"%{query}%", limit)
    )


def save_memory_event(chat_id, event_type, content):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO memory_events
            (chat_id, event_type, content)
            VALUES (%s, %s, %s)
            """,
            (chat_id, event_type, content)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_memory_events(chat_id, limit=50):
    return db_fetchall(
        """
        SELECT id, event_type, content, created_at
        FROM memory_events
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


# ============================================================
# DECISIONS
# ============================================================

def save_decision(chat_id, decision):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO decisions
            (chat_id, decision)
            VALUES (%s, %s)
            """,
            (chat_id, decision)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_decisions(chat_id, limit=50):
    return db_fetchall(
        """
        SELECT id, decision, status, created_at
        FROM decisions
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


# ============================================================
# TASKS
# ============================================================

def save_task(chat_id, task):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO tasks
            (chat_id, task)
            VALUES (%s, %s)
            """,
            (chat_id, task)
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_tasks(chat_id, limit=50):
    return db_fetchall(
        """
        SELECT id, task, status, created_at
        FROM tasks
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


# ============================================================
# PROJECTS
# ============================================================

def save_project(chat_id, name, description=""):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (chat_id, name, description)
        )

        project_id = cur.fetchone()[0]
        conn.commit()
        return project_id

    finally:
        cur.close()
        conn.close()


def get_projects(chat_id, limit=50):
    return db_fetchall(
        """
        SELECT id, name, description, status, created_at
        FROM projects
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


# ============================================================
# FACT ENGINE
# ============================================================

def normalize_fact_key(key):
    key = str(key or "").strip().lower()
    key = re.sub(r"\s+", "_", key)
    key = re.sub(r"[^a-zA-Z0-9ა-ჰ_]+", "", key)

    return key[:150]


def get_confirmed_fact(chat_id, fact_key):
    key = normalize_fact_key(fact_key)

    return db_fetchone(
        """
        SELECT *
        FROM facts
        WHERE chat_id = %s
          AND fact_key = %s
          AND status = 'CONFIRMED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (chat_id, key)
    )


def get_latest_fact(chat_id, fact_key):
    key = normalize_fact_key(fact_key)

    return db_fetchone(
        """
        SELECT *
        FROM facts
        WHERE chat_id = %s
          AND fact_key = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (chat_id, key)
    )


def create_fact(
    chat_id,
    fact_key,
    fact_value,
    status="PENDING",
    source="user",
    source_message_id=None
):
    key = normalize_fact_key(fact_key)
    value = str(fact_value).strip()

    if not key or not value:
        return None

    confirmed = get_confirmed_fact(chat_id, key)

    # Same confirmed value: don't duplicate it.
    if confirmed and str(confirmed["fact_value"]).strip() == value:
        return confirmed["id"]

    # Different value from confirmed value:
    # preserve confirmed fact and create conflict candidate.
    if confirmed and str(confirmed["fact_value"]).strip() != value:
        status = "CONFLICT"

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO facts
            (
                chat_id,
                fact_key,
                fact_value,
                status,
                source,
                source_message_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                chat_id,
                key,
                value,
                status,
                source,
                source_message_id
            )
        )

        fact_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO fact_history
            (
                fact_id,
                chat_id,
                action,
                old_value,
                new_value,
                details
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                fact_id,
                chat_id,
                "CREATED",
                confirmed["fact_value"] if confirmed else None,
                value,
                f"New fact created with status {status}"
            )
        )

        conn.commit()
        return fact_id

    finally:
        cur.close()
        conn.close()


def get_facts(chat_id, limit=100):
    return db_fetchall(
        """
        SELECT
            id,
            fact_key,
            fact_value,
            status,
            source,
            source_message_id,
            created_at
        FROM facts
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


def confirm_fact(chat_id, fact_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute(
            """
            SELECT *
            FROM facts
            WHERE id = %s
              AND chat_id = %s
            """,
            (fact_id, chat_id)
        )

        fact = cur.fetchone()

        if not fact:
            conn.rollback()
            return False

        key = fact["fact_key"]
        value = fact["fact_value"]

        cur.execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
              AND fact_key = %s
              AND status = 'CONFIRMED'
              AND id <> %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, key, fact_id)
        )

        old = cur.fetchone()

        if old and str(old["fact_value"]).strip() != value:
            cur.execute(
                """
                UPDATE facts
                SET status = 'REJECTED'
                WHERE id = %s
                """,
                (old["id"],)
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
                    details
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    old["id"],
                    chat_id,
                    "SUPERSEDED",
                    old["fact_value"],
                    value,
                    f"Superseded by confirmed fact {fact_id}"
                )
            )

        cur.execute(
            """
            UPDATE facts
            SET status = 'CONFIRMED'
            WHERE id = %s
              AND chat_id = %s
            """,
            (fact_id, chat_id)
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
                details
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                fact_id,
                chat_id,
                "CONFIRMED",
                fact["fact_value"],
                fact["fact_value"],
                "User confirmed fact"
            )
        )

        conn.commit()
        return True

    finally:
        cur.close()
        conn.close()


def reject_fact(chat_id, fact_id):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            UPDATE facts
            SET status = 'REJECTED'
            WHERE id = %s
              AND chat_id = %s
            """,
            (fact_id, chat_id)
        )

        cur.execute(
            """
            INSERT INTO fact_history
            (
                fact_id,
                chat_id,
                action,
                details
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                fact_id,
                chat_id,
                "REJECTED",
                "User rejected fact"
            )
        )

        conn.commit()
        return cur.rowcount > 0

    finally:
        cur.close()
        conn.close()


def get_fact_history(chat_id, limit=100):
    return db_fetchall(
        """
        SELECT
            id,
            fact_id,
            action,
            old_value,
            new_value,
            details,
            created_at
        FROM fact_history
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


# ============================================================
# AUTOMATIC MEMORY / FACT DETECTION
# ============================================================

def contains_any(text, words):
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def should_save_memory(text):
    return contains_any(
        text,
        [
            "დაიმახსოვრე",
            "დაიმახსოვრეთ",
            "შეინახე",
            "შეინახე ეს",
            "remember this",
            "remember that",
            "save this",
            "memorize"
        ]
    )


def should_save_decision(text):
    return contains_any(
        text,
        [
            "გადავწყვიტეთ",
            "გადავწყვიტე",
            "გადაწყვეტილია",
            "decision",
            "we decided"
        ]
    )


def should_save_task(text):
    return contains_any(
        text,
        [
            "უნდა გავაკეთოთ",
            "გასაკეთებელია",
            "შემდეგ გავაკეთოთ",
            "task",
            "todo",
            "მომავალში გააკეთე"
        ]
    )


def capture_memory(chat_id, text):
    if should_save_memory(text):
        save_memory(chat_id, text)
        save_memory_event(chat_id, "MEMORY_CREATED", text)

    if should_save_decision(text):
        save_decision(chat_id, text)
        save_memory_event(chat_id, "DECISION_CREATED", text)

    if should_save_task(text):
        save_task(chat_id, text)
        save_memory_event(chat_id, "TASK_CREATED", text)


# ============================================================
# GEMINI
# ============================================================

GENIOSA_SYSTEM = """
You are Geniosa.

You are the user's personal business advisor, economist, developer,
research assistant and digital assistant.

CORE CONSTITUTION:

1. Never invent facts, prices, laws, regulations, market data, people,
companies, financial results or external actions.

2. If something is unknown, clearly say that it is unknown.

3. Never present an assumption as a confirmed fact.

4. Distinguish clearly between:
   - CONFIRMED FACT
   - USER-PROVIDED BUT UNCONFIRMED INFORMATION
   - ASSUMPTION
   - ESTIMATE
   - OPINION
   - RESEARCH RESULT

5. Confirmed facts must never be silently replaced.

6. If two values conflict, identify the conflict and ask which one should
become the confirmed value.

7. Financial calculations must clearly state assumptions and formulas.

8. Current market, legal, financial or other time-sensitive information
should be researched when appropriate.

9. Do not claim that an email was sent, a file was uploaded, code was
deployed, a company was contacted, money was transferred or any other
external action happened unless it actually happened.

10. Never expose API keys, passwords, database credentials or other secrets.

11. When generating code, provide practical working code and distinguish:
syntax errors, runtime errors, dependency errors, configuration errors
and logic errors.

12. Do not automatically deploy or execute production code.

13. Preserve existing working functionality when improving the system.

14. Geniosa is not limited to SAMTISI or NIKKEA. It is the user's general
personal business advisor.

Answer in the same language as the user unless the user requests another
language.

Be direct, practical and honest.
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
            if isinstance(part, dict) and "text" in part:
                texts.append(part["text"])

        return "\n".join(texts).strip()

    except Exception:
        return ""


def extract_grounding_sources(data):
    sources = []

    try:
        candidates = data.get("candidates", [])

        for candidate in candidates:
            metadata = candidate.get("groundingMetadata", {})

            chunks = metadata.get("groundingChunks", [])

            for chunk in chunks:
                web = chunk.get("web", {})

                uri = web.get("uri")
                title = web.get("title")

                if uri:
                    sources.append({
                        "title": title or uri,
                        "uri": uri
                    })

    except Exception:
        pass

    return sources


def call_gemini(
    prompt,
    use_search=False,
    temperature=0.2,
    max_output_tokens=4096
):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

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

    if use_search:
        payload["tools"] = [
            {
                "google_search": {}
            }
        ]

    response = requests.post(
        GEMINI_API,
        json=payload,
        timeout=90
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    data = response.json()

    return {
        "text": extract_gemini_text(data),
        "sources": extract_grounding_sources(data),
        "raw": data
    }


# ============================================================
# STRUCTURED FACT EXTRACTION
# ============================================================

def extract_json_object(text):
    if not text:
        return None

    cleaned = text.strip()

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

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        candidate = cleaned[start:end + 1]

        try:
            return json.loads(candidate)
        except Exception:
            try:
                return ast.literal_eval(candidate)
            except Exception:
                return None

    return None


def extract_facts_from_user_text(
    chat_id,
    text,
    source_message_id=None
):
    """
    Uses Gemini only to identify explicit factual statements.
    It must not invent facts.
    """

    if not GEMINI_API_KEY:
        return []

    prompt = f"""
{GENIOSA_SYSTEM}

Analyze the following USER MESSAGE.

Extract only explicit factual statements made by the user.

Do NOT infer.
Do NOT calculate.
Do NOT invent.
Do NOT turn questions into facts.

Return ONLY valid JSON in this exact structure:

{{
  "facts": [
    {{
      "key": "short_stable_key",
      "value": "exact factual value",
      "confidence": "explicit"
    }}
  ]
}}

If there are no explicit facts, return:

{{"facts":[]}}

USER MESSAGE:
{text}
"""

    try:
        result = call_gemini(
            prompt,
            use_search=False,
            temperature=0,
            max_output_tokens=1200
        )

        data = extract_json_object(result["text"])

        if not isinstance(data, dict):
            return []

        facts = data.get("facts", [])

        if not isinstance(facts, list):
            return []

        created = []

        for item in facts:
            if not isinstance(item, dict):
                continue

            key = normalize_fact_key(item.get("key"))
            value = str(item.get("value", "")).strip()

            if not key or not value:
                continue

            fact_id = create_fact(
                chat_id=chat_id,
                fact_key=key,
                fact_value=value,
                status="PENDING",
                source="user_message",
                source_message_id=source_message_id
            )

            if fact_id:
                created.append({
                    "id": fact_id,
                    "key": key,
                    "value": value
                })

        return created

    except Exception as exc:
        print("Fact extraction error:", repr(exc))
        return []


# ============================================================
# CONTEXT
# ============================================================

def build_context(chat_id):
    facts = get_facts(chat_id, 100)
    memories = get_memories(chat_id, 50)
    decisions = get_decisions(chat_id, 30)
    tasks = get_tasks(chat_id, 30)
    projects = get_projects(chat_id, 30)
    messages = get_recent_messages(chat_id, 20)

    return {
        "facts": facts,
        "memories": memories,
        "decisions": decisions,
        "tasks": tasks,
        "projects": projects,
        "messages": messages
    }


def context_to_text(context):
    lines = []

    lines.append("=== FACTS ===")

    for fact in context["facts"]:
        lines.append(
            f"[{fact['status']}] "
            f"{fact['fact_key']} = {fact['fact_value']}"
        )

    lines.append("\n=== MEMORIES ===")

    for memory in context["memories"]:
        lines.append(
            f"- {memory['memory']}"
        )

    lines.append("\n=== DECISIONS ===")

    for decision in context["decisions"]:
        lines.append(
            f"- [{decision['status']}] {decision['decision']}"
        )

    lines.append("\n=== TASKS ===")

    for task in context["tasks"]:
        lines.append(
            f"- [{task['status']}] {task['task']}"
        )

    lines.append("\n=== PROJECTS ===")

    for project in context["projects"]:
        lines.append(
            f"- {project['name']}: "
            f"{project.get('description') or ''}"
        )

    lines.append("\n=== RECENT CONVERSATION ===")

    for message in context["messages"]:
        lines.append(
            f"{message['role']}: {message['message']}"
        )

    return "\n".join(lines)


# ============================================================
# WEB RESEARCH
# ============================================================

def should_use_web_search(text):
    lowered = text.lower()

    keywords = [
        "მოიძიე",
        "მოძებნე",
        "მომიძიე",
        "ინტერნეტში",
        "უახლესი",
        "ახლანდელი",
        "დღეს",
        "ამჟამად",
        "ბოლო",
        "ბაზარზე",
        "ფასი",
        "ფასები",
        "კომპანიები",
        "ინვესტორი",
        "ინვესტორები",
        "კანონი",
        "კანონმდებლობა",
        "რეგულაცია",
        "რეგულაციები",
        "საბაზრო",
        "market",
        "price",
        "prices",
        "latest",
        "current",
        "today",
        "research",
        "search",
        "investor",
        "investors",
        "law",
        "regulation",
        "market",
        "companies"
    ]

    return any(word in lowered for word in keywords)


def save_research(chat_id, query, answer, sources):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO research
            (chat_id, query, answer, sources)
            VALUES (%s, %s, %s, %s)
            """,
            (
                chat_id,
                query,
                answer,
                json.dumps(
                    sources or [],
                    ensure_ascii=False
                )
            )
        )

        conn.commit()

    finally:
        cur.close()
        conn.close()


# ============================================================
# FINANCIAL ENGINE
# ============================================================

def safe_float(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(",", "")
    text = text.replace("$", "")
    text = text.replace("€", "")
    text = text.replace("₾", "")

    multiplier = 1

    lowered = text.lower()

    if lowered.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]

    elif lowered.endswith("k"):
        multiplier = 1_000
        text = text[:-1]

    try:
        return float(text) * multiplier
    except Exception:
        return None


def npv(rate, cash_flows):
    total = 0.0

    for period, cash_flow in enumerate(cash_flows):
        total += cash_flow / ((1 + rate) ** period)

    return total


def calculate_irr(cash_flows):
    if not cash_flows or len(cash_flows) < 2:
        return None

    if not any(cf > 0 for cf in cash_flows):
        return None

    if not any(cf < 0 for cf in cash_flows):
        return None

    low = -0.9999
    high = 10.0

    try:
        low_npv = npv(low, cash_flows)
        high_npv = npv(high, cash_flows)

        if low_npv * high_npv > 0:
            return None

        for _ in range(200):
            mid = (low + high) / 2
            mid_npv = npv(mid, cash_flows)

            if abs(mid_npv) < 1e-8:
                return mid

            if low_npv * mid_npv <= 0:
                high = mid
                high_npv = mid_npv
            else:
                low = mid
                low_npv = mid_npv

        return (low + high) / 2

    except Exception:
        return None


def calculate_payback(cash_flows):
    if not cash_flows or len(cash_flows) < 2:
        return None

    cumulative = cash_flows[0]

    if cumulative >= 0:
        return 0.0

    for i in range(1, len(cash_flows)):
        previous = cumulative
        cumulative += cash_flows[i]

        if cumulative >= 0:
            current_cf = cash_flows[i]

            if current_cf == 0:
                return float(i)

            fraction = (-previous) / current_cf

            return (i - 1) + fraction

    return None


def calculate_break_even(
    fixed_costs,
    variable_cost_ratio
):
    fixed = safe_float(fixed_costs)
    ratio = safe_float(variable_cost_ratio)

    if fixed is None or ratio is None:
        return None

    if ratio < 0 or ratio >= 1:
        return None

    return fixed / (1 - ratio)


def calculate_financial_model(model):
    currency = model.get("currency") or "USD"

    land_cost = safe_float(model.get("land_cost")) or 0.0
    construction_cost = (
        safe_float(model.get("construction_cost")) or 0.0
    )
    other_costs = (
        safe_float(model.get("other_costs")) or 0.0
    )

    initial_investment = safe_float(
        model.get("initial_investment")
    )

    if initial_investment is None:
        base_cost = (
            land_cost +
            construction_cost +
            other_costs
        )
    else:
        base_cost = initial_investment

    loan_amount = safe_float(model.get("loan_amount")) or 0.0
    loan_rate = (
        safe_float(model.get("loan_rate_percent")) or 0.0
    )
    loan_years = (
        safe_float(model.get("loan_years")) or 0.0
    )

    loan_interest = 0.0

    if loan_amount > 0 and loan_rate > 0 and loan_years > 0:
        loan_interest = (
            loan_amount *
            (loan_rate / 100.0) *
            loan_years
        )

    total_cost = base_cost + loan_interest

    sales_revenue = (
        safe_float(model.get("sales_revenue")) or 0.0
    )

    rental_revenue = (
        safe_float(model.get("rental_revenue")) or 0.0
    )

    other_revenue = (
        safe_float(model.get("other_revenue")) or 0.0
    )

    total_revenue = (
        sales_revenue +
        rental_revenue +
        other_revenue
    )

    net_profit = total_revenue - total_cost

    equity_investment = total_cost - loan_amount

    roi = None

    if equity_investment > 0:
        roi = (
            net_profit /
            equity_investment
        ) * 100

    investor_share = (
        safe_float(model.get("investor_share_percent"))
    )

    investor_profit = None

    if investor_share is not None:
        investor_profit = (
            net_profit *
            investor_share /
            100
        )

    annual_cash_flows = model.get("annual_cash_flows")

    clean_cash_flows = []

    if isinstance(annual_cash_flows, list):
        for value in annual_cash_flows:
            number = safe_float(value)

            if number is not None:
                clean_cash_flows.append(number)

    irr = None
    payback = None

    if len(clean_cash_flows) >= 2:
        irr = calculate_irr(clean_cash_flows)
        payback = calculate_payback(clean_cash_flows)

    fixed_costs = model.get("fixed_costs")
    variable_cost_ratio = model.get("variable_cost_ratio")

    break_even = calculate_break_even(
        fixed_costs,
        variable_cost_ratio
    )

    profit_margin = None

    if total_revenue > 0:
        profit_margin = (
            net_profit /
            total_revenue
        ) * 100

    return {
        "currency": currency,
        "base_cost": base_cost,
        "loan_interest": loan_interest,
        "total_cost": total_cost,
        "loan_amount": loan_amount,
        "equity_investment": equity_investment,
        "total_revenue": total_revenue,
        "net_profit": net_profit,
        "roi_percent": roi,
        "profit_margin_percent": profit_margin,
        "investor_profit": investor_profit,
        "irr_percent": (
            irr * 100
            if irr is not None
            else None
        ),
        "payback_period_years": payback,
        "break_even_revenue": break_even
    }


def format_money(value, currency="USD"):
    if value is None:
        return "N/A"

    return f"{value:,.2f} {currency}"


def format_financial_result(model, result):
    currency = result["currency"]

    lines = [
        "📊 FINANCIAL ENGINE",
        "",
        f"პროექტი: {model.get('project_name', 'N/A')}",
        "",
        f"საწყისი/ძირითადი ინვესტიცია: "
        f"{format_money(result['base_cost'], currency)}",
        f"სესხი: "
        f"{format_money(result['loan_amount'], currency)}",
        f"სესხის პროცენტი: "
        f"{format_money(result['loan_interest'], currency)}",
        f"სრული ღირებულება: "
        f"{format_money(result['total_cost'], currency)}",
        "",
        f"საერთო შემოსავალი: "
        f"{format_money(result['total_revenue'], currency)}",
        f"წმინდა მოგება: "
        f"{format_money(result['net_profit'], currency)}",
        "",
    ]

    if result["roi_percent"] is not None:
        lines.append(
            f"ROI: {result['roi_percent']:.2f}%"
        )
    else:
        lines.append("ROI: N/A")

    if result["profit_margin_percent"] is not None:
        lines.append(
            f"Profit Margin: "
            f"{result['profit_margin_percent']:.2f}%"
        )

    if result["investor_profit"] is not None:
        lines.append(
            f"ინვესტორის მოგების წილი: "
            f"{format_money(result['investor_profit'], currency)}"
        )

    if result["irr_percent"] is not None:
        lines.append(
            f"IRR: {result['irr_percent']:.2f}%"
        )
    else:
        lines.append(
            "IRR: N/A — საჭიროა პერიოდული Cash Flow."
        )

    if result["payback_period_years"] is not None:
        lines.append(
            f"Payback: "
            f"{result['payback_period_years']:.2f} წელი"
        )
    else:
        lines.append(
            "Payback: N/A — საჭიროა პერიოდული Cash Flow."
        )

    if result["break_even_revenue"] is not None:
        lines.append(
            f"Break-even Revenue: "
            f"{format_money(result['break_even_revenue'], currency)}"
        )

    lines.extend(
        [
            "",
            "⚠️ გამოთვლა ეფუძნება მხოლოდ მოწოდებულ მონაცემებს.",
            "სესხის პროცენტი აქ დათვლილია მარტივი წლიური პროცენტის "
            "მოდელით, თუ სხვა მეთოდი არ არის მითითებული."
        ]
    )

    return "\n".join(lines)


def save_financial_model(chat_id, model):
    project_name = (
        model.get("project_name")
        or "Unnamed Project"
    )

    currency = model.get("currency") or "USD"

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO financial_models
            (
                chat_id,
                project_name,
                currency,
                model_json
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                chat_id,
                project_name,
                currency,
                json.dumps(
                    model,
                    ensure_ascii=False
                )
            )
        )

        model_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO financial_history
            (
                model_id,
                chat_id,
                action,
                model_json
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                model_id,
                chat_id,
                "CREATED",
                json.dumps(
                    model,
                    ensure_ascii=False
                )
            )
        )

        conn.commit()

        return model_id

    finally:
        cur.close()
        conn.close()


def get_financial_models(chat_id, limit=20):
    return db_fetchall(
        """
        SELECT
            id,
            project_name,
            currency,
            model_json,
            created_at,
            updated_at
        FROM financial_models
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


def extract_financial_model_from_text(text):
    prompt = f"""
You are a strict financial data extraction engine.

Extract ONLY numerical/business information explicitly contained
in the user message.

Never invent missing values.

Return ONLY valid JSON.

Schema:

{{
  "project_name": null,
  "currency": "USD",
  "initial_investment": null,
  "land_cost": null,
  "construction_cost": null,
  "other_costs": null,
  "sales_revenue": null,
  "rental_revenue": null,
  "other_revenue": null,
  "loan_amount": null,
  "loan_rate_percent": null,
  "loan_years": null,
  "investor_share_percent": null,
  "annual_cash_flows": [],
  "fixed_costs": null,
  "variable_cost_ratio": null
}}

Rules:
- Missing value = null.
- Never estimate.
- Never infer.
- Preserve the user's numbers.
- Percentages must be numeric without % sign.
- Cash flows must be a JSON array of numbers.
- If a value is unclear, use null.

USER MESSAGE:
{text}
"""

    try:
        result = call_gemini(
            prompt,
            use_search=False,
            temperature=0,
            max_output_tokens=1500
        )

        data = extract_json_object(result["text"])

        if not isinstance(data, dict):
            return None

        return data

    except Exception as exc:
        print("Financial extraction error:", repr(exc))
        return None


def financial_engine_from_text(chat_id, text):
    model = extract_financial_model_from_text(text)

    if not model:
        return (
            "ფინანსური მოდელის ამოსაღებად მონაცემები ვერ წავიკითხე.\n\n"
            "მომეცი მინიმუმ:\n"
            "• ინვესტიცია/ღირებულება\n"
            "• შემოსავალი\n"
            "ან ხარჯები + შემოსავლის მონაცემები."
        )

    model["project_name"] = (
        model.get("project_name")
        or "Unnamed Project"
    )

    result = calculate_financial_model(model)

    save_financial_model(chat_id, model)

    return format_financial_result(model, result)


def should_use_financial_engine(text):
    lowered = text.lower()

    keywords = [
        "roi",
        "irr",
        "payback",
        "cash flow",
        "cashflow",
        "break even",
        "break-even",
        "მოგება",
        "შემოსავალი",
        "ხარჯი",
        "ხარჯები",
        "ინვესტიცია",
        "ინვესტორი",
        "სესხი",
        "პროცენტი",
        "რენტაბელობა",
        "ფინანსური მოდელი",
        "ფინანსურად",
        "profit",
        "revenue",
        "cost",
        "investment",
        "loan",
        "financial model"
    ]

    return any(word in lowered for word in keywords)


# ============================================================
# DEVELOPER ENGINE
# ============================================================

def extract_code(text):
    blocks = re.findall(
        r"```(?:python|py)?\s*(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    if blocks:
        return blocks[0].strip()

    return None


def check_python_syntax(code):
    try:
        ast.parse(code)
        return {
            "ok": True,
            "error": None
        }
    except SyntaxError as exc:
        return {
            "ok": False,
            "error": (
                f"{exc.msg} "
                f"(line {exc.lineno}, column {exc.offset})"
            )
        }


def detect_common_python_problems(code):
    problems = []

    if "\t" in code and "    " in code:
        problems.append(
            "Mixed tabs and spaces may cause indentation problems."
        )

    if re.search(
        r"(^|\n)\s*def\s+\w+\([^)]*\):\s*\n\s*$",
        code
    ):
        problems.append(
            "A function definition appears to have no body."
        )

    if "openai" in code.lower() and "OPENAI_API_KEY" not in code:
        problems.append(
            "OpenAI code may require OPENAI_API_KEY."
        )

    if "psycopg2" in code and "psycopg2-binary" not in code:
        problems.append(
            "psycopg2 may require psycopg2-binary in Render requirements."
        )

    return problems


def analyze_python_code(code):
    syntax = check_python_syntax(code)

    problems = detect_common_python_problems(code)

    return {
        "syntax": syntax,
        "problems": problems
    }


def developer_engine(chat_id, text):
    code = extract_code(text)

    if code:
        analysis = analyze_python_code(code)

        if analysis["syntax"]["ok"]:
            status = "✅ Python syntax: OK"
        else:
            status = (
                "❌ Python syntax error:\n"
                + analysis["syntax"]["error"]
            )

        if analysis["problems"]:
            extra = "\n\n⚠️ შესაძლო პრობლემები:\n- " + (
                "\n- ".join(analysis["problems"])
            )
        else:
            extra = "\n\n✅ აშკარა სტატიკური პრობლემა ვერ ვიპოვე."

        return (
            status +
            extra +
            "\n\nთუ გინდა სრული გამოსწორება, გამომიგზავნე "
            "მთელი მიმდინარე ფაილი."
        )

    prompt = f"""
{GENIOSA_SYSTEM}

You are now operating as Geniosa Developer Engine.

Analyze the user's technical request.

Return:
1. Root cause or explanation.
2. Exact solution.
3. Complete usable code when code is requested.
4. Required dependencies.
5. Required environment variables.
6. Testing steps.
7. Risks or limitations.

USER REQUEST:
{text}
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.15,
        max_output_tokens=6000
    )

    return result["text"]


# ============================================================
# DEBUG ENGINE
# ============================================================

def debug_engine(chat_id, text):
    code = extract_code(text)

    if code:
        analysis = analyze_python_code(code)

        prompt = f"""
{GENIOSA_SYSTEM}

You are Geniosa Debug Engine.

A user supplied Python code.

STATIC ANALYSIS:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

CODE:

{code}

Give:
- exact root cause
- whether syntax/runtime/dependency/config/logic issue
- corrected complete code if possible
- requirements
- Render configuration
- test steps

Do not use nested markdown fences around the final corrected code.
"""

    else:
        prompt = f"""
{GENIOSA_SYSTEM}

You are Geniosa Debug Engine.

Analyze this problem:

{text}

Identify:
- root cause
- error type
- exact fix
- configuration/dependencies
- test procedure

Do not invent logs that were not provided.
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.1,
        max_output_tokens=7000
    )

    return result["text"]


# ============================================================
# CODE PROJECTS
# ============================================================

def create_code_project(chat_id, name, description=""):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO code_projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (chat_id, name, description)
        )

        project_id = cur.fetchone()[0]

        conn.commit()

        return project_id

    finally:
        cur.close()
        conn.close()


def save_code_version(
    chat_id,
    project_id,
    version,
    code,
    notes=""
):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO code_versions
            (
                project_id,
                chat_id,
                version,
                code,
                notes
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                project_id,
                chat_id,
                version,
                code,
                notes
            )
        )

        version_id = cur.fetchone()[0]

        conn.commit()

        return version_id

    finally:
        cur.close()
        conn.close()


def get_code_versions(chat_id, limit=50):
    return db_fetchall(
        """
        SELECT
            id,
            project_id,
            version,
            notes,
            created_at
        FROM code_versions
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )


# ============================================================
# BOT FACTORY
# ============================================================

def create_bot_project(chat_id, name, description=""):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO bot_projects
            (chat_id, name, description)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (chat_id, name, description)
        )

        bot_id = cur.fetchone()[0]

        conn.commit()

        return bot_id

    finally:
        cur.close()
        conn.close()


def save_bot_file(bot_project_id, filename, content):
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO bot_files
            (
                bot_project_id,
                filename,
                content
            )
            VALUES (%s, %s, %s)
            """,
            (
                bot_project_id,
                filename,
                content
            )
        )

        conn.commit()

    finally:
        cur.close()
        conn.close()


def get_bot_projects(chat_id):
    return db_fetchall(
        """
        SELECT *
        FROM bot_projects
        WHERE chat_id = %s
        ORDER BY id DESC
        """,
        (chat_id,)
    )


def get_bot_files(bot_project_id):
    return db_fetchall(
        """
        SELECT id, filename, content, created_at
        FROM bot_files
        WHERE bot_project_id = %s
        ORDER BY id DESC
        """,
        (bot_project_id,)
    )


def bot_factory_create(chat_id, text):
    prompt = f"""
{GENIOSA_SYSTEM}

You are Geniosa Bot Factory.

Create a practical Telegram bot project based on this request:

{text}

Return JSON only:

{{
  "name": "bot name",
  "description": "description",
  "files": [
    {{
      "filename": "main.py",
      "content": "complete file content"
    }},
    {{
      "filename": "requirements.txt",
      "content": "dependencies"
    }},
    {{
      "filename": ".env.example",
      "content": "environment variables"
    }},
    {{
      "filename": "README.md",
      "content": "instructions"
    }}
  ]
}}

Never include secrets.
"""

    result = call_gemini(
        prompt,
        use_search=False,
        temperature=0.15,
        max_output_tokens=10000
    )

    data = extract_json_object(result["text"])

    if not isinstance(data, dict):
        return "Bot Factory-მ ვერ შექმნა სტრუქტურირებული პროექტი."

    name = data.get("name") or "New Bot"
    description = data.get("description") or ""

    bot_id = create_bot_project(
        chat_id,
        name,
        description
    )

    files = data.get("files", [])

    for file in files:
        if not isinstance(file, dict):
            continue

        filename = file.get("filename")
        content = file.get("content")

        if filename and content:
            save_bot_file(
                bot_id,
                filename,
                content
            )

    return (
        f"🤖 Bot Factory\n\n"
        f"შეიქმნა პროექტი: {name}\n"
        f"ID: {bot_id}\n"
        f"ფაილები: {len(files)}\n\n"
        f"პროექტი შენახულია PostgreSQL-ში."
    )


# ============================================================
# ANSWER GENERATION
# ============================================================

def generate_answer(chat_id, user_text):
    context = build_context(chat_id)
    context_text = context_to_text(context)

    if should_use_financial_engine(user_text):
        try:
            financial_answer = financial_engine_from_text(
                chat_id,
                user_text
            )

            return financial_answer

        except Exception as exc:
            print("Financial engine failed:", repr(exc))

    use_search = should_use_web_search(user_text)

    prompt = f"""
{GENIOSA_SYSTEM}

CURRENT USER CONTEXT:

{context_text}

USER'S NEW MESSAGE:

{user_text}

Answer the user directly.

Important:
- Do not invent facts.
- Treat PENDING facts as unconfirmed.
- Treat CONFLICT facts as conflicting.
- Confirmed facts must remain protected.
- If current information is needed, use web research.
- If the user asks for calculations, show assumptions.
"""

    result = call_gemini(
        prompt,
        use_search=use_search,
        temperature=0.2,
        max_output_tokens=5000
    )

    answer = result["text"]

    if not answer:
        answer = (
            "პასუხის გენერირება ვერ მოხერხდა. "
            "გთხოვ, თავიდან გამომიგზავნე კითხვა."
        )

    sources = result.get("sources") or []

    if use_search:
        save_research(
            chat_id,
            user_text,
            answer,
            sources
        )

    return answer


# ============================================================
# TELEGRAM
# ============================================================

def telegram_call(method, payload=None, timeout=60):
    if not TELEGRAM_API:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
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

    return data


def send_message(chat_id, text):
    if text is None:
        text = ""

    text = str(text).strip()

    if not text:
        return

    max_length = 3900

    chunks = [
        text[i:i + max_length]
        for i in range(0, len(text), max_length)
    ]

    for chunk in chunks:
        telegram_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk
            },
            timeout=30
        )


def delete_webhook():
    if not TELEGRAM_API:
        return

    try:
        result = telegram_call(
            "deleteWebhook",
            {
                "drop_pending_updates": False
            },
            timeout=30
        )

        print("deleteWebhook:", result)

    except Exception as exc:
        print("deleteWebhook error:", repr(exc))


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def help_text():
    return """
🤖 GENIOSA 4.5

პირადი ბიზნეს-მრჩეველი / ეკონომისტი / დეველოპერი / ასისტენტი

ძირითადი:

/start
/help
/memory
/facts
/fact_history
/projects
/decisions
/tasks

ფინანსები:

/finance
/financial_models

კოდი:

/code
/code_history
/debug

Bot Factory:

/bots
/bot_create
/bot_code
/bot_update
/bot_history

Geniosa ასევე ამუშავებს ბუნებრივ ენაზე:

• ბიზნეს ანალიზს
• ინვესტიციებს
• ფინანსურ მოდელებს
• ROI / IRR / Payback
• Web Research-ს
• პროექტებს
• კოდის შექმნას და Debug-ს
• ფაქტების მეხსიერებას
"""


def command_memory(chat_id):
    memories = get_memories(chat_id, 50)

    if not memories:
        return "მეხსიერებაში ჩანაწერები ჯერ არ არის."

    lines = ["🧠 MEMORY", ""]

    for item in memories:
        lines.append(
            f"• {item['memory']}"
        )

    return "\n".join(lines)


def command_facts(chat_id):
    facts = get_facts(chat_id, 100)

    if not facts:
        return "📌 Facts-ში ჩანაწერები ჯერ არ არის."

    lines = ["📌 FACT ENGINE", ""]

    for fact in facts:
        lines.append(
            f"#{fact['id']} "
            f"[{fact['status']}] "
            f"{fact['fact_key']} = {fact['fact_value']}"
        )

    return "\n".join(lines)


def command_fact_history(chat_id):
    history = get_fact_history(chat_id, 100)

    if not history:
        return "Fact History ჯერ ცარიელია."

    lines = ["📜 FACT HISTORY", ""]

    for item in history:
        lines.append(
            f"#{item['id']} "
            f"{item['action']} "
            f"| {item.get('details') or ''}"
        )

    return "\n".join(lines)


def command_projects(chat_id):
    projects = get_projects(chat_id)

    if not projects:
        return "პროექტები ჯერ არ არის."

    lines = ["📁 PROJECTS", ""]

    for project in projects:
        lines.append(
            f"#{project['id']} "
            f"{project['name']} "
            f"[{project['status']}]"
        )

    return "\n".join(lines)


def command_decisions(chat_id):
    decisions = get_decisions(chat_id)

    if not decisions:
        return "გადაწყვეტილებები ჯერ არ არის."

    lines = ["🧭 DECISIONS", ""]

    for item in decisions:
        lines.append(
            f"• [{item['status']}] {item['decision']}"
        )

    return "\n".join(lines)


def command_tasks(chat_id):
    tasks = get_tasks(chat_id)

    if not tasks:
        return "Tasks ჯერ არ არის."

    lines = ["📋 TASKS", ""]

    for item in tasks:
        lines.append(
            f"• [{item['status']}] {item['task']}"
        )

    return "\n".join(lines)


def command_financial_models(chat_id):
    models = get_financial_models(chat_id)

    if not models:
        return "📊 ფინანსური მოდელები ჯერ არ არის."

    lines = ["📊 FINANCIAL MODELS", ""]

    for model in models:
        lines.append(
            f"#{model['id']} "
            f"{model['project_name']} "
            f"({model['currency']})"
        )

    return "\n".join(lines)


def command_code_history(chat_id):
    versions = get_code_versions(chat_id)

    if not versions:
        return "Code History ჯერ ცარიელია."

    lines = ["💻 CODE HISTORY", ""]

    for item in versions:
        lines.append(
            f"#{item['id']} "
            f"project={item['project_id']} "
            f"version={item['version']}"
        )

    return "\n".join(lines)


def command_bots(chat_id):
    bots = get_bot_projects(chat_id)

    if not bots:
        return "🤖 Bot Factory-ში პროექტები ჯერ არ არის."

    lines = ["🤖 BOT PROJECTS", ""]

    for bot in bots:
        lines.append(
            f"#{bot['id']} "
            f"{bot['name']} "
            f"[{bot['status']}]"
        )

    return "\n".join(lines)


def parse_debug_command(text):
    content = text[len("/debug"):].strip()

    if not content:
        return None

    code = extract_code(content)

    if code:
        return code

    return content


def handle_command(chat_id, text):
    command_line = text.strip()

    command = command_line.split()[0].lower()

    if command == "/start":
        return (
            "გამარჯობა 👋\n\n"
            "მე ვარ Geniosa 4.5 — შენი პირადი ბიზნეს-მრჩეველი, "
            "ეკონომისტი, დეველოპერი და ასისტენტი.\n\n"
            "გამოიყენე /help სრული ფუნქციებისთვის."
        )

    if command == "/help":
        return help_text()

    if command == "/memory":
        return command_memory(chat_id)

    if command == "/facts":
        return command_facts(chat_id)

    if command == "/fact_history":
        return command_fact_history(chat_id)

    if command == "/projects":
        return command_projects(chat_id)

    if command == "/decisions":
        return command_decisions(chat_id)

    if command == "/tasks":
        return command_tasks(chat_id)

    if command == "/financial_models":
        return command_financial_models(chat_id)

    if command == "/code_history":
        return command_code_history(chat_id)

    if command == "/bots":
        return command_bots(chat_id)

    if command == "/finance":
        content = command_line[len("/finance"):].strip()

        if not content:
            return (
                "📊 Financial Engine\n\n"
                "მომწერე ბუნებრივი ენით ფინანსური მონაცემები.\n\n"
                "მაგალითი:\n"
                "ინვესტიცია 10 მილიონი დოლარი, "
                "შემოსავალი 15 მილიონი, "
                "ინვესტორის წილი 80%, "
                "სესხი 5 მილიონი 12%-ით 3 წლით.\n\n"
                "Geniosa გამოთვლის Profit, ROI და სხვა მაჩვენებლებს."
            )

        return financial_engine_from_text(
            chat_id,
            content
        )

    if command == "/code":
        content = command_line[len("/code"):].strip()

        if not content:
            return "მომწერე რა კოდი ან პროგრამა გჭირდება."

        return developer_engine(
            chat_id,
            content
        )

    if command == "/debug":
        content = parse_debug_command(text)

        if not content:
            return (
                "გამოიყენე:\n"
                "/debug შენი პრობლემა\n\n"
                "ან ჩასვი კოდი ```python ... ``` ფორმატში."
            )

        return debug_engine(
            chat_id,
            content
        )

    if command == "/bot_create":
        content = command_line[len("/bot_create"):].strip()

        if not content:
            return (
                "მომწერე როგორი Telegram bot გინდა."
            )

        return bot_factory_create(
            chat_id,
            content
        )

    if command == "/bot_code":
        parts = command_line.split(maxsplit=1)

        if len(parts) < 2:
            return "გამოიყენე /bot_code BOT_ID"

        try:
            bot_id = int(parts[1])
        except Exception:
            return "BOT_ID უნდა იყოს რიცხვი."

        files = get_bot_files(bot_id)

        if not files:
            return "ამ Bot პროექტში ფაილები ვერ მოიძებნა."

        output = ["🤖 BOT CODE", ""]

        for file in files:
            output.append(
                f"===== {file['filename']} ====="
            )
            output.append(
                file["content"]
            )
            output.append("")

        return "\n".join(output)

    return None


# ============================================================
# TELEGRAM UPDATE
# ============================================================

def process_update(update):
    try:
        message = update.get("message")

        if not message:
            return

        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if not chat_id:
            return

        text = message.get("text")

        if not text:
            return

        text = str(text).strip()

        if not text:
            return

        message_id = save_message(
            chat_id,
            "user",
            text
        )

        # Store automatic memory signals.
        try:
            capture_memory(
                chat_id,
                text
            )
        except Exception as exc:
            print("capture_memory error:", repr(exc))

        # Store structured facts.
        try:
            extract_facts_from_user_text(
                chat_id,
                text,
                source_message_id=message_id
            )
        except Exception as exc:
            print("fact extraction error:", repr(exc))

        # Commands.
        if text.startswith("/"):
            try:
                response = handle_command(
                    chat_id,
                    text
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

            except Exception as exc:
                error_text = (
                    "Command error:\n"
                    f"{type(exc).__name__}: {exc}"
                )

                print(error_text)

                send_message(
                    chat_id,
                    "ტექნიკური შეცდომა:\n" + error_text
                )

                return

        # Normal AI response.
        try:
            response = generate_answer(
                chat_id,
                text
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

        except Exception as exc:
            error_text = (
                f"{type(exc).__name__}: {exc}"
            )

            print(
                "process_update AI error:",
                repr(exc)
            )

            fallback = (
                "Geniosa-ს პასუხის გენერირებისას "
                "ტექნიკური შეცდომა მოხდა.\n\n"
                f"{error_text}"
            )

            save_message(
                chat_id,
                "assistant",
                fallback
            )

            send_message(
                chat_id,
                fallback
            )

    except Exception as exc:
        print(
            "process_update fatal error:",
            repr(exc)
        )


# ============================================================
# POSTGRESQL DISTRIBUTED TELEGRAM LOCK
# ============================================================

polling_connection = None
polling_started = False
polling_thread = None
polling_thread_lock = threading.Lock()


def get_polling_lock_key():
    if TELEGRAM_BOT_TOKEN:
        digest = hashlib.sha256(
            TELEGRAM_BOT_TOKEN.encode("utf-8")
        ).digest()

        return int.from_bytes(
            digest[:8],
            byteorder="big",
            signed=False
        ) & 0x7FFFFFFFFFFFFFFF

    return 4455667788


def acquire_polling_lock():
    global polling_connection

    try:
        conn = get_db()

        cur = conn.cursor()

        lock_key = get_polling_lock_key()

        cur.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (lock_key,)
        )

        locked = cur.fetchone()[0]

        cur.close()

        if not locked:
            conn.close()

            print(
                "Telegram polling lock is already held "
                "by another Geniosa instance."
            )

            return False

        polling_connection = conn

        print(
            "Telegram PostgreSQL polling lock acquired."
        )

        return True

    except Exception as exc:
        print(
            "Could not acquire Telegram polling lock:",
            repr(exc)
        )

        return False


def release_polling_lock():
    global polling_connection

    if not polling_connection:
        return

    try:
        cur = polling_connection.cursor()

        cur.execute(
            "SELECT pg_advisory_unlock(%s)",
            (get_polling_lock_key(),)
        )

        polling_connection.commit()
        cur.close()

    except Exception as exc:
        print(
            "Polling lock release error:",
            repr(exc)
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
            "TELEGRAM_BOT_TOKEN is missing."
        )
        return

    if not acquire_polling_lock():
        print(
            "Telegram polling will NOT start in this instance."
        )
        return

    delete_webhook()

    offset = None

    polling_started = True

    print(
        "Telegram polling started."
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

                # 409 means another polling process is active.
                # Do not hammer Telegram continuously.
                if result.get("error_code") == 409:
                    time.sleep(15)
                else:
                    time.sleep(5)

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:
                update_id = update.get("update_id")

                if update_id is not None:
                    offset = update_id + 1

                try:
                    process_update(update)
                except Exception as exc:
                    print(
                        "Update processing error:",
                        repr(exc)
                    )

        except Exception as exc:
            print(
                "Telegram polling exception:",
                repr(exc)
            )

            time.sleep(5)


def start_polling_once():
    global polling_thread

    with polling_thread_lock:

        if polling_thread is not None:
            if polling_thread.is_alive():
                return

        polling_thread = threading.Thread(
            target=telegram_polling,
            daemon=True,
            name="geniosa-telegram-polling"
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
        "status": "online"
    }


@app.get("/health")
def health():
    database_ok = False

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT 1"
        )

        cur.fetchone()

        cur.close()
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
# STARTUP / SHUTDOWN
# ============================================================

@app.on_event("startup")
def startup_event():
    print(
        f"Starting Geniosa {APP_VERSION}"
    )

    try:
        init_db()

        print(
            "Database initialized successfully."
        )

    except Exception as exc:
        print(
            "Database initialization error:",
            repr(exc)
        )

        raise

    if not GEMINI_API_KEY:
        print(
            "WARNING: GEMINI_API_KEY is missing."
        )

    if not TELEGRAM_BOT_TOKEN:
        print(
            "WARNING: TELEGRAM_BOT_TOKEN is missing."
        )
    else:
        start_polling_once()


@app.on_event("shutdown")
def shutdown_event():
    global polling_started

    polling_started = False

    release_polling_lock()

    print(
        "Geniosa shutdown completed."
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
