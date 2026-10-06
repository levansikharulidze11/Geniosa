import os

import re

import time

import logging

import threading

from pathlib import Path

import requests

import psycopg2

from fastapi import FastAPI

from openpyxl import Workbook, load_workbook

from pptx import Presentation

from pptx.util import Inches, Pt

from pypdf import PdfReader

from docx import Document

# ============================================================

# GENIOSA v3.0

# Telegram AI Business & Investment Advisor

# ============================================================

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s | %(levelname)s | %(message)s"

)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not GEMINI_API_KEY:

    raise RuntimeError("GEMINI_API_KEY is missing")

if not DATABASE_URL:

    raise RuntimeError("DATABASE_URL is missing")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

GEMINI_API = (

    "https://generativelanguage.googleapis.com/"

    "v1beta/models/gemini-3.5-flash-lite:generateContent"

)

app = FastAPI(

    title="Geniosa",

    version="3.0"

)

# ============================================================

# DATABASE

# ============================================================

def db():

    return psycopg2.connect(DATABASE_URL)

def init_db():

    conn = db()

    cur = conn.cursor()

    cur.execute("""

        CREATE TABLE IF NOT EXISTS messages (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            role TEXT,

            content TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    cur.execute("""

        CREATE TABLE IF NOT EXISTS business_memory (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            memory TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    cur.execute("""

        CREATE TABLE IF NOT EXISTS projects (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            name TEXT,

            location TEXT,

            land_area DOUBLE PRECISION,

            saleable_area DOUBLE PRECISION,

            construction_area DOUBLE PRECISION,

            hotel_area DOUBLE PRECISION,

            investment DOUBLE PRECISION,

            revenue DOUBLE PRECISION,

            cost DOUBLE PRECISION,

            profit DOUBLE PRECISION,

            notes TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    cur.execute("""

        CREATE TABLE IF NOT EXISTS documents (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            filename TEXT,

            file_type TEXT,

            extracted_text TEXT,

            analysis TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    conn.commit()

    cur.close()

    conn.close()

# ============================================================

# MESSAGE MEMORY

# ============================================================

def save_message(chat_id, role, content):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO messages (chat_id, role, content)

        VALUES (%s, %s, %s)

        """,

        (chat_id, role, content)

    )

    conn.commit()

    cur.close()

    conn.close()

def get_messages(chat_id, limit=30):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        SELECT role, content

        FROM messages

        WHERE chat_id=%s

        ORDER BY id DESC

        LIMIT %s

        """,

        (chat_id, limit)

    )

    rows = cur.fetchall()

    cur.close()

    conn.close()

    return list(reversed(rows))

# ============================================================

# BUSINESS MEMORY

# ============================================================

def save_memory(chat_id, memory):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO business_memory (chat_id, memory)

        VALUES (%s, %s)

        """,

        (chat_id, memory)

    )

    conn.commit()

    cur.close()

    conn.close()

def get_memories(chat_id, limit=50):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        SELECT memory

        FROM business_memory

        WHERE chat_id=%s

        ORDER BY id DESC

        LIMIT %s

        """,

        (chat_id, limit)

    )

    rows = cur.fetchall()

    cur.close()

    conn.close()

    return [x[0] for x in rows]

def delete_memories(chat_id):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        "DELETE FROM business_memory WHERE chat_id=%s",

        (chat_id,)

    )

    conn.commit()

    cur.close()

    conn.close()

# ============================================================

# PROJECT DATABASE

# ============================================================

def save_project(

    chat_id,

    name,

    location,

    land_area,

    saleable_area,

    construction_area,

    hotel_area,

    investment=None,

    revenue=None,

    cost=None,

    profit=None,

    notes=""

):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO projects (

            chat_id,

            name,

            location,

            land_area,

            saleable_area,

            construction_area,

            hotel_area,

            investment,

            revenue,

            cost,

            profit,

            notes

        )

        VALUES (

            %s,%s,%s,%s,%s,%s,

            %s,%s,%s,%s,%s,%s

        )

        """,

        (

            chat_id,

            name,

            location,

            land_area,

            saleable_area,

            construction_area,

            hotel_area,

            investment,

            revenue,

            cost,

            profit,

            notes

        )

    )

    conn.commit()

    cur.close()

    conn.close()

def get_projects(chat_id):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        SELECT

            id,

            name,

            location,

            land_area,

            saleable_area,

            construction_area,

            hotel_area,

            investment,

            revenue,

            cost,

            profit,

            notes

        FROM projects

        WHERE chat_id=%s

        ORDER BY id DESC

        """,

        (chat_id,)

    )

    rows = cur.fetchall()

    cur.close()

    conn.close()

    return rows# =========================

