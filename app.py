os

import time

import logging

import threading

import requests

import psycopg2

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not GEMINI_API_KEY:

    raise RuntimeError("GEMINI_API_KEY is not set")

if not DATABASE_URL:

    raise RuntimeError("DATABASE_URL is not set")

TELEGRAM_API = (

    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

)

GEMINI_API = (

    "https://generativelanguage.googleapis.com/v1beta/"

    "models/gemini-3.5-flash-lite:generateContent"

)

# =========================================================

# DATABASE

# =========================================================

def get_db_connection():

    return psycopg2.connect(DATABASE_URL)

def init_database():

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        # Conversation memory

        cursor.execute("""

            CREATE TABLE IF NOT EXISTS messages (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                role VARCHAR(20) NOT NULL,

                message TEXT NOT NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

        """)

        # Long-term business memory

        cursor.execute("""

            CREATE TABLE IF NOT EXISTS business_memory (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                memory TEXT NOT NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

        """)

        # Projects

        cursor.execute("""

            CREATE TABLE IF NOT EXISTS projects (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                name TEXT NOT NULL,

                location TEXT,

                land_area NUMERIC,

                saleable_area NUMERIC,

                construction_area NUMERIC,

                hotel_area NUMERIC,

                investment NUMERIC,

                revenue NUMERIC,

                cost NUMERIC,

                profit NUMERIC,

                notes TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

        """)

        connection.commit()

        cursor.close()

        logging.info(

            "PostgreSQL database initialized successfully."

        )

    except Exception:

        logging.exception(

            "Database initialization error"

        )

    finally:

        if connection:

            connection.close()

# =========================================================

# MESSAGES

# =========================================================

def save_message(chat_id, role, message):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            INSERT INTO messages

            (chat_id, role, message)

            VALUES (%s, %s, %s)

            """,

            (

                chat_id,

                role,

                message

            )

        )

        connection.commit()

        cursor.close()

    except Exception:

        logging.exception(

            "Could not save message"

        )

    finally:

        if connection:

            connection.close()

def get_recent_messages(chat_id, limit=20):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            SELECT role, message

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

        rows = cursor.fetchall()

        cursor.close()

        rows.reverse()

        return rows

    except Exception:

        logging.exception(

            "Could not read messages"

        )

        return []

    finally:

        if connection:

            connection.close()

def count_messages(chat_id):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            SELECT COUNT(*)

            FROM messages

            WHERE chat_id = %s

            """,

            (chat_id,)

        )

        count = cursor.fetchone()[0]

        cursor.close()

        return count

    except Exception:

        logging.exception(

            "Could not count messages"

        )

        return 0

    finally:

        if connection:

            connection.close()

def delete_memory(chat_id):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            DELETE FROM messages

            WHERE chat_id = %s

            """,

            (chat_id,)

        )

        deleted = cursor.rowcount

        connection.commit()

        cursor.close()

        return deleted

    except Exception:

        logging.exception(

            "Could not delete memory"

        )

        return 0

    finally:

        if connection:

            connection.close()

# =========================================================

# BUSINESS MEMORY

# =========================================================

def save_business_memory(chat_id, memory):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            INSERT INTO business_memory

            (chat_id, memory)

            VALUES (%s, %s)

            """,

            (

                chat_id,

                memory

            )

        )

        connection.commit()

        cursor.close()

    except Exception:

        logging.exception(

            "Could not save business memory"

        )

    finally:

        if connection:

            connection.close()

