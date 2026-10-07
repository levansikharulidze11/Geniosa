# ============================================================
# GENIOSA 4.2
# Personal Business Advisor
# Fact Engine + Developer Engine + Debug/Fix Engine
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


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="Geniosa",
    version="4.2"
)


# ============================================================
# CONFIGURATION STATUS
# ============================================================

def configuration_status():
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")

    if not DATABASE_URL:
        missing.append("DATABASE_URL")

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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT DEFAULT 'USER_CONFIRMED',
            confidence TEXT DEFAULT 'confirmed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, fact_key)
        )
        """
    )

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

def save_message(chat_id, role, text):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO messages
            (chat_id, role, text)
        VALUES
            (%s, %s, %s)
        """,
        (
            chat_id,
            role,
            text
        )
    )

    conn.commit()

    cur.close()
    conn.close()


def get_recent_messages(chat_id, limit=20):
    conn = get_db()
    cur = conn.cursor(
        cursor_factory=RealDictCursor
    )

    cur.execute(
        """
        SELECT role, text, created_at
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
# FACT ENGINE
# ============================================================

def save_fact(
    chat_id,
    fact_key,
    fact_value,
    source="USER_CONFIRMED",
    confidence="confirmed"
):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO facts
            (
                chat_id,
                fact_key,
                fact_value,
                source,
                confidence
            )
        VALUES
            (%s, %s, %s, %s, %s)
        ON CONFLICT (chat_id, fact_key)
        DO UPDATE SET
            fact_value = EXCLUDED.fact_value,
            source = EXCLUDED.source,
            confidence = EXCLUDED.confidence,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            chat_id,
            fact_key,
            fact_value,
            source,
            confidence
        )
    )

    conn.commit()

    cur.close()
    conn.close()


def get_facts(chat_id):
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
            created_at,
            updated_at
        FROM facts
        WHERE chat_id = %s
        ORDER BY id
        """,
        (chat_id,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def build_fact_context(chat_id):
    facts = get_facts(chat_id)

    if not facts:
        return (
            "FACT DATABASE: "
            "No confirmed facts stored yet."
        )

    lines = [
        "FACT DATABASE:",
        "Only use confirmed facts as facts."
    ]

    for fact in facts:
        lines.append(
            "- "
            + str(fact["fact_key"])
            + " = "
            + str(fact["fact_value"])
            + " | source="
            + str(fact["source"])
            + " | confidence="
            + str(fact["confidence"])
        )

    return "\n".join(lines)


def extract_explicit_facts(
    chat_id,
    user_text
):
    text = user_text.strip()
    lower = text.lower()

    detected = []

    if (
        "samtisi construction" in lower
        or "samtisi" in lower
        or "სამთისი" in lower
        or "სამტისი" in lower
    ):
        save_fact(
            chat_id,
            "company_name",
            "SAMTISI CONSTRUCTION LLC",
            "USER_CONFIRMED",
            "confirmed"
        )

        detected.append(
            "company_name"
        )

    if (
        "nikkea 12" in lower
        or "ნიკეა 12" in lower
    ):
        save_fact(
            chat_id,
            "nikkea_12_land_area",
            "3,070 m²",
            "USER_CONFIRMED",
            "confirmed"
        )

        save_fact(
            chat_id,
            "nikkea_12_total_saleable_area",
            "10,854 m²",
            "USER_CONFIRMED",
            "confirmed"
        )

        save_fact(
            chat_id,
            "nikkea_12_hotel_rooms_area",
            "7,212 m²",
            "USER_CONFIRMED",
            "confirmed"
        )

        detected.extend(
            [
                "nikkea_12_land_area",
                "nikkea_12_total_saleable_area",
                "nikkea_12_hotel_rooms_area"
            ]
        )

    if (
        "samgori" in lower
        or "სამგორი" in lower
    ):
        save_fact(
            chat_id,
            "samgori_land_area",
            "7,390 m²",
            "USER_CONFIRMED",
            "confirmed"
        )

        save_fact(
            chat_id,
            "samgori_build_area",
            "46,131 m²",
            "USER_CONFIRMED",
            "confirmed"
        )

        save_fact(
            chat_id,
            "samgori_saleable_area",
            "30,540 m²",
            "USER_CONFIRMED",
            "confirmed"
        )

        detected.extend(
            [
                "samgori_land_area",
                "samgori_build_area",
                "samgori_saleable_area"
            ]
        )

    if (
        "golden lake" in lower
        or "oqri lake" in lower
        or "ოქროს ტბა" in lower
    ):
        save_fact(
            chat_id,
            "golden_lake_main_land",
            "approximately 46 hectares",
            "USER_CONFIRMED",
            "confirmed"
        )

        detected.append(
            "golden_lake_main_land"
        )

    return detected


# ============================================================
# LEGACY MEMORY DETECTION
# ============================================================

def detect_memories(
    chat_id,
    user_text
):
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
                "Land 3,070 m². "
                "Total saleable area 10,854 m². "
                "Hotel rooms area 7,212 m²."
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
                "Land 7,390 m². "
                "Build area 46,131 m². "
                "Saleable area 30,540 m²."
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
                "Main land approximately 46 ha."
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

    current = row[0] if row else 0

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
                parts.append(current)

            current = paragraph

        else:
            if current:
                current += "\n"

            current += paragraph

    if current:
        parts.append(current)

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
    if not os.path.exists(filepath):
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
            text = part.get("text")

            if text:
                answer_parts.append(text)

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
    )


# ============================================================
# CONTEXT
# ============================================================

def build_memory_context(chat_id):
    memories = get_memories(chat_id)

    if not memories:
        return (
            "Legacy memory: empty."
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
    projects = get_projects(chat_id)

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
                project.get("description")
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
            row.get("role", "")
        )

        text = str(
            row.get("text", "")
        )

        if len(text) > 1200:
            text = text[:1200] + "..."

        lines.append(
            role
            + ": "
            + text
        )

    return "\n".join(lines)


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
        ast.parse(code)

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


def detect_common_python_problems(code):
    problems = []

    if "inplace-True" in code:
        problems.append(
            {
                "type": "runtime/API",
                "message": (
                    "აღმოჩნდა 'inplace-True'. "
                    "სავარაუდოდ უნდა იყოს 'inplace=True'."
                )
            }
        )

    if "inplace-False" in code:
        problems.append(
            {
                "type": "runtime/API",
                "message": (
                    "აღმოჩნდა 'inplace-False'. "
                    "სავარაუდოდ უნდა იყოს 'inplace=False'."
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
                        "საჭირო იყოს openpyxl."
                    )
                }
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
        safe_name = "geniosa_code.py"

    if not safe_name.endswith(".py"):
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
        file.write(code)

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
        "as facts.\n"
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
        "- runtime/API error\n"
        "- dependency error\n"
        "- logic error\n"
        "- configuration error\n"
        "\n"
        "If a code correction is requested, explain "
        "what was wrong and provide the corrected "
        "complete version.\n"
    )

    parts.append(
        build_fact_context(chat_id)
    )

    parts.append(
        build_memory_context(chat_id)
    )

    parts.append(
        build_project_context(chat_id)
    )

    parts.append(
        build_history_context(chat_id)
    )

    parts.append(
        "USER TASK:\n"
        + task
    )

    return "\n\n".join(parts)


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

    filename = "geniosa_generated.py"

    lowered = task.lower()

    if "app.py" in lowered:
        filename = "app.py"

    elif "requirements" in lowered:
        filename = "requirements.txt"

    elif "telegram" in lowered:
        filename = "telegram_bot.py"

    save_code_project(
        chat_id,
        project_name,
        "python",
        task[:1000]
    )

    if code:
        saved = save_code_version(
            chat_id,
            project_name,
            filename,
            "python",
            code,
            analysis["syntax_ok"],
            analysis["syntax_error"],
            "initial generation"
        )
    else:
        saved = None

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
            after["syntax_ok"],
            after["syntax_error"],
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
        build_fact_context(chat_id)
    )

    parts.append(
        build_memory_context(chat_id)
    )

    parts.append(
        build_project_context(chat_id)
    )

    parts.append(
        build_history_context(chat_id)
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
        "\n\n".join(parts)
    )