# PART 2 — ფინანსური ძრავი + Telegram + Gemini

# =========================

def calculate_roi(investment, profit):

    if investment == 0:

        return 0

    return (profit / investment) * 100

def calculate_margin(revenue, profit):

    if revenue == 0:

        return 0

    return (profit / revenue) * 100

def calculate_payback(investment, annual_profit):

    if annual_profit <= 0:

        return None

    return investment / annual_profit

def calculate_npv(initial_investment, cashflows, discount_rate):

    npv = -initial_investment

    for year, cashflow in enumerate(cashflows, start=1):

        npv += cashflow / ((1 + discount_rate) ** year)

    return npv

def calculate_irr(initial_investment, cashflows):

    low = -0.99

    high = 10.0

    def npv(rate):

        value = -initial_investment

        for year, cashflow in enumerate(cashflows, start=1):

            value += cashflow / ((1 + rate) ** year)

        return value

    try:

        if npv(low) * npv(high) > 0:

            return None

        for _ in range(200):

            middle = (low + high) / 2

            value = npv(middle)

            if abs(value) < 0.000001:

                return middle

            if npv(low) * value <= 0:

                high = middle

            else:

                low = middle

        return (low + high) / 2

    except Exception:

        return None

# =========================

# TELEGRAM MESSAGES

# =========================

def send_message(chat_id, text):

    try:

        requests.post(

            f"{TELEGRAM_API}/sendMessage",

            json={

                "chat_id": chat_id,

                "text": text

            },

            timeout=60

        )

    except Exception as e:

        logging.error(f"Telegram send error: {e}")

def send_long_message(chat_id, text):

    if not text:

        return

    max_length = 4000

    for i in range(0, len(text), max_length):

        part = text[i:i + max_length]

        send_message(chat_id, part)

        time.sleep(0.3)

# =========================

# GEMINI AI

# =========================

def ask_gemini(user_text, chat_id, document_context=""):

    memories = get_memories(chat_id)

    projects = get_projects(chat_id)

    messages = get_messages(chat_id, 20)

    if memories:

        memory_text = "\n".join(

            f"- {m}" for m in memories

        )

    else:

        memory_text = "No saved business memory."

    if projects:

        project_text = "\n".join(

            f"- {p['name']} | "

            f"{p.get('location') or 'N/A'} | "

            f"land={p.get('land_area') or 'N/A'} m² | "

            f"saleable={p.get('saleable_area') or 'N/A'} m² | "

            f"construction={p.get('construction_area') or 'N/A'} m²"

            for p in projects

        )

    else:

        project_text = "No saved projects."

    if messages:

        history_text = "\n".join(

            f"{role}: {content}"

            for role, content in messages

        )

    else:

        history_text = ""

    system_prompt = """

You are Geniosa — a professional private business,

investment and real-estate development advisor.

Your job is to help the user make better business decisions.

IMPORTANT RULES:

1. Separate CONFIRMED FACTS from ASSUMPTIONS.

2. Never invent financial figures.

3. If information is missing, clearly say what is missing.

4. When calculating, show the formula and result.

5. For investment projects analyze:

   - revenue

   - construction cost

   - operating costs

   - financing

   - investor return

   - profit

   - risks

   - timeline

   - exit strategy

6. Point out contradictions in project data.

7. Be practical and professional.

8. Answer in Georgian unless the user asks

   for another language.

9. Never present an assumption as a confirmed fact.

10. When useful, give a recommended next step.

Think like a combination of:

- Investment Analyst

- Real Estate Developer

- CFO

- Project Manager

- Investor Relations Advisor

You are not just a generic chatbot.

You are a professional business advisor.

"""

    prompt = f"""

{system_prompt}

SAVED BUSINESS MEMORY:

{memory_text}

SAVED PROJECTS:

{project_text}

RECENT CONVERSATION:

{history_text}

DOCUMENT CONTEXT:

{document_context[:30000]}

USER REQUEST:

{user_text}

Give a professional and useful answer.

"""

    try:

        response = requests.post(

            GEMINI_URL,

            headers={

                "Content-Type": "application/json",

                "x-goog-api-key": GEMINI_API_KEY

            },

            json={

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

                    "maxOutputTokens": 4000

                }

            },

            timeout=120

        )

        if response.status_code != 200:

            logging.error(

                f"Gemini error {response.status_code}: "

                f"{response.text[:1000]}"

            )

            return "Gemini-სთან დაკავშირებისას შეცდომა მოხდა."

        data = response.json()

        candidates = data.get("candidates", [])

        if not candidates:

            return "Gemini-მ პასუხი ვერ დააბრუნა."

        parts = candidates[0].get(

            "content",

            {}

        ).get(

            "parts",

            []

        )

        answer = "\n".join(

            p.get("text", "")

            for p in parts

            if p.get("text")

        ).strip()

        if not answer:

            return "ცარიელი პასუხი მივიღე Gemini-სგან."

        return answer

    except Exception as e:

        logging.error(f"Gemini exception: {e}")

        return "AI მოდულთან დაკავშირებისას ტექნიკური შეცდომა მოხდა."