def get_business_memory(chat_id, limit=50):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            SELECT memory

            FROM business_memory

            WHERE chat_id = %s

            ORDER BY id DESC

            LIMIT %s

            """,

            (

                chat_id,

                limit

            )

        )

        rows = cursor.fetchall()

        cursor.close()

        return [

            row[0]

            for row in rows

        ]

    except Exception:

        logging.exception(

            "Could not read business memory"

        )

        return []

    finally:

        if connection:

            connection.close()

# =========================================================

# PROJECTS

# =========================================================

def save_project(

    chat_id,

    name,

    location="",

    land_area=None,

    saleable_area=None,

    construction_area=None,

    hotel_area=None,

    investment=None,

    revenue=None,

    cost=None,

    profit=None,

    notes=""

):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            INSERT INTO projects

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

            VALUES

            (

                %s, %s, %s, %s, %s, %s,

                %s, %s, %s, %s, %s, %s

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

        connection.commit()

        cursor.close()

        return True

    except Exception:

        logging.exception(

            "Could not save project"

        )

        return False

    finally:

        if connection:

            connection.close()

def get_projects(chat_id):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

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

            WHERE chat_id = %s

            ORDER BY id DESC

            """,

            (chat_id,)

        )

        rows = cursor.fetchall()

        cursor.close()

        return rows

    except Exception:

        logging.exception(

            "Could not read projects"

        )

        return []

    finally:

        if connection:

            connection.close()

# =========================================================

# FINANCIAL ENGINE

# =========================================================

def calculate_roi(investment, profit):

    if not investment:

        return 0

    return (

        profit / investment

    ) * 100

def calculate_margin(revenue, profit):

    if not revenue:

        return 0

    return (

        profit / revenue

    ) * 100

def calculate_payback(

    investment,

    annual_profit

):

    if annual_profit <= 0:

        return None

    return investment / annual_profit

def calculate_npv(

    investment,

    cash_flows,

    discount_rate

):

    npv = -investment

    for year, cash_flow in enumerate(

        cash_flows,

        start=1

    ):

        npv += (

            cash_flow /

            ((1 + discount_rate) ** year)

        )

    return npv

def calculate_irr(

    investment,

    cash_flows

):

    low = -0.99

    high = 10.0

    for _ in range(200):

        rate = (

            low + high

        ) / 2

        npv = -investment

        for year, cash_flow in enumerate(

            cash_flows,

            start=1

        ):

            npv += (

                cash_flow /

                ((1 + rate) ** year)

            )

        if abs(npv) < 0.0001:

            return rate * 100

        if npv > 0:

            low = rate

        else:

            high = rate

    return (

        (low + high) / 2

    ) * 100

# =========================================================

# TELEGRAM

# =========================================================

def send_message(chat_id, text):

    response = requests.post(

        f"{TELEGRAM_API}/sendMessage",

        json={

            "chat_id": chat_id,

            "text": text

        },

        timeout=30

    )

    response.raise_for_status()# =========================================================

# GEMINI AI

# =========================================================

