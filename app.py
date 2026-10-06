import os

import time

import logging

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

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

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

        cursor.execute("""

            CREATE TABLE IF NOT EXISTS messages (

                id SERIAL PRIMARY KEY,

                chat_id BIGINT NOT NULL,

                role VARCHAR(20) NOT NULL,

                message TEXT NOT NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

            )

        """)

        connection.commit()

        cursor.close()

        logging.info("PostgreSQL database initialized successfully.")

    except Exception:

        logging.exception("Database initialization error")

    finally:

        if connection:

            connection.close()

def save_message(chat_id, role, message):

    connection = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()

        cursor.execute(

            """

            INSERT INTO messages (chat_id, role, message)

            VALUES (%s, %s, %s)

            """,

            (chat_id, role, message)

        )

        connection.commit()

        cursor.close()

    except Exception:

        logging.exception("Could not save message to database")

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

            (chat_id, limit)

        )

        rows = cursor.fetchall()

        cursor.close()

        rows.reverse()

        return rows

    except Exception:

        logging.exception("Could not read messages from database")

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

        logging.exception("Could not count messages")

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

        logging.exception("Could not delete memory")

        return 0

    finally:

        if connection:

            connection.close()

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

    response.raise_for_status()

# =========================================================

# GEMINI

# =========================================================

def ask_gemini(user_text, chat_id):

    recent_messages = get_recent_messages(chat_id, limit=20)

    conversation_parts = []

    for role, message in recent_messages:

        if role == "user":

            conversation_parts.append(

                f"User: {message}"

            )

        elif role == "assistant":

            conversation_parts.append(

                f"Geniosa: {message}"

            )

    conversation_context = "\n".join(conversation_parts)

    system_instruction = """

You are Geniosa — a professional private business and investment advisor.

Your main areas of expertise are:

1. Business strategy and management

2. Investments and investor relations

3. Construction and real estate development

4. Real estate market analysis

5. Financial analysis and financial models

6. Project profitability and investment returns

7. Negotiations and deal structuring

8. Business proposals and investor presentations

9. Risk analysis and due diligence

10. Market research

IMPORTANT RULES:

- Give practical and actionable answers.

- Do not give vague generic advice when a practical answer is possible.

- Think like an experienced investor, developer and business consultant.

- When analyzing a project, consider revenue, costs, profit, cash flow, ROI, IRR, payback period and risks when relevant.

- Clearly separate facts, assumptions and estimates.

- Never invent financial figures, companies, investors, market data or legal facts.

- If important information is missing, ask the user for it.

- When calculating financial figures, show the calculation clearly.

- When evaluating an investment, explain both advantages and risks.

- When discussing a proposed deal, identify potential problems and suggest better structures when appropriate.

- Be direct and honest. If the user's idea has weaknesses, explain them clearly and propose a solution.

- Always prioritize the user's business interests.

- Answer in the same language as the user.

- If the user writes in Georgian, answer in Georgian.

- If the user writes in Russian, answer in Russian.

- If the user writes in English, answer in English.

You are not simply a chatbot.

You are a long-term business advisor.

Use the conversation history provided to maintain continuity.

Important:

The conversation history may contain assumptions or old information.

Do not blindly treat old assumptions as current facts if the user changes them.

"""

    full_prompt = f"""

Previous conversation:

{conversation_context}

Current user message:

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

    candidates = result.get("candidates", [])

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

    )

# =========================================================

# TELEGRAM POLLING

# =========================================================

def telegram_polling():

    logging.info(

        "Starting Telegram polling..."

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

            # Telegram 409 means another polling connection

            # temporarily exists.

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

                offset = update["update_id"] + 1

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

                # -------------------------

                # MEMORY COMMAND

                # -------------------------

                if text.strip().lower() == "/memory":

                    count = count_messages(

                        chat_id

                    )

                    send_message(

                        chat_id,

                        f"🧠 Geniosa-ს მეხსიერებაში "

                        f"შენახულია {count} შეტყობინება."

                    )

                    continue

                # -------------------------

                # FORGET COMMAND

                # -------------------------

                if text.strip().lower() == "/forget":

                    deleted = delete_memory(

                        chat_id

                    )

                    send_message(

                        chat_id,

                        f"🗑 მეხსიერება გასუფთავებულია.\n"

                        f"წაიშალა {deleted} შეტყობინება."

                    )

                    continue

                # -------------------------

                # SAVE USER MESSAGE

                # -------------------------

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

                    send_message(

                        chat_id,

                        answer

                    )

                except Exception:

                    logging.exception(

                        "Gemini/message processing error"

                    )

                    send_message(

                        chat_id,

                        "ბოდიში, ტექნიკური პრობლემა მოხდა. "

                        "გთხოვთ, სცადოთ თავიდან."

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

        "memory": "PostgreSQL enabled"

    }

@app.on_event("startup")

def startup_event():

    # Create database table

    init_database()

    # Start Telegram polling

    import threading

    thread = threading.Thread(

        target=telegram_polling,

        daemon=True

    )

    thread.start()