# =========================

# TELEGRAM FILE DOWNLOAD

# =========================

DOWNLOAD_DIR = Path("/tmp/geniosa")

DOWNLOAD_DIR.mkdir(

    parents=True,

    exist_ok=True

)

def download_telegram_file(file_id, filename):

    try:

        result = requests.get(

            f"{TELEGRAM_API}/getFile",

            params={

                "file_id": file_id

            },

            timeout=60

        )

        data = result.json()

        if not data.get("ok"):

            return None

        file_path = data["result"]["file_path"]

        file_url = (

            "https://api.telegram.org/file/bot"

            f"{TELEGRAM_BOT_TOKEN}/{file_path}"

        )

        file_response = requests.get(

            file_url,

            timeout=120

        )

        if file_response.status_code != 200:

            return None

        safe_name = re.sub(

            r"[^A-Za-z0-9._-]",

            "_",

            filename

        )

        local_path = DOWNLOAD_DIR / safe_name

        with open(

            local_path,

            "wb"

        ) as f:

            f.write(file_response.content)

        return str(local_path)

    except Exception as e:

        logging.error(

            f"File download error: {e}"

        )

        return None

print("Geniosa Part 2 loaded")# =========================

# PART 3 — FILE READING

# =========================

def extract_pdf(file_path):

    try:

        reader = PdfReader(file_path)

        pages = []

        for page in reader.pages:

            text = page.extract_text() or ""

            if text.strip():

                pages.append(text)

        return "\n\n".join(pages)

    except Exception as e:

        logging.error(f"PDF extraction error: {e}")

        return ""

def extract_docx(file_path):

    try:

        document = Document(file_path)

        paragraphs = []

        for paragraph in document.paragraphs:

            text = paragraph.text.strip()

            if text:

                paragraphs.append(text)

        return "\n".join(paragraphs)

    except Exception as e:

        logging.error(f"DOCX extraction error: {e}")

        return ""

def extract_pptx(file_path):

    try:

        presentation = Presentation(file_path)

        slides_text = []

        for slide_number, slide in enumerate(

            presentation.slides,

            start=1

        ):

            slide_parts = [

                f"--- SLIDE {slide_number} ---"

            ]

            for shape in slide.shapes:

                if hasattr(shape, "text"):

                    text = shape.text.strip()

                    if text:

                        slide_parts.append(text)

            slides_text.append(

                "\n".join(slide_parts)

            )

        return "\n\n".join(slides_text)

    except Exception as e:

        logging.error(f"PPTX extraction error: {e}")

        return ""

def extract_xlsx(file_path):

    try:

        workbook = load_workbook(

            file_path,

            data_only=True

        )

        sheets = []

        for worksheet in workbook.worksheets:

            rows = [

                f"--- SHEET: {worksheet.title} ---"

            ]

            for row in worksheet.iter_rows(

                values_only=True

            ):

                values = []

                for value in row:

                    if value is not None:

                        values.append(str(value))

                if values:

                    rows.append(

                        " | ".join(values)

                    )

            sheets.append(

                "\n".join(rows)

            )

        return "\n\n".join(sheets)

    except Exception as e:

        logging.error(f"XLSX extraction error: {e}")

        return ""

def extract_document(file_path):

    extension = Path(file_path).suffix.lower()

    if extension == ".pdf":

        return extract_pdf(file_path)

    if extension == ".docx":

        return extract_docx(file_path)

    if extension == ".pptx":

        return extract_pptx(file_path)

    if extension == ".xlsx":

        return extract_xlsx(file_path)

    return ""

# =========================

# SAVE DOCUMENT

# =========================

