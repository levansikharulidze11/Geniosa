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

    "models/gemini-2.5-flash:generateContent"

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

    data = {

        "system_instruction": {

            "parts": [

                {

                    "text": (

                        "You are Geniosa, a professional private business advisor. "

                        "Give practical, clear and concise business advice. "

                        "You can help with investments, construction, real estate, "

                        "business strategy, financial analysis and negotiations. "

                        "Answer in the same language as the user."

                    )

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

    response.raise_for_status()

    result = response.json()

    return result["candidates"][0]["content"]["parts"][0]["text"]

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