def ask_gemini(user_text, chat_id):

    recent_messages = get_recent_messages(

        chat_id,

        limit=30

    )

    business_memories = get_business_memory(

        chat_id,

        limit=50

    )

    projects = get_projects(chat_id)

    conversation_context = []

    for role, message in recent_messages:

        if role == "user":

            conversation_context.append(

                f"User: {message}"

            )

        elif role == "assistant":

            conversation_context.append(

                f"Geniosa: {message}"

            )

    conversation_text = "\n".join(

        conversation_context

    )

    memory_text = "\n".join(

        f"- {memory}"

        for memory in reversed(

            business_memories

        )

    )

    project_text = ""

    for project in projects:

        (

            project_id,

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

        ) = project

        project_text += (

            f"\nProject #{project_id}\n"

            f"Name: {name}\n"

            f"Location: {location}\n"

            f"Land area: {land_area}\n"

            f"Saleable area: {saleable_area}\n"

            f"Construction area: {construction_area}\n"

            f"Hotel area: {hotel_area}\n"

            f"Investment: {investment}\n"

            f"Revenue: {revenue}\n"

            f"Cost: {cost}\n"

            f"Profit: {profit}\n"

            f"Notes: {notes}\n"

        )

    system_instruction = """

You are Geniosa — a professional private

business and investment advisor.

Your expertise includes:

1. Business strategy

2. Investments

3. Investor relations

4. Construction

5. Real estate development

6. Financial modelling

7. ROI

8. IRR

9. NPV

10. Payback

11. Cash flow

12. Deal structuring

13. Negotiations

14. Market research

15. Risk analysis

16. Due diligence

Always give practical and actionable answers.

Clearly separate facts, assumptions

and estimates.

Never invent financial figures,

companies, investors or market data.

If important information is missing,

ask the user for it.

When financial calculations are relevant,

show the calculation clearly.

When evaluating investments,

consider revenue, costs, profit,

ROI, IRR, payback and risks.

When evaluating a deal,

identify weaknesses and suggest

better structures.

Protect the user's business interests.

Do not blindly agree with the user.

If an idea is weak or risky,

explain why.

The BUSINESS MEMORY contains

long-term information deliberately

saved by the user.

The PROJECT DATABASE contains

structured project information.

Use stored project information when

the user asks about an existing project.

Always answer in the same language

as the user.

If Georgian, answer Georgian.

If Russian, answer Russian.

If English, answer English.

Be professional, direct and practical.

"""

    full_prompt = f"""

BUSINESS MEMORY:

{memory_text}

PROJECT DATABASE:

{project_text}

RECENT CONVERSATION:

{conversation_text}

CURRENT USER MESSAGE:

{user_text}

"""

    headers = {

        "Content-Type": "application/json",

        "x-goog-api-key": GEMINI_API_KEY

    }

    data = {

        "system_instruction": {

            "parts": [

                {

                    "text": system_instruction

                }

            ]

        },

        "contents": [

            {

                "role": "user",

                "parts": [

                    {

                        "text": full_prompt

                    }

                ]

            }

        ]

    }

    response = requests.post(

        GEMINI_API,

        headers=headers,

        json=data,

        timeout=60

    )

    if not response.ok:

        logging.error(

            "Gemini API error %s: %s",

            response.status_code,

            response.text

        )

    response.raise_for_status()

    result = response.json()

    candidates = result.get(

        "candidates",

        []

    )

    if not candidates:

        raise RuntimeError(

            "Gemini returned no candidates"

        )

    parts = candidates[0].get(

        "content",

        {}

    ).get(

        "parts",

        []

    )

    if not parts:

        raise RuntimeError(

            "Gemini returned no text"

        )

    return parts[0].get(

        "text",

        "ბოდიში, პასუხი ვერ მივიღე."

    )# =========================================================

# TELEGRAM HELPERS

# =========================================================

def send_long_message(chat_id, text):

    max_length = 4000

    if not text:

        return

    for i in range(

        0,

        len(text),

        max_length

    ):

        send_message(

            chat_id,

            text[i:i + max_length]

        )

# =========================================================

# PROJECT COMMAND

# =========================================================