def save_document(

    chat_id,

    filename,

    file_type,

    extracted_text,

    analysis=""

):

    conn = db()

    try:

        cur = conn.cursor()

        cur.execute(

            """

            INSERT INTO documents

            (

                chat_id,

                filename,

                file_type,

                extracted_text,

                analysis

            )

            VALUES (%s, %s, %s, %s, %s)

            """,

            (

                chat_id,

                filename,

                file_type,

                extracted_text,

                analysis

            )

        )

        conn.commit()

        cur.close()

    except Exception as e:

        conn.rollback()

        logging.error(

            f"Document save error: {e}"

        )

    finally:

        conn.close()

# =========================

# DOCUMENT ANALYSIS

# =========================

def analyze_document(

    chat_id,

    filename,

    extracted_text

):

    if not extracted_text.strip():

        return (

            "ფაილიდან ტექსტის ამოღება ვერ მოხერხდა.\n"

            "შეამოწმე, ხომ არ არის დოკუმენტი "

            "სკანირებული სურათის სახით."

        )

    prompt = f"""

You are Geniosa, a professional investment

and real-estate development analyst.

Analyze the following business document.

FILE:

{filename}

DOCUMENT:

{extracted_text[:50000]}

Return the analysis in Georgian using exactly

these sections:

1. CONFIRMED PROJECT FACTS

List facts explicitly stated in the document.

2. FINANCIAL DATA

List revenues, costs, investment,

profit, prices, taxes, financing and other

financial figures.

3. INVESTOR TERMS

Identify investor contribution,

ownership, profit share, return,

financing structure and other terms.

4. RETAINED ASSETS

Identify assets that remain with the company

or are not included in sales.

5. TIMELINE

Identify construction, sales,

financing and other time periods.

6. ASSUMPTIONS

Clearly identify numbers or statements

that appear to be assumptions.

7. MISSING INFORMATION

List important information required

for a professional investment analysis

but missing from the document.

8. RISKS

Identify financial, construction,

legal, market, financing and execution risks.

9. IMPORTANT CORRECTIONS

Identify contradictions, inconsistent

numbers, unclear wording or data that

should be verified.

10. INVESTOR VIEW

Give a short professional assessment:

what is attractive to an investor,

what is weak,

and what should be corrected before

sending the document to an investor.

IMPORTANT:

Never invent information.

If something is not in the document,

say "არ არის მითითებული".

"""

    try:

        response = requests.post(

            GEMINI_URL,

            headers={

                "Content-Type": "application/json",

                "x-goog-api-key": GEMINI_API_KEY

            },

            json={

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

                    "temperature": 0.1,

                    "maxOutputTokens": 6000

                }

            },

            timeout=180

        )

        if response.status_code != 200:

            logging.error(

                f"Document Gemini error: "

                f"{response.status_code} "

                f"{response.text[:1000]}"

            )

            return (

                "დოკუმენტის AI ანალიზისას "

                "შეცდომა მოხდა."

            )

        data = response.json()

        candidates = data.get(

            "candidates",

            []

        )

        if not candidates:

            return "Gemini-მ ანალიზი ვერ დააბრუნა."

        parts = candidates[0].get(

            "content",

            {}

        ).get(

            "parts",

            []

        )

        analysis = "\n".join(

            p.get("text", "")

            for p in parts

            if p.get("text")

        ).strip()

        if not analysis:

            return "დოკუმენტის ანალიზი ცარიელია."

        return analysis

    except Exception as e:

        logging.error(

            f"Document analysis error: {e}"

        )

        return (

            "დოკუმენტის ანალიზისას "

            "ტექნიკური შეცდომა მოხდა."

        )

print("Geniosa Part 3 loaded")# =========================

# PART 4 — MEMORY + PROJECTS

# =========================

def extract_memories_from_analysis(chat_id, analysis):

    if not analysis:

        return

    prompt = f"""

You are Geniosa.

From the following business document analysis,

identify important LONG-TERM business facts that

should be remembered for future conversations.

Only save facts that are explicitly supported

by the analysis.

Do NOT save:

- temporary conversation details

- guesses

- uncertain assumptions

- general advice

Return one fact per line.

ANALYSIS:

{analysis[:30000]}

"""

    try:

        response = requests.post(

            GEMINI_URL,

            headers={

                "Content-Type": "application/json",

                "x-goog-api-key": GEMINI_API_KEY

            },

            json={

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

                    "temperature": 0.1,

                    "maxOutputTokens": 2500

                }

            },

            timeout=120

        )

        if response.status_code != 200:

            return

        data = response.json()

        candidates = data.get(

            "candidates",

            []

        )

        if not candidates:

            return

        parts = candidates[0].get(

            "content",

            {}

        ).get(

            "parts",

            []

        )

        result = "\n".join(

            p.get("text", "")

            for p in parts

            if p.get("text")

        ).strip()

        if not result:

            return

        lines = result.splitlines()

        saved = 0

        for line in lines:

            fact = line.strip()

            if not fact:

                continue

            fact = re.sub(

                r"^[\-\*\d\.\)\s]+",

                "",

                fact

            ).strip()

            if len(fact) < 10:

                continue

            save_memory(

                chat_id,

                fact

            )

            saved += 1

            if saved >= 30:

                break

    except Exception as e:

        logging.error(

            f"Memory extraction error: {e}"

        )