# ============================================================
# COMMANDS
# ============================================================

def command_start():
    return (
        "გამარჯობა. მე ვარ Geniosa 4.2. 🤖\n\n"
        "მე შემიძლია დაგეხმარო:\n"
        "• ბიზნესის ანალიზში\n"
        "• საინვესტიციო პროექტებში\n"
        "• ფინანსურ მოდელებში\n"
        "• პროექტების მართვაში\n"
        "• კოდის წერაში\n"
        "• debugging-ში\n"
        "• კოდის ვერსიების მართვაში\n"
        "• ფაქტების უსაფრთხო მეხსიერებაში\n\n"
        "Developer:\n"
        "/developer\n\n"
        "კოდის შექმნა:\n"
        "/code <დავალება>\n\n"
        "კოდის debugging:\n"
        "/debug <დავალება>\n\n"
        "კოდის ისტორია:\n"
        "/code_history\n\n"
        "ფაქტები:\n"
        "/facts\n\n"
        "მეხსიერება:\n"
        "/memory\n\n"
        "პროექტები:\n"
        "/projects\n\n"
        "/help"
    )


def command_help():
    return (
        "GENIOSA 4.2\n\n"
        "/start — დაწყება\n"
        "/help — დახმარება\n"
        "/facts — დადასტურებული ფაქტები\n"
        "/memory — მეხსიერება\n"
        "/projects — პროექტები\n"
        "/developer — Developer რეჟიმი\n"
        "/code <task> — კოდის შექმნა\n"
        "/debug <task> — კოდის debugging\n"
        "/code_history — კოდის ვერსიები"
    )


