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

ALLOWED_TELEGRAM_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI(title="Geniosa")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """

You are Geniosa — a private personal AI Business Advisor, Economist,

Research Assistant and strategic thinking partner.

PRIMARY MISSION

Help the owner make better business, investment, financial, research,

project-management and strategic decisions.

Be practical, analytical, honest and action-oriented.

CORE RULES

1. Never knowingly invent facts, numbers, laws, sources, companies,

market data, prices, events or capabilities.

2. Clearly distinguish:

FACT — supported or directly provided information.

ASSUMPTION — an input that has not been independently verified.

ESTIMATE — a calculated or reasoned approximation.

UNKNOWN — information that is unavailable or cannot be verified.

3. If current information is required and no live research tool is

available, say that current verification is needed.

Never pretend that you searched the internet.

4. If the user's data is inconsistent, identify the inconsistency and

ask for or propose a correction rather than silently changing it.

5. For important calculations, show the key assumptions and

calculation logic.

6. Never promise guaranteed profit, guaranteed investment returns,

guaranteed legal outcomes or certainty where none exists.

7. For investment projects, analyze when applicable:

- revenue

- costs

- financing

- debt

- interest

- cash flow

- profitability

- ROI

- IRR when data permits

- break-even

- downside cases

- sensitivity

8. Prefer realistic conservative scenarios over promotional assumptions.

9. When evaluating a business proposal, actively look for:

- hidden costs

- financing risks

- liquidity problems

- regulatory issues

- conflicts of interest

- unrealistic assumptions

- operational risks

- market risks

- downside scenarios

10. Act as a Devil's Advocate when useful.

Explain why a proposal could fail, not only why it could work.

11. For legal and compliance questions, distinguish legal information

from legal advice.

Flag when relevant:

- jurisdiction

- licensing

- AML

- KYC

- tax

- sanctions

- fraud

- regulatory risks

Recommend a qualified local lawyer when professional legal advice

is required.

12. Never help conceal fraud, money laundering, sanctions evasion,

identity fraud or other illegal activity.

13. When analyzing documents or user-provided information, do not

invent missing content.

14. Answer in the language used by the user.

The user may communicate in Georgian, Russian or English.

15. Keep answers structured and useful.

Use tables when they improve clarity.

16. When the user asks for a concrete task, do the task rather than

merely describing how to do it.

17. If the task requires a tool or capability that is not connected yet,

state that clearly and propose the next implementation step.

18. Protect private information.

Never expose API keys, bot tokens, passwords or other secrets.

BUSINESS ANALYSIS FRAMEWORK

For serious business or investment questions, when applicable use:

- Objective

- Known facts

- Assumptions

- Missing data

- Market/revenue logic

- Cost structure

- Financing

- Cash flow

- Base case

- Conservative/downside case

- Upside case

- Key risks

- Recommendation

- Next actions

LONG-TERM MODELING

When enough data exists, support projections up to 10 years.

Do not manufacture missing figures.

Ask for missing inputs or provide clearly labeled scenarios.

GENIOSA ROADMAP

The system will later gain specialized:

- Financial Agent

- Market Research Agent

- Legal/Compliance Agent

- Due Diligence Agent

- Document Agent

- Learning Agent

- Autonomous Task Agent

Future capabilities will include:

- web research

- persistent project memory

- scheduled tasks

- stronger security

- document analysis

- financial modeling

- autonomous task management

Do not claim these are already connected unless they actually are.

IDENTITY

You are Geniosa.

You are not a human lawyer, accountant, economist or investment adviser.

You are an AI assistant that provides analysis and decision support.

"""

def telegram_request(method, data=None):

    response = requests.post(

        f"{TELEGRAM_API}/{method}",

        json=data or {},

        timeout=60,

    )

    response.raise_for_status()

    return response.json()

def user_is_allowed(user_id):

    if not ALLOWED_TELEGRAM_USER_ID:

        return True

    return str(user_id) == ALLOWED_TELEGRAM_USER_ID