# =========================

# PROJECT COMMAND

# =========================

def handle_project_command(

    chat_id,

    text

):

    raw = text[len("/project"):].strip()

    if not raw:

        return (

            "გამოყენება:\n\n"

            "/project პროექტის სახელი | "

            "ქალაქი | მიწა მ² | გასაყიდი მ² | "

            "მშენებლობა მ² | სრული ფართობი მ²\n\n"

            "მაგალითი:\n"

            "/project NIKKEA 12 | Kutaisi | "

            "3070 | 8848 | 16000 | 16000"

        )

    parts = [

        p.strip()

        for p in raw.split("|")

    ]

    if len(parts) < 2:

        return (

            "ფორმატი არასწორია.\n\n"

            "გამოიყენე:\n"

            "/project სახელი | ქალაქი | "

            "მიწა | გასაყიდი | მშენებლობა | სრული ფართობი"

        )

    name = parts[0]

    location = parts[1] if len(parts) > 1 else ""

    def number(index):

        if len(parts) <= index:

            return None

        value = parts[index].strip()

        if not value:

            return None

        try:

            return float(

                value.replace(",", "")

            )

        except Exception:

            return None

    land_area = number(2)

    saleable_area = number(3)

    construction_area = number(4)

    total_area = number(5)

    save_project(

        chat_id=chat_id,

        name=name,

        location=location,

        land_area=land_area,

        saleable_area=saleable_area,

        construction_area=construction_area,

        total_area=total_area

    )

    return (

        "✅ პროექტი შენახულია.\n\n"

        f"პროექტი: {name}\n"

        f"ლოკაცია: {location}\n"

        f"მიწა: {land_area or 'N/A'} მ²\n"

        f"გასაყიდი: {saleable_area or 'N/A'} მ²\n"

        f"მშენებლობა: {construction_area or 'N/A'} მ²\n"

        f"სრული ფართობი: {total_area or 'N/A'} მ²"

    )

# =========================

# PROJECT LIST

# =========================

def handle_projects_command(chat_id):

    projects = get_projects(chat_id)

    if not projects:

        return (

            "პროექტები ჯერ არ არის შენახული."

        )

    result = [

        "📁 შენი პროექტები:",

        ""

    ]

    for index, project in enumerate(

        projects,

        start=1

    ):

        result.append(

            f"{index}. {project['name']}"

        )

        result.append(

            f"   📍 {project.get('location') or 'N/A'}"

        )

        result.append(

            f"   🏗 მიწა: "

            f"{project.get('land_area') or 'N/A'} მ²"

        )

        result.append(

            f"   💰 გასაყიდი: "

            f"{project.get('saleable_area') or 'N/A'} მ²"

        )

        result.append("")

    return "\n".join(result)

# =========================

# MEMORY COMMANDS

# =========================

def handle_memory_command(chat_id):

    memories = get_memories(chat_id)

    if not memories:

        return (

            "🧠 Geniosa-ს მეხსიერებაში "

            "ჯერ ინფორმაცია არ არის."

        )

    result = [

        "🧠 შენახული ბიზნეს ინფორმაცია:",

        ""

    ]

    for index, memory in enumerate(

        memories,

        start=1

    ):

        result.append(

            f"{index}. {memory}"

        )

    return "\n".join(result)

def handle_remember_command(

    chat_id,

    text

):

    memory = text[

        len("/remember"):

    ].strip()

    if not memory:

        return (

            "გამოყენება:\n"

            "/remember აქ ჩაწერე ინფორმაცია"

        )

    save_memory(

        chat_id,

        memory

    )

    return (

        "🧠 დამახსოვრებულია:\n\n"

        f"{memory}"

    )

def handle_forget_command(chat_id):

    delete_memories(chat_id)

    return (

        "🧹 ბიზნეს მეხსიერება წაიშალა.\n\n"

        "პროექტები და შეტყობინებების ისტორია "

        "არ წაშლილა."

    )

# =========================