def handle_project_command(

    chat_id,

    text

):

    content = text[

        len("/project"):

    ].strip()

    if not content:

        return (

            "გამოყენება:\n\n"

            "/project სახელი | ქალაქი | "

            "მიწა | გასაყიდი | მშენებლობა | სასტუმრო\n\n"

            "მაგალითი:\n"

            "/project NIKKEA 12 | Kutaisi | "

            "3070 | 10854 | 16000 | 7212"

        )

    parts = [

        part.strip()

        for part in content.split("|")

    ]

    try:

        name = parts[0]

        location = (

            parts[1]

            if len(parts) > 1

            else ""

        )

        land_area = (

            float(parts[2])

            if len(parts) > 2 and parts[2]

            else None

        )

        saleable_area = (

            float(parts[3])

            if len(parts) > 3 and parts[3]

            else None

        )

        construction_area = (

            float(parts[4])

            if len(parts) > 4 and parts[4]

            else None

        )

        hotel_area = (

            float(parts[5])

            if len(parts) > 5 and parts[5]

            else None

        )

    except Exception:

        return (

            "❌ ფორმატი არასწორია.\n\n"

            "მაგალითი:\n"

            "/project NIKKEA 12 | Kutaisi | "

            "3070 | 10854 | 16000 | 7212"

        )

    success = save_project(

        chat_id=chat_id,

        name=name,

        location=location,

        land_area=land_area,

        saleable_area=saleable_area,

        construction_area=construction_area,

        hotel_area=hotel_area

    )

    if success:

        return (

            "✅ პროექტი შენახულია.\n\n"

            f"პროექტი: {name}\n"

            f"📍 {location}\n"

            f"🌍 მიწა: {land_area} მ²\n"

            f"🏢 გასაყიდი: {saleable_area} მ²\n"

            f"🏗 მშენებლობა: {construction_area} მ²\n"

            f"🏨 სასტუმრო: {hotel_area} მ²"

        )

    return (

        "❌ პროექტის შენახვა ვერ მოხერხდა."

    )

# =========================================================

# PROJECT LIST

# =========================================================

def format_projects(chat_id):

    projects = get_projects(chat_id)

    if not projects:

        return (

            "📂 პროექტების ბაზა ცარიელია."

        )

    result = [

        "📂 Geniosa-ს პროექტების ბაზა:"

    ]

    for project in projects:

        (

            project_id,

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

        ) = project

        result.append(

            f"\n#{project_id} — {name}\n"

            f"📍 {location or '-'}\n"

            f"🌍 მიწა: {land_area or '-'} მ²\n"

            f"🏢 გასაყიდი: "

            f"{saleable_area or '-'} მ²\n"

            f"🏗 მშენებლობა: "

            f"{construction_area or '-'} მ²\n"

            f"🏨 სასტუმრო: "

            f"{hotel_area or '-'} მ²"

        )

    return "\n".join(result)

# =========================================================

# FINANCIAL CALCULATOR

# =========================================================

def handle_calc_command(

    chat_id,

    text

):

    content = text[

        len("/calc"):

    ].strip()

    parts = content.split()

    if len(parts) < 1:

        return (

            "📊 ფინანსური კალკულატორი\n\n"

            "/calc roi ინვესტიცია მოგება\n"

            "/calc margin შემოსავალი მოგება\n"

            "/calc payback ინვესტიცია წლიური_მოგება\n"

            "/calc npv ინვესტიცია განაკვეთი ფულადი1,ფულადი2\n"

            "/calc irr ინვესტიცია ფულადი1,ფულადი2"

        )

    command = parts[0].lower()

    try:

        if command == "roi":

            investment = float(parts[1])

            profit = float(parts[2])

            roi = calculate_roi(

                investment,

                profit

            )

            return (

                "📊 ROI\n\n"

                f"ინვესტიცია: ${investment:,.2f}\n"

                f"მოგება: ${profit:,.2f}\n"

                f"ROI: {roi:.2f}%"

            )

        if command == "margin":

            revenue = float(parts[1])

            profit = float(parts[2])

            margin = calculate_margin(

                revenue,

                profit

            )

            return (

                "📊 Profit Margin\n\n"

                f"შემოსავალი: ${revenue:,.2f}\n"

                f"მოგება: ${profit:,.2f}\n"

                f"Margin: {margin:.2f}%"

            )

        if command == "payback":

            investment = float(parts[1])

            annual_profit = float(parts[2])

            payback = calculate_payback(

                investment,

                annual_profit

            )

            if payback is None:

                return (

                    "❌ Payback ვერ ითვლება."

                )

            return (

                "📊 Payback Period\n\n"

                f"ინვესტიცია: ${investment:,.2f}\n"

                f"წლიური მოგება: "

                f"${annual_profit:,.2f}\n"

                f"დაბრუნება: {payback:.2f} წელი"

            )

        if command == "npv":

            investment = float(parts[1])

            discount_rate = (

                float(parts[2]) / 100

            )

            cash_flows = [

                float(x)

                for x in parts[3].split(",")

            ]

            npv = calculate_npv(

                investment,

                cash_flows,

                discount_rate

            )

            return (

                "📊 NPV\n\n"

                f"ინვესტიცია: ${investment:,.2f}\n"

                f"განაკვეთი: "

                f"{discount_rate * 100:.2f}%\n"

                f"NPV: ${npv:,.2f}"

            )

        if command == "irr":

            investment = float(parts[1])

            cash_flows = [

                float(x)

                for x in parts[2].split(",")

            ]

            irr = calculate_irr(

                investment,

                cash_flows

            )

            return (

                "📊 IRR\n\n"

                f"ინვესტიცია: ${investment:,.2f}\n"

                f"IRR: {irr:.2f}%"

            )

        return (

            "❌ უცნობი ფინანსური ბრძანება."

        )

    except (IndexError, ValueError):

        return (

            "❌ მონაცემების ფორმატი არასწორია.\n\n"

            "მაგალითი:\n"

            "/calc roi 100000 30000"

        )# =========================================================

