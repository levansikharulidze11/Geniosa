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
# Personal Business Advisor + Assistant
# Telegram + Gemini + PostgreSQL + Web Search
# Memory + Facts + Projects + Tasks + Decisions
# Developer Engine + Debug Engine + Bot Factory
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
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
)

GEMINI_API = (
    f"https://generativelanguage.googleapis.com/"
    f"v1beta/models/{GEMINI_MODEL}:generateContent"
    f"?key={GEMINI_API_KEY}"
)


app = FastAPI(
    title="Geniosa",
    version=APP_VERSION
)


# ============================================================
# BASIC UTILITIES
# ============================================================

def now_text():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def safe_json(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2
        )
    except Exception:
        return str(value)


# ============================================================
# DATABASE CONNECTION
# ============================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing."
        )

    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            category TEXT,
            content TEXT NOT NULL,
            source TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory_events (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            event_type TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            decision TEXT NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
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
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS fact_history (
            id SERIAL PRIMARY KEY,
            fact_id INTEGER,
            old_value TEXT,
            new_value TEXT,
            action TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS code_projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS code_versions (
            id SERIAL PRIMARY KEY,
            project_id INTEGER,
            version INTEGER,
            filename TEXT,
            code TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_projects (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_files (
            id SERIAL PRIMARY KEY,
            bot_project_id INTEGER,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS research (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            query TEXT NOT NULL,
            answer TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    cur.close()
    conn.close()

    print("Database initialized successfully.")


# ============================================================
# MESSAGE MEMORY
# ============================================================

def save_message(chat_id, role, message):
    message = clean_text(message)

    if not message:
        return

    conn = None

    try:
        conn = db_connect()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO messages
            (chat_id, role, message)
            VALUES (%s, %s, %s)
            """,
            (
                int(chat_id),
                role,
                message
            )
        )

        conn.commit()
        cur.close()

    except Exception as e:
        print("save_message error:", repr(e))

    finally:
        if conn:
            conn.close()


def get_recent_messages(chat_id, limit=30):
    conn = db_connect()
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
            int(chat_id),
            int(limit)
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    rows.reverse()

    return rows


def search_messages(chat_id, query, limit=20):
    conn = db_connect()
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
            int(chat_id),
            f"%{query}%",
            int(limit)
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ============================================================
# GENERAL MEMORY
# ============================================================

def save_memory(
    chat_id,
    content,
    category="general",
    source="user",
    status="PENDING"
):
    content = clean_text(content)

    if not content:
        return

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO memories
        (chat_id, category, content, source, status)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            int(chat_id),
            category,
            content,
            source,
            status
        )
    )

    conn.commit()
    cur.close()
    conn.close()


def save_memory_event(
    chat_id,
    event_type,
    content
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO memory_events
        (chat_id, event_type, content)
        VALUES (%s, %s, %s)
        """,
        (
            int(chat_id),
            event_type,
            content
        )
    )

    conn.commit()
    cur.close()
    conn.close()


def get_memories(
    chat_id,
    status=None,
    limit=100
):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if status:
        cur.execute(
            """
            SELECT *
            FROM memories
            WHERE chat_id = %s
            AND status = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                int(chat_id),
                status,
                int(limit)
            )
        )
    else:
        cur.execute(
            """
            SELECT *
            FROM memories
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                int(chat_id),
                int(limit)
            )
        )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def search_memories(
    chat_id,
    query,
    limit=50
):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM memories
        WHERE chat_id = %s
        AND content ILIKE %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (
            int(chat_id),
            f"%{query}%",
            int(limit)
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def get_memory_events(
    chat_id,
    limit=50
):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM memory_events
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (
            int(chat_id),
            int(limit)
        )
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
    description=""
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO projects
        (chat_id, name, description)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (
            int(chat_id),
            name,
            description
        )
    )

    project_id = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()

    return project_id


def get_projects(chat_id):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM projects
        WHERE chat_id = %s
        ORDER BY id DESC
        """,
        (int(chat_id),)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ============================================================
# DECISIONS
# ============================================================

def save_decision(
    chat_id,
    decision
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO decisions
        (chat_id, decision)
        VALUES (%s, %s)
        """,
        (
            int(chat_id),
            decision
        )
    )

    conn.commit()
    cur.close()
    conn.close()


def get_decisions(chat_id):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM decisions
        WHERE chat_id = %s
        ORDER BY id DESC
        """,
        (int(chat_id),)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ============================================================
# TASKS
# ============================================================

def save_task(
    chat_id,
    task
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO tasks
        (chat_id, task)
        VALUES (%s, %s)
        """,
        (
            int(chat_id),
            task
        )
    )

    conn.commit()
    cur.close()
    conn.close()


def get_tasks(chat_id):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM tasks
        WHERE chat_id = %s
        ORDER BY id DESC
        """,
        (int(chat_id),)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


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
        r"[^a-zA-Z0-9_\u10D0-\u10FF]",
        "",
        key
    )

    return key[:200]


def get_fact(
    chat_id,
    fact_key
):
    conn = db_connect()
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
            int(chat_id),
            normalize_fact_key(fact_key)
        )
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row


def create_fact(
    chat_id,
    fact_key,
    value,
    source="user",
    status="PENDING"
):
    fact_key = normalize_fact_key(fact_key)
    value = clean_text(value)

    existing = get_fact(
        chat_id,
        fact_key
    )

    if existing:
        if (
            existing["status"] == "CONFIRMED"
            and existing["value"] != value
        ):
            conn = db_connect()
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
            conn.close()

            return "CONFLICT"

        return "EXISTS"

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO facts
        (chat_id, fact_key, value, source, status)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            int(chat_id),
            fact_key,
            value,
            source,
            status
        )
    )

    conn.commit()
    cur.close()
    conn.close()

    return "CREATED"


def get_facts(
    chat_id,
    status=None,
    limit=100
):
    conn = db_connect()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if status:
        cur.execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            AND status = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                int(chat_id),
                status,
                int(limit)
            )
        )
    else:
        cur.execute(
            """
            SELECT *
            FROM facts
            WHERE chat_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                int(chat_id),
                int(limit)
            )
        )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def confirm_fact(
    chat_id,
    fact_id
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE facts
        SET status = 'CONFIRMED',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        AND chat_id = %s
        """,
        (
            int(fact_id),
            int(chat_id)
        )
    )

    changed = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    return changed > 0


def reject_fact(
    chat_id,
    fact_id
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE facts
        SET status = 'REJECTED',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        AND chat_id = %s
        """,
        (
            int(fact_id),
            int(chat_id)
        )
    )

    changed = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    return changed > 0