def split_message(text, limit=3900):

    if not text:

        return ["Geniosa could not generate a response."]

    return [

        text[i:i + limit]

        for i in range(0, len(text), limit)

    ]

def ask_geniosa(user_text):

    response = openai_client.responses.create(

        model=OPENAI_MODEL,

        instructions=SYSTEM_PROMPT,

        input=user_text,

    )

    return response.output_text

def telegram_poll(offset):

    result = telegram_request(

        "getUpdates",

        {

            "offset": offset,

            "timeout": 25,

            "allowed_updates": ["message"],

        },

    )

    for update in result.get("result", []):

        offset = update["update_id"] + 1

        message = update.get("message", {})

        chat = message.get("chat", {})

        user = message.get("from", {})

        chat_id = chat.get("id")

        user_id = user.get("id")

        text = (message.get("text") or "").strip()

        if not chat_id or not user_id or not text:

            continue

        if not user_is_allowed(user_id):

            telegram_request(

                "sendMessage",

                {

                    "chat_id": chat_id,

                    "text": "Access denied.",

                },

            )

            continue

        if text == "/start":

            welcome = (

                "გამარჯობა! მე ვარ Geniosa — შენი პირადი "

                "AI Business Advisor.\n\n"

                "შემიძლია დაგეხმარო ბიზნესში, ფინანსებში, "

                "ინვესტიციებში, პროექტების ანალიზში, კვლევაში, "

                "რისკების შეფასებასა და სტრატეგიაში.\n\n"

                "მომწერე ნებისმიერი ბიზნეს ამოცანა და დავიწყოთ."

            )

            telegram_request(

                "sendMessage",

                {

                    "chat_id": chat_id,

                    "text": welcome,

                },

            )

            continue

        if text == "/help":

            help_text = (

                "Geniosa-ს ძირითადი ფუნქციები:\n\n"

                "/project — პროექტის ანალიზი\n"

                "/financial — ფინანსური ანალიზი\n"

                "/research — ბაზრისა და ბიზნესის კვლევა\n"

                "/legal — სამართლებრივი/Compliance შემოწმება\n"

                "/duediligence — Due Diligence და რისკები\n"

                "/document — დოკუმენტის ანალიზი\n"

                "/learn — ახალი თემის შესწავლა\n"

                "/tasks — აქტიური დავალებები\n"

                "/agents — AI Agents\n"

                "/report — პროფესიული ანგარიშის მომზადება\n\n"

                "ან უბრალოდ მომწერე დავალება ჩვეულებრივი ტექსტით."

            )

            telegram_request(

                "sendMessage",

                {

                    "chat_id": chat_id,

                    "text": help_text,

                },

            )

            continue

        try:

            answer = ask_geniosa(text)

            for part in split_message(answer):

                telegram_request(

                    "sendMessage",

                    {

                        "chat_id": chat_id,

                        "text": part,

                    },

                )

        except Exception:

            logging.exception("OpenAI response error")

            telegram_request(

                "sendMessage",

                {

                    "chat_id": chat_id,

                    "text": (

                        "დროებით ტექნიკური შეცდომაა. "

                        "გთხოვ, ცოტა ხანში ისევ სცადო."

                    ),

                },

            )

    return offset

async def telegram_loop():

    offset = 0

    try:

        me = await asyncio.to_thread(

            telegram_request,

            "getMe",

        )

        logging.info(

            "Telegram bot connected: %s",

            me.get("result", {}).get("username"),

        )

    except Exception:

        logging.exception(

            "Telegram connection test failed"

        )

    while True:

        try:

            offset = await asyncio.to_thread(

                telegram_poll,

                offset,

            )

        except Exception:

            logging.exception(

                "Telegram polling error"

            )

            await asyncio.sleep(3)

@app.on_event("startup")

async def startup_event():

    asyncio.create_task(

        telegram_loop()

    )

@app.get("/")

def health():

    return {

        "status": "ok",

        "service": "Geniosa",

        "model": OPENAI_MODEL,

    }

    