# TELEGRAM POLLING

# =========================================================

def telegram_polling():

    logging.info(

        "Starting Telegram polling..."

    )

    try:

        # Remove webhook if one exists.

        requests.get(

            f"{TELEGRAM_API}/deleteWebhook",

            params={

                "drop_pending_updates": False

            },

            timeout=20

        )

        me = requests.get(

            f"{TELEGRAM_API}/getMe",

            timeout=20

        )

        me.raise_for_status()

        bot_info = me.json()["result"]

        logging.info(

            "Telegram bot connected: @%s",

            bot_info.get("username")

        )

    except Exception:

        logging.exception(

            "Could not connect to Telegram"

        )

        time.sleep(5)

    offset = None

    while True:

        try:

            params = {

                "timeout": 30

            }

            if offset is not None:

                params["offset"] = offset

            response = requests.get(

                f"{TELEGRAM_API}/getUpdates",

                params=params,

                timeout=40

            )

            if response.status_code == 409:

                logging.warning(

                    "Telegram polling conflict (409). "

                    "Waiting 10 seconds..."

                )

                time.sleep(10)

                continue

            response.raise_for_status()

            data = response.json()

            for update in data.get(

                "result",

                []

            ):

                offset = (

                    update["update_id"] + 1

                )

                message = update.get(

                    "message"

                )

                if not message:

                    continue

                chat = message.get(

                    "chat"

                )

                text = message.get(

                    "text"

                )

                if not chat or not text:

                    continue

                chat_id = chat["id"]

                logging.info(

                    "Message received from %s: %s",

                    chat_id,

                    text

                )

                command = text.strip().lower()

                # =================================================

                # START

                # =================================================

                if command == "/start":

                    send_message(

                        chat_id,

                        "🧠 Geniosa მზად არის.\n\n"

                        "მე ვარ შენი პირადი ბიზნესისა და "

                        "ინვესტიციების მრჩეველი.\n\n"

                        "შემიძლია დაგეხმარო:\n"

                        "• ბიზნეს სტრატეგიაში\n"

                        "• ინვესტიციებში\n"

                        "• სამშენებლო პროექტებში\n"

                        "• უძრავ ქონებაში\n"

                        "• ფინანსურ ანალიზში\n"

                        "• ROI / IRR / NPV-ში\n"

                        "• პროექტების შენახვასა და ანალიზში\n"

                        "• ინვესტორებთან გარიგებების სტრუქტურირებაში\n\n"

                        "მომწერე ჩვეულებრივად."

                    )

                    continue

                # =================================================

                # HELP

                # =================================================

                if command == "/help":

                    send_message(

                        chat_id,

                        "🧠 Geniosa — ბრძანებები\n\n"

                        "/start — დაწყება\n"

                        "/help — დახმარება\n"

                        "/memory — მეხსიერება\n"

                        "/remember ტექსტი — ინფორმაციის შენახვა\n"

                        "/forget — საუბრის მეხსიერების გასუფთავება\n"

                        "/projects — პროექტების ნახვა\n"

                        "/project — პროექტის დამატება\n"

                        "/calc — ფინანსური კალკულატორი"

                    )

                    continue

                # =================================================

                # MEMORY

                # =================================================

                if command == "/memory":

                    message_count = count_messages(

                        chat_id

                    )

                    memory_count = len(

                        get_business_memory(

                            chat_id

                        )

                    )

                    project_count = len(

                        get_projects(

                            chat_id

                        )

                    )

                    send_message(

                        chat_id,

                        "🧠 Geniosa-ს მეხსიერება\n\n"

                        f"საუბრის შეტყობინებები: "

                        f"{message_count}\n"

                        f"ბიზნეს მეხსიერება: "

                        f"{memory_count}\n"

                        f"პროექტები: "

                        f"{project_count}"

                    )

                    continue

                # =================================================

                # REMEMBER

                # =================================================

                if command.startswith(

                    "/remember"

                ):

                    memory = text[

                        len("/remember"):

                    ].strip()

                    if not memory:

                        send_message(

                            chat_id,

                            "გამოყენება:\n"

                            "/remember ტექსტი"

                        )

                        continue

                    save_business_memory(

                        chat_id,

                        memory

                    )

                    send_message(

                        chat_id,

                        "✅ ინფორმაცია გრძელვადიან "

                        "მეხსიერებაში შეინახა."

                    )

                    continue

                # =================================================

                # FORGET

                # =================================================

                if command == "/forget":

                    deleted = delete_memory(

                        chat_id

                    )

                    send_message(

                        chat_id,

                        "🗑 საუბრის მეხსიერება "

                        "გასუფთავებულია.\n"

                        f"წაიშალა {deleted} შეტყობინება.\n\n"

                        "ბიზნეს მეხსიერება და პროექტები "

                        "არ წაშლილა."

                    )

                    continue

                # =================================================

                # PROJECTS

                # =================================================

                if command == "/projects":

                    send_long_message(

                        chat_id,

                        format_projects(

                            chat_id

                        )

                    )

                    continue

                # =================================================

                # PROJECT

                # =================================================

                if command.startswith(

                    "/project"

                ):

                    result = handle_project_command(

                        chat_id,

                        text

                    )

                    send_long_message(

                        chat_id,

                        result

                    )

                    continue

                # =================================================

                # CALCULATOR

                # =================================================

                if command.startswith(

                    "/calc"

                ):

                    result = handle_calc_command(

                        chat_id,

                        text

                    )

                    send_long_message(

                        chat_id,

                        result

                    )

                    continue

                # =================================================

                # NORMAL AI MESSAGE

                # =================================================

                save_message(

                    chat_id,

                    "user",

                    text

                )

                try:

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

                except Exception:

                    logging.exception(

                        "Gemini/message processing error"

                    )

                    send_message(

                        chat_id,

                        "ბოდიში, ტექნიკური პრობლემა მოხდა.\n"

                        "გთხოვ, სცადე თავიდან."

                    )

        except Exception:

            logging.exception(

                "Telegram polling error"

            )

            time.sleep(5)

# =========================================================

# FASTAPI

# =========================================================

@app.get("/")

def home():

    return {

        "status": "Geniosa is running",

        "memory": "PostgreSQL enabled",

        "ai": "Gemini enabled",

        "version": "Geniosa v2.0"

    }

@app.get("/health")

def health():

    return {

        "status": "healthy"

    }

@app.on_event("startup")

def startup_event():

    init_database()

    thread = threading.Thread(

        target=telegram_polling,

        daemon=True

    )

    thread.start()