# ============================================================
# AUTOMATIC MEMORY CAPTURE
# ============================================================

def should_save_memory(text):
    text = clean_text(text).lower()

    triggers = [
        "დაიმახსოვრე",
        "გახსოვდეს",
        "დამიმახსოვრე",
        "შეინახე",
        "მომავალში",
        "ამიერიდან",
        "remember",
        "memorize",
        "save this",
        "from now on",
        "keep this"
    ]

    return any(
        trigger in text
        for trigger in triggers
    )


def should_save_decision(text):
    text = clean_text(text).lower()

    triggers = [
        "გადავწყვიტეთ",
        "გადავწყვიტე",
        "ვწყვეტთ",
        "ვაკეთებთ",
        "decision",
        "we decided",
        "i decided"
    ]

    return any(
        trigger in text
        for trigger in triggers
    )


def should_save_task(text):
    text = clean_text(text).lower()

    triggers = [
        "უნდა გავაკეთოთ",
        "უნდა გავაკეთო",
        "შემდეგი ნაბიჯი",
        "დავალება",
        "task",
        "todo",
        "next step"
    ]

    return any(
        trigger in text
        for trigger in triggers
    )


def capture_memory(chat_id, text):
    if should_save_memory(text):
        save_memory(
            chat_id,
            text,
            category="user_memory",
            source="user",
            status="PENDING"
        )

        save_memory_event(
            chat_id,
            "MEMORY_CREATED",
            text
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


# ============================================================
# TELEGRAM
# ============================================================

def telegram_call(
    method,
    payload=None,
    timeout=60
):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/{method}"
    )

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
            "status_code": response.status_code,
            "text": response.text
        }


