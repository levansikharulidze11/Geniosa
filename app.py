import os

import asyncio

import logging

import requests

from fastapi import FastAPI

from openai import OpenAI

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

ALLOWED_TELEGRAM_USER_ID = os.getenv(

    "ALLOWED_TELEGRAM_USER_ID",

    ""

).strip()

app = FastAPI(title="Geniosa")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

telegram_url = (

    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

)

offset = 0

SYSTEM_INSTRUCTIONS = """

You are Geniosa.

You are a private AI business advisor, economist,

research assistant and strategic thinking partner.

Your main principles:

1. NEVER knowingly invent facts, numbers, laws,

   sources, prices, companies or events.

2. Clearly distinguish between:

   FACT

   ASSUMPTION

   ESTIMATE

   UNKNOWN

3. If information is missing, say what is missing.

4. For financial analysis:

   - show assumptions

   - calculate carefully

   - explain risks

   - provide realistic scenarios

   - include downside cases when useful

   - never promise guaranteed profit

5. For investment projects:

   analyze revenue, costs, financing,

   profitability, cash flow, risks,

   sensitivity and long-term scenarios.

6. For legal and compliance questions:

   identify possible legal, regulatory,

   AML/KYC and fraud risks.

   Do not pretend to be a lawyer.

   Clearly state when professional legal advice

   is required.

7. Be alert to:

   - fraud

   - money laundering

   - suspicious transactions

   - fake investments

   - unrealistic returns

   - conflicts of interest

   - hidden liabilities

8. If the user asks for current information,

   do not pretend that old knowledge is current.

9. Answer in the language used by the user.

   Georgian, Russian and English are supported.

10. Be practical and business-oriented.

11. Do not claim that a function exists if it has

    not actually been connected yet.

Geniosa will gradually receive:

financial analysis,

market research,

legal/compliance analysis,

due diligence,

document analysis,

project management,

specialized AI agents,

web research,

10-year financial modelling,

and autonomous task management.

"""

def telegram_request(method, data):

    response = requests.post(

        f"{telegram_url}/{method}",

        json=data,

        timeout=70

    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):

        raise RuntimeError(

            result.get("description", "Telegram error")

        )

    return result.get("result")

def user_is_allowed(user_id):

    if not ALLOWED_TELEGRAM_USER_ID:

        return True

    return str(user_id) == ALLOWED_TELEGRAM_USER_ID

def ask_geniosa(user_text):

    response = openai_client.responses.create(

        model=OPENAI_MODEL,

        instructions=SYSTEM_INSTRUCTIONS,

        input=user_text

    )

    return response.output_text.strip()

def send_message(chat_id, text):

    if not text:

        text = "ვერ მოვამზადე პასუხი. სცადე კიდევ ერთხელ."

    # Telegram-ის შეტყობინების უსაფრთხო ზომა

    chunk_size = 3900

    for start in range(0, len(text), chunk_size):

        chunk = text[start:start + chunk_size]

        telegram_request(

            "sendMessage",

            {

                "chat_id": chat_id,

                "text": chunk

            }

        )

def process_update(update):

    message = update.get("message")

    if not message:

        return

    user = message.get("from", {})

    chat = message.get("chat", {})

    user_id = user.get("id")

    chat_id = chat.get("id")

    text = message.get("text", "")

    if not user_id or not chat_id:

        return

    if not user_is_allowed(user_id):

        send_message(

            chat_id,

            "წვდომა შეზღუდულია. "

            "ეს არის პირადი Geniosa-ს ბოტი."

        )

        return

    if text.startswith("/start"):

        send_message(

            chat_id,

            """გამარჯობა!

მე ვარ Geniosa — შენი პირადი AI ბიზნეს-ადვაიზერი, ეკონომისტი და კვლევითი ასისტენტი.

შეგიძლია დამისვა ბიზნესის, ფინანსების, ინვესტიციების, პროექტების, ბაზრის ანალიზის და სტრატეგიის შესახებ კითხვები.

პირველი MVP უკვე მზად არის.

შემდეგ ეტაპებზე დავამატებთ:

• ფინანსურ მოდელებს

• ბაზრის კვლევას

• Due Diligence-ს

• Legal & Compliance-ს

• დოკუმენტების ანალიზს

• პროექტების მენეჯმენტს

• სპეციალიზებულ AI აგენტებს

• 10-წლიან ფინანსურ პროგნოზებს

• ავტონომიურ ამოცანებს

მომწერე პირველი დავალება."""

        )

        return

    if text.startswith("/help"):

        send_message(

            chat_id,

            """Geniosa-ს ძირითადი ბრძანებები:

/start — დაწყება

/help — დახმარება

/project — პროექტის ანალიზი

/financial — ფინანსური ანალიზი

/research — ბაზრის კვლევა

/legal — Legal & Compliance

/duediligence — Due Diligence

/document — დოკუმენტის ანალიზი

/learn — ახალი თემის სწავლა

/tasks — ამოცანები

/agents — AI აგენტები

/report — პროფესიული ანგარიში

ან უბრალოდ მომწერე ჩვეულებრივი ტექსტით."""

        )

        return

    if not text.strip():

        send_message(

            chat_id,

            "ამ ეტაპზე ტექსტური შეტყობინებებია ჩართული."

        )

        return

    try:

        answer = ask_geniosa(text)

        send_message(

            chat_id,

            answer

        )

    except Exception as error:

        logging.exception(

            "Geniosa error: %s",

            error

        )

        send_message(

            chat_id,

            "ტექნიკური შეცდომა მოხდა. "

            "სცადე რამდენიმე წამში კიდევ ერთხელ."

        )

def telegram_poll():

    global offset

    result = telegram_request(

        "getUpdates",

        {

            "timeout": 50,

            "offset": offset,

            "allowed_updates": ["message"]

        }

    )

    for update in result:

        offset = max(

            offset,

            update["update_id"] + 1

        )

        process_update(update)

async def telegram_loop():

    logging.info(

        "Geniosa Telegram bot started."

    )

    while True:

        try:

            await asyncio.to_thread(

                telegram_poll

            )

        except Exception:

            logging.exception(

                "Telegram polling error"

            )

            await asyncio.sleep(3)

@app.on_event("startup")

async def startup():

    asyncio.create_task(

        telegram_loop()

    )

@app.get("/")

def health():

    return {

        "status": "ok",

        "service": "Geniosa"
        }

    