def command_facts(chat_id):
    facts = get_facts(chat_id)

    if not facts:
        return (
            "დადასტურებული ფაქტები ჯერ არ არის."
        )

    lines = [
        "დადასტურებული ფაქტები:"
    ]

    for fact in facts:
        lines.append(
            "\n"
            + str(fact["fact_key"])
            + "\n"
            + str(fact["fact_value"])
            + "\nsource: "
            + str(fact["source"])
        )

    return "\n".join(lines)


def command_memory(chat_id):
    memories = get_memories(chat_id)

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
            + str(item["memory_key"])
            + "\n"
            + str(item["memory_value"])
        )

    return "\n".join(lines)


def command_projects(chat_id):
    projects = get_projects(chat_id)

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
            + str(project["name"])
            + "\n"
            + str(
                project.get("description")
                or ""
            )
        )

    return "\n".join(lines)


def command_code_history(chat_id):
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
            + str(row["version_number"])
            + " | "
            + str(row["project_name"])
            + " | "
            + str(row["filename"])
            + " | "
            + status
            + "\n"
            + "Change: "
            + str(
                row.get("change_reason")
                or ""
            )
            + "\n"
            + str(row["created_at"])
        )

    return "\n".join(lines)


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

    try:
        save_message(
            chat_id,
            "user",
            user_text
        )
    except Exception as exc:
        print(
            "save_message user error:",
            repr(exc)
        )

    try:
        detect_memories(
            chat_id,
            user_text
        )

        extract_explicit_facts(
            chat_id,
            user_text
        )

    except Exception as exc:
        print(
            "fact/memory detection error:",
            repr(exc)
        )

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if user_text.startswith("/start"):
        answer = command_start()

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    elif user_text.startswith("/help"):
        answer = command_help()

    # --------------------------------------------------------
    # FACTS
    # --------------------------------------------------------

    elif user_text.startswith("/facts"):
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
    # MEMORY
    # --------------------------------------------------------

    elif user_text.startswith("/memory"):
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

    elif user_text.startswith("/projects"):
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

    elif user_text.startswith("/developer"):
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
                    "და კოდი.\n\n"
                    "მაგალითად:\n"
                    "/debug გაასწორე ეს კოდი..."
                )

            else:
                send_message(
                    chat_id,
                    "Debug Engine მუშაობს... 🔍"
                )

                try:
                    result = developer_debug(
                        chat_id,
                        task,
                        extract_code(task)
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
                            "Geniosa 4.2 Debug Engine\n"
                            "Fixed version\n"
                            "Python syntax: "
                            + (
                                "OK"
                                if after["syntax_ok"]
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
                                + str(len(common))
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

                time.sleep(10)
                continue

            if response.status_code != 200:
                print(
                    "getUpdates error:",
                    response.status_code,
                    response.text[:1000]
                )

                time.sleep(5)
                continue

            data = response.json()

            if not data.get("ok"):
                print(
                    "getUpdates not ok:",
                    data
                )

                time.sleep(5)
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
                        update_id + 1
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

            time.sleep(5)


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
def root():
    return {
        "service": "Geniosa",
        "version": "4.2",
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
        "version": "4.2",
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

@app.on_event("startup")
def startup_event():
    print(
        "=" * 60
    )

    print(
        "GENIOSA 4.2 STARTING"
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
        "Geniosa 4.2 startup complete."
    )


# ============================================================
# END
# ============================================================