def send_message(
    chat_id,
    text
):
    text = clean_text(text)

    if not text:
        text = "ცარიელი პასუხი მივიღე."

    max_length = 4000

    parts = []

    while len(text) > max_length:
        parts.append(
            text[:max_length]
        )
        text = text[max_length:]

    parts.append(text)

    results = []

    for part in parts:
        result = telegram_call(
            "sendMessage",
            {
                "chat_id": int(chat_id),
                "text": part
            }
        )

        results.append(result)

    return results


# ============================================================
# GEMINI
# ============================================================

def call_gemini(
    prompt,
    use_search=False,
    system_instruction=None
):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing."
        )

    contents = []

    if system_instruction:
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "text": system_instruction
                    }
                ]
            }
        )

    contents.append(
        {
            "role": "user",
            "parts": [
                {
                    "text": prompt
                }
            ]
        }
    )

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192
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
        timeout=120
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini API error "
            f"{response.status_code}: "
            f"{response.text[:2000]}"
        )

    data = response.json()

    candidates = data.get(
        "candidates",
        []
    )

    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidates."
        )

    candidate = candidates[0]

    parts = (
        candidate
        .get("content", {})
        .get("parts", [])
    )

    answer_parts = []

    for part in parts:
        if "text" in part:
            answer_parts.append(
                part["text"]
            )

    answer = "\n".join(
        answer_parts
    ).strip()

    if not answer:
        raise RuntimeError(
            "Gemini returned an empty answer."
        )

    return {
        "text": answer,
        "raw": data
    }


# ============================================================
# WEB SEARCH
# ============================================================

def should_use_web_search(text):
    text = clean_text(text).lower()

    keywords = [
        "ინტერნეტში",
        "მოიძიე",
        "მოძებნე",
        "მომიძიე",
        "მოიძიე ინტერნეტში",
        "დღეს",
        "ახლა",
        "ამჟამად",
        "უახლესი",
        "ბოლო",
        "ფასი",
        "ფასები",
        "ბაზარი",
        "კომპანია",
        "ინვესტორი",
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
        "company",
        "law",
        "regulation"
    ]

    return any(
        keyword in text
        for keyword in keywords
    )


def extract_grounding_sources(data):
    sources = []

    try:
        metadata = (
            data
            .get("candidates", [{}])[0]
            .get("groundingMetadata", {})
        )

        chunks = metadata.get(
            "groundingChunks",
            []
        )

        for chunk in chunks:
            web = chunk.get(
                "web",
                {}
            )

            uri = web.get("uri")
            title = web.get("title")

            if uri:
                sources.append(
                    {
                        "title": title or uri,
                        "uri": uri
                    }
                )

    except Exception as e:
        print(
            "extract_grounding_sources error:",
            repr(e)
        )

    unique = []
    seen = set()

    for source in sources:
        uri = source.get("uri")

        if uri and uri not in seen:
            seen.add(uri)
            unique.append(source)

    return unique