# DOCUMENT ANALYSIS COMMAND

# =========================

def analyze_saved_document(

    chat_id,

    document_id

):

    conn = db()

    try:

        cur = conn.cursor()

        cur.execute(

            """

            SELECT filename, extracted_text, analysis

            FROM documents

            WHERE id = %s

            AND chat_id = %s

            """,

            (

                document_id,

                chat_id

            )

        )

        row = cur.fetchone()

        cur.close()

        if not row:

            return (

                "დოკუმენტი ვერ მოიძებნა."

            )

        filename = row[0]

        extracted_text = row[1] or ""

        analysis = row[2] or ""

        if not analysis:

            analysis = analyze_document(

                chat_id,

                filename,

                extracted_text

            )

            cur = conn.cursor()

            cur.execute(

                """

                UPDATE documents

                SET analysis = %s

                WHERE id = %s

                AND chat_id = %s

                """,

                (

                    analysis,

                    document_id,

                    chat_id

                )

            )

            conn.commit()

            cur.close()

            extract_memories_from_analysis(

                chat_id,

                analysis

            )

        return analysis

    except Exception as e:

        conn.rollback()

        logging.error(

            f"Saved document analysis error: {e}"

        )

        return (

            "დოკუმენტის ანალიზისას "

            "შეცდომა მოხდა."

        )

    finally:

        conn.close()

# =========================

# SAVE ANALYSIS

# =========================

def save_document_analysis(

    chat_id,

    filename,

    file_type,

    extracted_text,

    analysis

):

    save_document(

        chat_id=chat_id,

        filename=filename,

        file_type=file_type,

        extracted_text=extracted_text,

        analysis=analysis

    )

    extract_memories_from_analysis(

        chat_id,

        analysis

    )

print("Geniosa Part 4 loaded")# =========================

# PART 5 — TELEGRAM BOT ENGINE

# =========================

def handle_calculator(chat_id, text):

    raw = text[len("/calc"):].strip()

    if not raw:

        return (

            "🧮 კალკულატორი\n\n"

            "გამოყენება:\n"

            "/calc roi 100000 30000\n"

            "/calc margin 500000 100000\n"

            "/calc payback 100000 25000\n"

            "/calc npv 100000 50000,50000,50000 10\n"

            "/calc irr 100000 30000,40000,50000"

        )

    parts = raw.split()

    try:

        calculation = parts[0].lower()

        if calculation == "roi":

            investment = float(parts[1])

            profit = float(parts[2])

            result = calculate_roi(

                investment,

                profit

            )

            return (

                "📊 ROI\n\n"

                f"ინვესტიცია: ${investment:,.2f}\n"

                f"მოგება: ${profit:,.2f}\n\n"

                f"ROI = {result:.2f}%"

            )

        elif calculation == "margin":

            revenue = float(parts[1])

            profit = float(parts[2])

            result = calculate_margin(

                revenue,

                profit

            )

            return (

                "📊 Profit Margin\n\n"

                f"შემოსავალი: ${revenue:,.2f}\n"

                f"მოგება: ${profit:,.2f}\n\n"

                f"Margin = {result:.2f}%"

            )

        elif calculation == "payback":

            investment = float(parts[1])

            annual_profit = float(parts[2])

            result = calculate_payback(

                investment,

                annual_profit

            )

            if result is None:

                return (

                    "Payback ვერ გამოითვალა.\n"

                    "წლიური მოგება უნდა იყოს 0-ზე მეტი."

                )

            return (

                "⏱ Payback Period\n\n"

                f"ინვესტიცია: ${investment:,.2f}\n"

                f"წლიური მოგება: ${annual_profit:,.2f}\n\n"

                f"Payback = {result:.2f} წელი"

            )

        elif calculation == "npv":

            investment = float(parts[1])

            cashflows = [

                float(x)

                for x in parts[2].split(",")

            ]

            discount_rate = (

                float(parts[3]) / 100

            )

            result = calculate_npv(

                investment,

                cashflows,

                discount_rate

            )

            return (

                "📈 NPV\n\n"

                f"საწყისი ინვესტიცია: "

                f"${investment:,.2f}\n"

                f"დისკონტის განაკვეთი: "

                f"{discount_rate * 100:.2f}%\n\n"

                f"NPV = ${result:,.2f}"

            )

        elif calculation == "irr":

            investment = float(parts[1])

            cashflows = [

                float(x)

                for x in parts[2].split(",")

            ]

            result = calculate_irr(

                investment,

                cashflows

            )

            if result is None:

                return (

                    "IRR ვერ გამოითვალა ამ "

                    "ფულადი ნაკადებით."

                )

            return (

                "📈 IRR\n\n"

                f"საწყისი ინვესტიცია: "

                f"${investment:,.2f}\n\n"

                f"IRR = {result * 100:.2f}%"

            )

        else:

            return (

                "უცნობი კალკულაცია.\n\n"

                "ხელმისაწვდომია:\n"

                "roi\n"

                "margin\n"

                "payback\n"

                "npv\n"

                "irr"

            )

    except (ValueError, IndexError):

        return (

            "❌ კალკულაციის ფორმატი არასწორია.\n\n"

            "მაგალითი:\n"

            "/calc roi 100000 30000"

        )

