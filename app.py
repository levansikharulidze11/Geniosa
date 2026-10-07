# ============================================================
# GENIOSA 4.1
# Personal Business Advisor + Developer Engine
# Telegram + Gemini + PostgreSQL + Code Generation
# ============================================================

import os
import re
import ast
import time
import json
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
    version="4.1"
)


# ============================================================
# BASIC VALIDATION
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
# DATABASE
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10
    )


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
# DATABASE — MESSAGES
# ============================================================

def save_message(chat_id, role, text):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO messages (chat_id, role, text)
        VALUES (%s, %s, %s)
        """,
        (chat_id, role, text)
    )

    conn.commit()
    cur.close()
    conn.close()


def get_recent_messages(chat_id, limit=20):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT role, text, created_at
        FROM messages
        WHERE chat_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (chat_id, limit)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    rows.reverse()

    return rows


# ============================================================
# DATABASE — MEMORY
# ============================================================

def save_memory(chat_id, memory_key, memory_value):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO memories
            (chat_id, memory_key, memory_value)
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
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT memory_key, memory_value
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
# DATABASE — PROJECTS
# ============================================================

def save_project(chat_id, name, description):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO projects
            (chat_id, name, description)
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
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """
        SELECT name, description, created_at, updated_at
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
# DATABASE — CODE PROJECTS
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
            (chat_id, name, language, description)
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


def save_code_version(
    chat_id,
    project_name,
    filename,
    language,
    code,
    syntax_ok,
    syntax_error=""
):
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
                syntax_ok,
                syntax_error
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            chat_id,
            project_name,
            filename,
            language,
            code,
            syntax_ok,
            syntax_error
        )
    )

    conn.commit()
    cur.close()
    conn.close()