def save_research(
    chat_id,
    query,
    answer,
    sources
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO research
        (chat_id, query, answer, sources)
        VALUES (%s, %s, %s, %s)
        """,
        (
            int(chat_id),
            query,
            answer,
            json.dumps(
                sources,
                ensure_ascii=False
            )
        )
    )

    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# PERSONAL CONTEXT
# ============================================================

def build_memory_context(chat_id):
    parts = []

    facts = get_facts(
        chat_id,
        status="CONFIRMED",
        limit=100
    )

    if facts:
        parts.append(
            "CONFIRMED FACTS:\n"
            + safe_json(facts)
        )

    memories = get_memories(
        chat_id,
        status="CONFIRMED",
        limit=50
    )

    if memories:
        parts.append(
            "CONFIRMED MEMORIES:\n"
            + safe_json(memories)
        )

    projects = get_projects(
        chat_id
    )

    if projects:
        parts.append(
            "PROJECTS:\n"
            + safe_json(projects)
        )

    decisions = get_decisions(
        chat_id
    )

    if decisions:
        parts.append(
            "DECISIONS:\n"
            + safe_json(decisions)
        )

    tasks = get_tasks(
        chat_id
    )

    if tasks:
        parts.append(
            "TASKS:\n"
            + safe_json(tasks)
        )

    return "\n\n".join(parts)


def build_recent_context(chat_id):
    rows = get_recent_messages(
        chat_id,
        limit=30
    )

    if not rows:
        return ""

    lines = []

    for row in rows:
        role = row.get(
            "role",
            "unknown"
        )

        message = row.get(
            "message",
            ""
        )

        lines.append(
            f"{role}: {message}"
        )

    return "\n".join(lines)


# ============================================================
# GENIOSA CONSTITUTION
# ============================================================

GENIOSA_SYSTEM = """
You are Geniosa.

You are the user's personal business advisor, economist,
developer, researcher and digital assistant.

You are NOT limited to any single company, project or industry.

The user may work on:
- construction
- real estate
- development
- investment
- finance
- technology
- companies
- projects
- personal business tasks
- software
- Telegram bots
- research
- documents
- negotiations
- strategy

CORE CONSTITUTION:

1. Never invent facts.
2. Never present assumptions as confirmed facts.
3. If you do not know something, clearly say that you do not know.
4. User-confirmed facts must not be silently replaced.
5. If two important values conflict, identify the conflict.
6. Financial calculations must clearly identify assumptions.
7. Important decisions should be preserved.
8. Distinguish facts, assumptions, estimates and recommendations.
9. When internet research is used, distinguish researched information from stored user facts.
10. When sources are available, mention them.
11. For coding tasks, distinguish syntax, runtime, dependency,
configuration and logic problems.
12. Produce actually usable code.
13. Never expose API keys, passwords or secrets.
14. Never automatically deploy production code.
15. Never claim that code has been deployed unless it actually has.
16. Never claim that an external action was performed unless it actually was.
17. Preserve working functionality when improving code.
18. Ask for clarification when a critical ambiguity can materially change the result.
19. User information stored as PENDING must not be presented as CONFIRMED.
20. CONFIRMED facts must remain CONFIRMED unless the user explicitly changes them.
21. When new information conflicts with a confirmed fact, flag the conflict.
22. Be practical, concise and decision-oriented.
23. For important business decisions, explain the reasoning and risks.
"""


# ============================================================
# GENERAL AI ANSWER
# ============================================================

def generate_answer(
    chat_id,
    user_text,
    force_search=False
):
    memory_context = build_memory_context(
        chat_id
    )

    recent_context = build_recent_context(
        chat_id
    )

    use_search = (
        force_search
        or should_use_web_search(user_text)
    )

    prompt = f"""
USER REQUEST:
{user_text}

STORED USER CONTEXT:
{memory_context}

RECENT CONVERSATION:
{recent_context}

IMPORTANT:
Use stored context only according to its status.
Do not treat PENDING information as confirmed.

Answer the user directly.

If calculations are needed, show the assumptions.

If current information is required and web search is available,
use it.

If something is unknown, say that it is unknown.
"""

    try:
        result = call_gemini(
            prompt,
            use_search=use_search,
            system_instruction=GENIOSA_SYSTEM
        )

    except Exception as first_error:
        print(
            "Gemini primary error:",
            repr(first_error)
        )

        if use_search:
            try:
                result = call_gemini(
                    prompt,
                    use_search=False,
                    system_instruction=GENIOSA_SYSTEM
                )
            except Exception as second_error:
                return (
                    "Geniosa-ს AI სერვისთან დაკავშირებისას "
                    "შეცდომა მოხდა.\n\n"
                    f"{second_error}"
                )
        else:
            return (
                "Geniosa-ს AI სერვისთან დაკავშირებისას "
                "შეცდომა მოხდა.\n\n"
                f"{first_error}"
            )

    answer = result["text"]

    sources = extract_grounding_sources(
        result["raw"]
    )

    if use_search:
        try:
            save_research(
                chat_id,
                user_text,
                answer,
                sources
            )
        except Exception as e:
            print(
                "save_research error:",
                repr(e)
            )

    if sources:
        answer += "\n\nწყაროები:\n"

        for source in sources[:8]:
            title = source.get(
                "title",
                "Source"
            )

            uri = source.get(
                "uri",
                ""
            )

            answer += (
                f"- {title}: {uri}\n"
            )

    return answer


# ============================================================
# DEVELOPER ENGINE
# ============================================================

def extract_code(text):
    matches = re.findall(
        r"```(?:python|py)?\s*(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )

    if matches:
        return matches[0].strip()

    return text.strip()


def check_python_syntax(code):
    try:
        ast.parse(code)

        return {
            "valid": True,
            "error": None
        }

    except SyntaxError as e:
        return {
            "valid": False,
            "error": {
                "type": "SyntaxError",
                "message": str(e),
                "line": e.lineno,
                "offset": e.offset
            }
        }

    except Exception as e:
        return {
            "valid": False,
            "error": {
                "type": type(e).__name__,
                "message": str(e)
            }
        }


def detect_common_python_problems(code):
    problems = []

    if "openai" in code:
        problems.append(
            "Code references OpenAI. "
            "Verify whether this dependency is intentionally required."
        )

    if "psycopg2" in code:
        problems.append(
            "Code requires psycopg2/psycopg2-binary."
        )

    if "os.getenv" in code:
        problems.append(
            "Code depends on environment variables."
        )

    if "requests." in code:
        problems.append(
            "Code requires the requests package."
        )

    if "uvicorn" in code:
        problems.append(
            "Code requires uvicorn."
        )

    if "FastAPI" in code:
        problems.append(
            "Code requires FastAPI."
        )

    return problems


def analyze_python_code(code):
    syntax = check_python_syntax(
        code
    )

    common = detect_common_python_problems(
        code
    )

    return {
        "syntax": syntax,
        "common_problems": common,
        "code_length": len(code)
    }


def save_code_project(
    chat_id,
    name,
    description=""
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO code_projects
        (chat_id, name, description)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (
            int(chat_id),
            name,
            description
        )
    )

    project_id = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()

    return project_id


def save_code_version(
    project_id,
    filename,
    code,
    description=""
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COALESCE(MAX(version), 0)
        FROM code_versions
        WHERE project_id = %s
        """,
        (int(project_id),)
    )

    last_version = cur.fetchone()[0]
    next_version = last_version + 1

    cur.execute(
        """
        INSERT INTO code_versions
        (project_id, version, filename, code, description)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            int(project_id),
            next_version,
            filename,
            code,
            description
        )
    )

    conn.commit()
    cur.close()
    conn.close()

    return next_version


def get_code_history(chat_id):
    conn = db_connect()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT
            cp.name,
            cv.version,
            cv.filename,
            cv.description,
            cv.created_at
        FROM code_projects cp
        JOIN code_versions cv
        ON cv.project_id = cp.id
        WHERE cp.chat_id = %s
        ORDER BY cv.created_at DESC
        LIMIT 100
        """,
        (int(chat_id),)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def developer_engine(
    chat_id,
    task
):
    prompt = f"""
You are Geniosa Developer Engine.

USER'S REQUEST:
{task}

Rules:
- Produce complete usable code when code is requested.
- Do not invent unavailable credentials.
- Use environment variables for secrets.
- Explain important dependencies.
- Do not claim that code was deployed.
- Preserve working functionality.
- If information is missing, clearly identify it.
"""

    try:
        result = call_gemini(
            prompt,
            use_search=False,
            system_instruction=GENIOSA_SYSTEM
        )

        answer = result["text"]

    except Exception as e:
        return (
            "Developer Engine შეცდომა:\n"
            + str(e)
        )

    code = extract_code(
        answer
    )

    analysis = analyze_python_code(
        code
    )

    try:
        project_id = save_code_project(
            chat_id,
            "Geniosa Developer Project",
            task[:500]
        )

        save_code_version(
            project_id,
            "main.py",
            code,
            task[:500]
        )

    except Exception as e:
        print(
            "Developer project save error:",
            repr(e)
        )

    return (
        answer
        + "\n\n"
        + "CODE ANALYSIS:\n"
        + safe_json(analysis)
    )


# ============================================================
# DEBUG ENGINE
# ============================================================

def debug_engine(
    chat_id,
    task,
    code
):
    before = analyze_python_code(
        code
    )

    prompt = f"""
You are Geniosa Debug Engine.

USER'S DEBUG REQUEST:
{task}

CURRENT CODE:
```python
{code}