# =========================

# HELP

# =========================

def get_help():

    return """

🤖 GENIOSA

პროფესიონალური ბიზნესისა და

ინვესტიციების AI მრჩეველი.

ძირითადი ბრძანებები:

/start

დაწყება

/help

ბრძანებების სია

/memory

შენახული ბიზნეს ინფორმაცია

/remember ტექსტი

ინფორმაციის დამახსოვრება

/forget

ბიზნეს მეხსიერების გასუფთავება

/projects

შენახული პროექტები

/project

ახალი პროექტის დამატება

/calc

ფინანსური კალკულატორი

/analyze

დოკუმენტის ანალიზი

/package პროექტი

Investor Package-ის შექმნა

📎 შეგიძლია გამომიგზავნო:

PDF

PPTX

XLSX

DOCX

Geniosa წაიკითხავს დოკუმენტს,

გაანალიზებს ინფორმაციას და შეინახავს

მნიშვნელოვან ბიზნეს ფაქტებს.

უბრალოდ მომწერე ჩვეულებრივი ტექსტითაც

ნებისმიერი ბიზნეს ან საინვესტიციო კითხვა.

"""

# =========================

# START COMMAND

# =========================

def handle_start(chat_id):

    return """

🤖 მოგესალმები Geniosa-ში.

მე ვარ შენი პირადი ბიზნესისა და

ინვესტიციების AI მრჩეველი.

შემიძლია:

• პროექტების დამახსოვრება

• ბიზნეს ინფორმაციის შენახვა

• PDF / Excel / Word / PowerPoint

  ფაილების ანალიზი

• ფინანსური გათვლები

• ROI / IRR / NPV / Payback

• საინვესტიციო პროექტების შეფასება

• Investor Package-ის მომზადება

დასაწყებად გამომიგზავნე პროექტის

ინფორმაცია ან ფაილი.

მაგალითად:

/project NIKKEA 12 | Kutaisi | 3070 | 8848 | 16000 | 16000

"""

# =========================

# DOCUMENT HANDLER

# =========================

def handle_document(

    chat_id,

    document

):

    filename = document.get(

        "file_name",

        "document"

    )

    file_id = document.get(

        "file_id"

    )

    if not file_id:

        send_message(

            chat_id,

            "❌ ფაილის ID ვერ მოიძებნა."

        )

        return

    extension = Path(

        filename

    ).suffix.lower()

    allowed = [

        ".pdf",

        ".pptx",

        ".xlsx",

        ".docx"

    ]

    if extension not in allowed:

        send_message(

            chat_id,

            "❌ ამ ეტაპზე მხარდაჭერილია მხოლოდ:\n\n"

            "PDF\n"

            "PPTX\n"

            "XLSX\n"

            "DOCX"

        )

        return

    send_message(

        chat_id,

        "📥 ფაილი მივიღე.\n\n"

        "ვკითხულობ დოკუმენტს..."

    )

    local_path = download_telegram_file(

        file_id,

        filename

    )

    if not local_path:

        send_message(

            chat_id,

            "❌ ფაილის ჩამოტვირთვა ვერ მოხერხდა."

        )

        return

    send_message(

        chat_id,

        "🔎 დოკუმენტის ტექსტს ვამუშავებ..."

    )

    extracted_text = extract_document(

        local_path

    )

    if not extracted_text.strip():

        send_message(

            chat_id,

            "❌ დოკუმენტიდან ინფორმაციის ამოღება ვერ მოხერხდა."

        )

        return

    send_message(

        chat_id,

        "🧠 ახლა Geniosa აანალიზებს დოკუმენტს..."

    )

    analysis = analyze_document(

        chat_id,

        filename,

        extracted_text

    )

    save_document_analysis(

        chat_id=chat_id,

        filename=filename,

        file_type=extension,

        extracted_text=extracted_text,

        analysis=analysis

    )

    send_long_message(

        chat_id,

        "✅ დოკუმენტი დამუშავებულია.\n\n"

        + analysis

    )

