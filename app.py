import os

import logging

import requests

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

if not GEMINI_API_KEY:

    raise RuntimeError("GEMINI_API_KEY is not set")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

GEMINI_API = (

    "https://generativelanguage.googleapis.com/v1beta/"

    "models/gemini-3.5-flash-lite:generateContent"

)

@app.get("/")

def home():

    return {"status": "Geniosa is running"}

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

def ask_gemini(user_text):

    headers = {

        "Content-Type": "application/json",

        "x-goog-api-key": GEMINI_API_KEY

    }

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

The user is building a professional business assistant called Geniosa.

Geniosa should behave as a long-term business advisor rather than a simple chatbot.

"""

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

                        "text": user_text

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

        raise RuntimeError("Gemini returned no candidates")

    parts = candidates[0].get("content", {}).get("parts", [])

    if not parts:

        raise RuntimeError("Gemini returned no text")

    return parts[0].get(

        "text",

        "ბოდიში, პასუხი ვერ მივიღე."
    )

def telegram_polling():

    logging.info("Starting Telegram polling...")

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

            response.raise_for_status()

            data = response.json()

            for update in data.get("result", []):

                offset = update["update_id"] + 1

                message = update.get("message")

                if not message:

                    continue

                chat = message.get("chat")

                text = message.get("text")

                if not chat or not text:

                    continue

                chat_id = chat["id"]

                logging.info(

                    "Message received from %s: %s",

                    chat_id,

                    text

                )

                try:

                    answer = ask_gemini(text)

                    send_message(chat_id, answer)

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

            logging.exception("Telegram polling error")

@app.on_event("startup")

def startup_event():

    import threading

    thread = threading.Thread(

        target=telegram_polling,

        daemon=True

    )

    thread.start()