def get_code_versions(chat_id, project_name=None, limit=10):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if project_name:
        cur.execute(
            """
            SELECT
                id,
                project_name,
                filename,
                language,
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


def telegram_request(method, payload=None, files=None, timeout=60):
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
                "TELEGRAM 409 CONFLICT: another polling process "
                "may be using this bot token."
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
        print("Telegram request error:", repr(exc))
        return None


def delete_webhook():
    result = telegram_request(
        "deleteWebhook",
        {
            "drop_pending_updates": False
        },
        timeout=30
    )

    print("Telegram deleteWebhook:", result)


def send_message(chat_id, text):
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
        if len(current) + len(paragraph) + 1 > max_length:
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
        with open(filepath, "rb") as document:
            result = telegram_request(
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

        return result

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
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL
        + ":generateContent?key="
        + GEMINI_API_KEY
    )


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return (
            "Gemini API key არ არის დაყენებული. "
            "Render → Environment-ში შეამოწმე GEMINI_API_KEY."
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
                "AI მოდელთან დაკავშირებისას მოხდა შეცდომა.\n\n"
                "HTTP status: "
                + str(response.status_code)
                + "\n\n"
                + response.text[:1000]
            )

        data = response.json()

        candidates = data.get("candidates", [])

        if not candidates:
            return "Gemini-მ პასუხი ვერ დააბრუნა."

        content = candidates[0].get("content", {})

        parts = content.get("parts", [])

        answer_parts = []

        for part in parts:
            text = part.get("text")

            if text:
                answer_parts.append(text)

        answer = "\n".join(answer_parts).strip()

        if not answer:
            return "Gemini-მ ცარიელი პასუხი დააბრუნა."

        return answer

    except Exception as exc:
        print(
            "Gemini request exception:",
            repr(exc)
        )

        return (
            "Gemini-სთან დაკავშირებისას ტექნიკური შეცდომა მოხდა.\n"
            + str(exc)
        )


# ============================================================
# GENIOSA CONSTITUTION
# ============================================================

def constitution():
    return (
        "GENIOSA CONSTITUTION\n"
        "=====================\n"
        "\n"
        "Geniosa არის მომხმარებლის პირადი ბიზნეს მრჩეველი, "
        "ეკონომისტი, ანალიტიკოსი და ტექნიკური ასისტენტი.\n"
        "\n"
        "ძირითადი წესები:\n"
        "1. არ მოიგონო ფაქტები.\n"
        "2. თუ ინფორმაცია არ გაქვს, პირდაპირ თქვი, რომ არ იცი.\n"
        "3. გაარჩიე ფაქტი, ვარაუდი და ფინანსური პროგნოზი.\n"
        "4. ფინანსურ გათვლებში აჩვენე ძირითადი დაშვებები.\n"
        "5. დიდი გადაწყვეტილებისას მიუთითე რისკები.\n"
        "6. მომხმარებლის პროექტების კონტექსტი გამოიყენე მხოლოდ "
        "როდესაც ის შესაბამისია.\n"
        "7. არ წაშალო ან შეცვალო მნიშვნელოვანი ინფორმაცია "
        "მომხმარებლის დადასტურების გარეშე.\n"
        "8. კოდის წერისას პრიორიტეტია სწორი, გაშვებადი და "
        "სტრუქტურირებული კოდი.\n"
        "9. კოდის გენერირებისას არ წარმოადგინო დაუმოწმებელი კოდი "
        "როგორც გარანტირებულად გამართულად.\n"
        "10. production სისტემის თვითნებური შეცვლა დაუშვებელია.\n"
        "11. მომხმარებლის თანხმობის გარეშე არ უნდა მოხდეს "
        "production deployment.\n"
        "12. უსაფრთხოების, ფინანსური ან სამართლებრივი რისკი "
        "უნდა აღინიშნოს მკაფიოდ.\n"
    )


# ============================================================
# MEMORY CONTEXT
# ============================================================

def build_memory_context(chat_id):
    memories = get_memories(chat_id)

    if not memories:
        return "მუდმივი მეხსიერება ამ ჩატისთვის ჯერ არ არსებობს."

    lines = [
        "მომხმარებლის შენახული მეხსიერება:"
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
        return "შენახული პროექტები არ არის."

    lines = [
        "შენახული პროექტები:"
    ]

    for project in projects:
        description = project.get("description") or ""

        lines.append(
            "- "
            + str(project["name"])
            + ": "
            + description
        )

    return "\n".join(lines)


def build_history_context(chat_id):
    rows = get_recent_messages(
        chat_id,
        limit=12
    )

    if not rows:
        return "წინა საუბრის ისტორია არ არსებობს."

    lines = [
        "ბოლო საუბრის ისტორია:"
    ]

    for row in rows:
        role = str(row.get("role", ""))
        text = str(row.get("text", ""))

        if len(text) > 1500:
            text = text[:1500] + "..."

        lines.append(
            role
            + ": "
            + text
        )

    return "\n".join(lines)


# ============================================================
# AUTO MEMORY DETECTION
# ============================================================

def detect_memories(chat_id, user_text):
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

    if "nikkea 12" in text or "ნიკეა 12" in text:
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

    if "samgori" in text or "სამგორი" in text:
        save_memory(
            chat_id,
            "project_samgori",
            (
                "Tbilisi, Samgori, Giorgi Naderishvili Street. "
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
                "Golden Lake / Oqri Lake development concept. "
                "Main land approximately 46 ha."
            )
        )


# ============================================================
# PYTHON CODE UTILITIES
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


def create_code_file(code, filename):
    safe_name = os.path.basename(filename)

    if not safe_name:
        safe_name = "geniosa_code.py"

    if not safe_name.endswith(".py"):
        safe_name += ".py"

    filepath = os.path.join(
        "/tmp",
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

def developer_access_allowed(chat_id):
    if not OWNER_ID:
        return True

    return str(chat_id) == str(OWNER_ID)


# ============================================================
# DEVELOPER PROMPT
# ============================================================

def build_developer_prompt(
    chat_id,
    task
):
    memory = build_memory_context(chat_id)
    projects = build_project_context(chat_id)
    history = build_history_context(chat_id)

    prompt_parts = []

    prompt_parts.append(constitution())

    prompt_parts.append(
        "\nDEVELOPER MODE\n"
        "==============\n"
        "შენ ახლა მუშაობ როგორც პროგრამისტი და software architect.\n"
        "შეგიძლია დაწერო Python კოდი, გაასწორო კოდი, "
        "გადააწყო კოდი, გააანალიზო შეცდომა და შექმნა ახალი "
        "პროგრამული მოდული.\n"
        "\n"
        "მნიშვნელოვანი წესი:\n"
        "მომხმარებელს უნდა მისცე რეალურად გამოსაყენებელი კოდი.\n"
        "არ დაწერო მხოლოდ ფსევდოკოდი, თუ მომხმარებელი სრულ კოდს ითხოვს.\n"
        "თუ რაიმე დეტალი უცნობია, მკაფიოდ მიუთითე.\n"
        "\n"
        "Python კოდის მოთხოვნის შემთხვევაში:\n"
        "- კოდი ჩასვი ```python ... ``` ბლოკში.\n"
        "- არ ჩასვა არასაჭირო ტექსტი კოდის ბლოკში.\n"
        "- შეეცადე კოდი იყოს დამოუკიდებლად გაშვებადი.\n"
        "- გაითვალისწინე შეცდომების დამუშავება.\n"
        "- არ გამოიყენო საიდუმლო API keys პირდაპირ კოდში.\n"
        "- production ცვლილება მომხმარებლის დადასტურების გარეშე არ გააკეთო.\n"
    )

    prompt_parts.append(
        "\nCURRENT USER MEMORY\n"
        + memory
    )

    prompt_parts.append(
        "\nCURRENT PROJECTS\n"
        + projects
    )

    prompt_parts.append(
        "\nRECENT CONVERSATION\n"
        + history
    )

    prompt_parts.append(
        "\nUSER DEVELOPER TASK\n"
        + task
    )

    prompt_parts.append(
        "\nპასუხი ჩამოაყალიბე პროფესიონალურად. "
        "თუ კოდს წერ, სრული კოდი მიეცი."
    )

    return "\n\n".join(prompt_parts)


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
        task
    )

    answer = call_gemini(prompt)

    code = extract_code(answer)

    syntax = check_python_syntax(code)

    filename = "geniosa_generated.py"

    lowered = task.lower()

    if "app.py" in lowered:
        filename = "app.py"
    elif "requirements" in lowered:
        filename = "requirements.txt"
    elif "telegram" in lowered:
        filename = "telegram_bot.py"

    if code and (
        "```" in answer
        or "def " in code
        or "import " in code
        or "from " in code
        or "class " in code
    ):
        save_code_project(
            chat_id,
            project_name,
            "python",
            task[:1000]
        )

        save_code_version(
            chat_id,
            project_name,
            filename,
            "python",
            code,
            syntax["ok"],
            syntax["error"]
        )

    return {
        "answer": answer,
        "code": code,
        "filename": filename,
        "syntax_ok": syntax["ok"],
        "syntax_error": syntax["error"]
    }


# ============================================================
# GENERAL AI RESPONSE
# ============================================================

def generate_general_response(
    chat_id,
    user_text
):
    prompt_parts = []

    prompt_parts.append(
        constitution()
    )

    prompt_parts.append(
        "\nUSER MEMORY\n"
        + build_memory_context(chat_id)
    )

    prompt_parts.append(
        "\nPROJECTS\n"
        + build_project_context(chat_id)
    )

    prompt_parts.append(
        "\nRECENT HISTORY\n"
        + build_history_context(chat_id)
    )

    prompt_parts.append(
        "\nUSER MESSAGE\n"
        + user_text
    )

    prompt_parts.append(
        "\nუპასუხე მომხმარებელს ქართულად, "
        "თუ მომხმარებელი სხვა ენაზე არ წერს."
    )

    prompt_parts.append(
        "იყავი კონკრეტული, ფაქტობრივი და პრაქტიკული."
    )

    prompt = "\n\n".join(prompt_parts)

    return call_gemini(prompt)


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def command_start(chat_id):
    return (
        "გამარჯობა. მე ვარ Geniosa 4.1.\n\n"
        "მე შემიძლია დაგეხმარო:\n"
        "• ბიზნესის ანალიზში\n"
        "• საინვესტიციო პროექტებში\n"
        "• ფინანსურ მოდელებში\n"
        "• პროექტების მართვაში\n"
        "• კოდის წერაში და debugging-ში\n"
        "• Python პროგრამების შექმნაში\n"
        "• კოდის ვერსიების შენახვაში\n\n"
        "Developer რეჟიმი:\n"
        "/developer\n\n"
        "კოდის შექმნა:\n"
        "/code შენი დავალება\n\n"
        "კოდის ისტორია:\n"
        "/code_history\n\n"
        "მეხსიერება:\n"
        "/memory\n\n"
        "პროექტები:\n"
        "/projects\n\n"
        "დახმარება:\n"
        "/help"
    )


def command_help():
    return (
        "GENIOSA 4.1 COMMANDS\n\n"
        "/start — დაწყება\n"
        "/help — დახმარება\n"
        "/memory — შენახული მეხსიერება\n"
        "/projects — პროექტები\n"
        "/developer — Developer რეჟიმი\n"
        "/code <task> — კოდის შექმნა\n"
        "/code_history — კოდის ვერსიები\n\n"
        "მაგალითი:\n"
        "/code შექმენი Python Telegram bot, "
        "რომელიც PostgreSQL-ში ინახავს შეტყობინებებს."
    )


def command_memory(chat_id):
    memories = get_memories(chat_id)

    if not memories:
        return "მეხსიერება ჯერ ცარიელია."

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
        return "პროექტები ჯერ არ არის შენახული."

    lines = [
        "შენახული პროექტები:"
    ]

    for project in projects:
        lines.append(
            "\n"
            + str(project["name"])
            + "\n"
            + str(project.get("description") or "")
        )

    return "\n".join(lines)


def command_code_history(chat_id):
    rows = get_code_versions(
        chat_id,
        limit=10
    )

    if not rows:
        return "კოდის ვერსიები ჯერ არ არის."

    lines = [
        "ბოლო კოდის ვერსიები:"
    ]

    for row in rows:
        status = (
            "OK"
            if row["syntax_ok"]
            else "ERROR"
        )

        created = str(
            row["created_at"]
        )

        lines.append(
            "\n#"
            + str(row["id"])
            + " | "
            + str(row["project_name"])
            + " | "
            + str(row["filename"])
            + " | "
            + status
            + "\n"
            + created
        )

    return "\n".join(lines)


# ============================================================
# UPDATE PROCESSING
# ============================================================

def process_update(update):
    if not update:
        return

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat")

    if not chat:
        return

    chat_id = chat.get("id")

    user_text = message.get("text")

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
    except Exception as exc:
        print(
            "memory detection error:",
            repr(exc)
        )

    if user_text.startswith("/start"):
        answer = command_start(chat_id)

    elif user_text.startswith("/help"):
        answer = command_help()

    elif user_text.startswith("/memory"):
        try:
            answer = command_memory(chat_id)
        except Exception as exc:
            answer = (
                "მეხსიერების წაკითხვა ვერ მოხერხდა: "
                + str(exc)
            )

    elif user_text.startswith("/projects"):
        try:
            answer = command_projects(chat_id)
        except Exception as exc:
            answer = (
                "პროექტების წაკითხვა ვერ მოხერხდა: "
                + str(exc)
            )

    elif user_text.startswith("/developer"):
        if not developer_access_allowed(chat_id):
            answer = (
                "Developer რეჟიმზე წვდომა ამ ჩატისთვის "
                "დაშვებული არ არის."
            )
        else:
            answer = (
                "Developer რეჟიმი ჩართულია.\n\n"
                "გამოიყენე:\n"
                "/code <დავალება>\n\n"
                "მაგალითად:\n"
                "/code შექმენი Python პროგრამა, "
                "რომელიც CSV ფაილს კითხულობს და ანალიზს აკეთებს."
            )

    elif user_text.startswith("/code_history"):
        if not developer_access_allowed(chat_id):
            answer = (
                "Developer რეჟიმზე წვდომა ამ ჩატისთვის "
                "დაშვებული არ არის."
            )
        else:
            try:
                answer = command_code_history(
                    chat_id
                )
            except Exception as exc:
                answer = (
                    "კოდის ისტორიის წაკითხვა ვერ მოხერხდა: "
                    + str(exc)
                )

    elif user_text.startswith("/code"):
        if not developer_access_allowed(chat_id):
            answer = (
                "Developer რეჟიმზე წვდომა ამ ჩატისთვის "
                "დაშვებული არ არის."
            )
        else:
            task = user_text[
                len("/code"):
            ].strip()

            if not task:
                answer = (
                    "მომწერე კოდის დავალება.\n\n"
                    "მაგალითად:\n"
                    "/code შექმენი Python Telegram bot."
                )
            else:
                send_message(
                    chat_id,
                    "ვმუშაობ კოდზე... ⏳"
                )

                try:
                    result = developer_generate(
                        chat_id,
                        task
                    )

                    answer = result["answer"]

                    code = result["code"]

                    if code:
                        syntax_ok = result["syntax_ok"]

                        if syntax_ok:
                            caption = (
                                "Geniosa Developer Engine\n"
                                "Python syntax: OK"
                            )
                        else:
                            caption = (
                                "Geniosa Developer Engine\n"
                                "Python syntax: ERROR\n"
                                + result["syntax_error"]
                            )

                        try:
                            filepath = create_code_file(
                                code,
                                result["filename"]
                            )

                            send_document_to_chat(
                                chat_id,
                                filepath,
                                caption
                            )

                        except Exception as exc:
                            print(
                                "code file error:",
                                repr(exc)
                            )

                            answer += (
                                "\n\nფაილის შექმნისას მოხდა "
                                "ტექნიკური შეცდომა: "
                                + str(exc)
                            )

                        if syntax_ok:
                            answer += (
                                "\n\n✅ Python syntax შემოწმებულია."
                            )
                        else:
                            answer += (
                                "\n\n⚠️ Python syntax-ში "
                                "შეცდომა აღმოჩნდა:\n"
                                + result["syntax_error"]
                            )

                except Exception as exc:
                    answer = (
                        "Developer Engine-ში მოხდა შეცდომა:\n"
                        + str(exc)
                    )

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
                "პასუხის გენერირებისას მოხდა ტექნიკური "
                "შეცდომა:\n"
                + str(exc)
            )

    try:
        send_message(
            chat_id,
            answer
        )
    except Exception as exc:
        print(
            "send_message final error:",
            repr(exc)
        )

    try:
        save_message(
            chat_id,
            "assistant",
            answer
        )
    except Exception as exc:
        print(
            "save_message assistant error:",
            repr(exc)
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():
    print("Telegram polling thread started.")

    offset = None

    while True:
        try:
            params = {
                "timeout": 30
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                telegram_url("getUpdates"),
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
                    "getUpdates returned not ok:",
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
                    offset = update_id + 1

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
# FASTAPI ROUTES
# ============================================================

@app.get("/")
def root():
    return {
        "service": "Geniosa",
        "version": "4.1",
        "status": "online"
    }


@app.get("/health")
def health():
    missing = configuration_status()

    db_status = "unknown"

    if DATABASE_URL:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            conn.close()

            db_status = "ok"

        except Exception as exc:
            db_status = "error: " + str(exc)

    return {
        "service": "Geniosa",
        "version": "4.1",
        "configuration_missing": missing,
        "database": db_status,
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
    print("=" * 60)
    print("GENIOSA 4.1 STARTING")
    print("=" * 60)

    missing = configuration_status()

    if missing:
        print(
            "Missing environment variables:",
            missing
        )

    try:
        init_database()
        print("Database initialized successfully.")

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
            "Telegram bot token is missing. "
            "Polling not started."
        )

    print(
        "Geniosa 4.1 startup complete."
    )


# ============================================================
# END
# ============================================================