# =========================

# TEXT MESSAGE HANDLER

# =========================

def handle_text_message(

    chat_id,

    text

):

    text = text.strip()

    if not text:

        return

    save_message(

        chat_id,

        "user",

        text

    )

    if text == "/start":

        answer = handle_start(

            chat_id

        )

    elif text == "/help":

        answer = get_help()

    elif text == "/memory":

        answer = handle_memory_command(

            chat_id

        )

    elif text.startswith("/remember"):

        answer = handle_remember_command(

            chat_id,

            text

        )

    elif text == "/forget":

        answer = handle_forget_command(

            chat_id

        )

    elif text == "/projects":

        answer = handle_projects_command(

            chat_id

        )

    elif text.startswith("/project"):

        answer = handle_project_command(

            chat_id,

            text

        )

    elif text.startswith("/calc"):

        answer = handle_calculator(

            chat_id,

            text

        )

    elif text.startswith("/analyze"):

        answer = (

            "📎 გამომიგზავნე PDF, PPTX, XLSX "

            "ან DOCX ფაილი და მე მას გავაანალიზებ."

        )

    elif text.startswith("/package"):

        answer = (

            "📦 Investor Package-ის გენერაცია "

            "შემდეგ ეტაპზე დაემატება.\n\n"

            "ჯერ პროექტის ფაილი გამომიგზავნე."

        )

    else:

        answer = ask_gemini(

            text,

            chat_id

        )

    save_message(

        chat_id,

        "assistant",

        answer

    )

    send_long_message(

        chat_id,

        answer

    )

print("Geniosa Part 5 loaded")# =========================

# PART 6 — TELEGRAM POLLING + FASTAPI

# =========================

def process_update(update):

    try:

        message = update.get("message")

        if not message:

            return

        chat = message.get("chat")

        if not chat:

            return

        chat_id = chat.get("id")

        # =========================

        # DOCUMENT

        # =========================

        document = message.get("document")

        if document:

            handle_document(

                chat_id,

                document

            )

            return

        # =========================

        # TEXT

        # =========================

        text = message.get(

            "text",

            ""

        )

        if text:

            handle_text_message(

                chat_id,

                text

            )

    except Exception as e:

        logging.error(

            f"Update processing error: {e}"

        )

def delete_webhook():

    try:

        response = requests.get(

            f"{TELEGRAM_API}/deleteWebhook",

            params={

                "drop_pending_updates": False

            },

            timeout=30

        )

        logging.info(

            f"deleteWebhook: {response.text}"

        )

    except Exception as e:

        logging.error(

            f"deleteWebhook error: {e}"

        )

def telegram_polling():

    logging.info(

        "Geniosa Telegram polling started"

    )

    delete_webhook()

    offset = None

    while True:

        try:

            params = {

                "timeout": 50

            }

            if offset is not None:

                params["offset"] = offset

            response = requests.get(

                f"{TELEGRAM_API}/getUpdates",

                params=params,

                timeout=65

            )

            if response.status_code != 200:

                logging.error(

                    f"Telegram API error: "

                    f"{response.status_code}"

                )

                time.sleep(5)

                continue

            data = response.json()

            if not data.get("ok"):

                logging.error(

                    f"Telegram returned error: "

                    f"{data}"

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

                process_update(

                    update

                )

        except requests.exceptions.ReadTimeout:

            continue

        except Exception as e:

            error_text = str(e)

            logging.error(

                f"Polling error: {error_text}"

            )

            # Telegram 409 Conflict:

            # სხვა polling connection შეიძლება

            # დროებით იყოს აქტიური.

            if "409" in error_text:

                time.sleep(10)

            else:

                time.sleep(5)

# =========================

# FASTAPI

# =========================

@app.get("/")

def root():

    return {

        "status": "online",

        "service": "Geniosa",

        "version": "3.0"

    }

@app.get("/health")

def health():

    return {

        "status": "healthy"

    }

# =========================

# STARTUP

# =========================

@app.on_event("startup")

def startup_event():

    logging.info(

        "Geniosa startup started"

    )

    try:

        init_db()

        logging.info(

            "Database initialized"

        )

    except Exception as e:

        logging.error(

            f"Database initialization error: {e}"

        )

    polling_thread = threading.Thread(

        target=telegram_polling,

        daemon=True

    )

    polling_thread.start()

    logging.info(

        "Telegram polling thread started"

    )

print("Geniosa Part 6 loaded")
