import os

import re

import json

import time

import logging

import threading

from pathlib import Path

from datetime import datetime

import requests

import psycopg2

from psycopg2.extras import RealDictCursor

from fastapi import FastAPI

from openpyxl import Workbook, load_workbook

from pptx import Presentation

from pptx.util import Inches, Pt

from pypdf import PdfReader

from docx import Document

# ============================================================

# GENIOSA 4.0

# Universal Business & Investment Intelligence Platform

# ============================================================

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s %(levelname)s %(message)s"

)

# ============================================================

# ENVIRONMENT

# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN:

    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

if not GEMINI_API_KEY:

    raise RuntimeError("GEMINI_API_KEY is missing")

if not DATABASE_URL:

    raise RuntimeError("DATABASE_URL is missing")

# ============================================================

# TELEGRAM

# ============================================================

TELEGRAM_API = (

    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

)

# ============================================================

# GEMINI

# ============================================================

GEMINI_MODEL = os.getenv(

    "GEMINI_MODEL",

    "gemini-3.5-flash-lite"

)

GEMINI_URL = (

    "https://generativelanguage.googleapis.com/"

    f"v1beta/models/{GEMINI_MODEL}:generateContent"

)
def telegram_url(method):

    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
# ============================================================
def send_message(chat_id, text):

    try:

        response = requests.post(

            telegram_url("sendMessage"),

            json={

                "chat_id": chat_id,

                "text": text

            },

            timeout=30

        )

        return response.json()

    except Exception:

        logging.exception("send_message error")

        return None
# APPLICATION

# ============================================================

app = FastAPI(

    title="Geniosa",

    version="4.0"

)

# ============================================================

# FILE STORAGE

# ============================================================

DOWNLOAD_DIR = Path("/tmp/geniosa")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_TELEGRAM_MESSAGE = 3900

# ============================================================

# OPTIONAL OWNER SECURITY

# ============================================================

OWNER_ID = os.getenv(

    "GENIOSA_OWNER_ID",

    ""

).strip()

def user_allowed(chat_id):

    """

    If GENIOSA_OWNER_ID is configured,

    only that Telegram user can use Geniosa.

    If it is empty, the bot remains open.

    """

    if not OWNER_ID:

        return True

    return str(chat_id) == OWNER_ID

# ============================================================

# DATABASE CONNECTION

# ============================================================

def db():

    return psycopg2.connect(

        DATABASE_URL,

        cursor_factory=RealDictCursor

    )

# ============================================================

# DATABASE INITIALIZATION

# ============================================================

def init_db():

    conn = db()

    cur = conn.cursor()

    # --------------------------------------------------------

    # Conversation history

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS messages (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            role TEXT,

            text TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Long-term business memory

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS business_memory (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            memory TEXT,

            category TEXT DEFAULT 'general',

            importance INTEGER DEFAULT 5,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Universal projects

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS projects (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            name TEXT NOT NULL,

            industry TEXT DEFAULT 'other',

            location TEXT,

            description TEXT,

            land_area DOUBLE PRECISION,

            saleable_area DOUBLE PRECISION,

            construction_area DOUBLE PRECISION,

            total_area DOUBLE PRECISION,

            revenue DOUBLE PRECISION,

            total_cost DOUBLE PRECISION,

            operating_cost DOUBLE PRECISION,

            net_profit DOUBLE PRECISION,

            investor_capital DOUBLE PRECISION,

            investor_profit DOUBLE PRECISION,

            investor_share DOUBLE PRECISION,

            status TEXT DEFAULT 'active',

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Documents

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS documents (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            project_id INTEGER,

            filename TEXT,

            file_type TEXT,

            extracted_text TEXT,

            analysis TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Investors

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS investors (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            name TEXT NOT NULL,

            company TEXT,

            country TEXT,

            contact TEXT,

            investment_capacity DOUBLE PRECISION,

            preferred_sector TEXT,

            status TEXT DEFAULT 'new',

            notes TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Investment deals / CRM

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS deals (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            project_id INTEGER,

            investor_id INTEGER,

            stage TEXT DEFAULT 'new',

            proposed_amount DOUBLE PRECISION,

            proposed_share DOUBLE PRECISION,

            valuation DOUBLE PRECISION,

            notes TEXT,

            next_step TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Market research

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS research (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            project_id INTEGER,

            query TEXT,

            result TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Financial analyses

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS financial_analyses (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            project_id INTEGER,

            analysis_type TEXT,

            input_data TEXT,

            result_data TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Assets generated by Geniosa

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS generated_assets (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            project_id INTEGER,

            asset_type TEXT,

            filename TEXT,

            description TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Investment instruments

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS securities (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            symbol TEXT,

            name TEXT,

            asset_type TEXT,

            exchange TEXT,

            currency TEXT,

            quantity DOUBLE PRECISION,

            average_price DOUBLE PRECISION,

            notes TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    # --------------------------------------------------------

    # Crypto assets

    # --------------------------------------------------------

    cur.execute("""

        CREATE TABLE IF NOT EXISTS crypto_assets (

            id SERIAL PRIMARY KEY,

            chat_id BIGINT,

            symbol TEXT,

            name TEXT,

            quantity DOUBLE PRECISION,

            average_price DOUBLE PRECISION,

            wallet TEXT,

            notes TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )

    """)

    conn.commit()

    cur.close()

    conn.close()

    logging.info(

        "Geniosa 4.0 PostgreSQL database initialized successfully."

    )

# ============================================================

# MESSAGE FUNCTIONS

# ============================================================

def save_message(chat_id, role, text):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO messages

        (chat_id, role, text)

        VALUES (%s, %s, %s)

        """,

        (chat_id, role, text)

    )

    conn.commit()

    cur.close()

    conn.close()

def get_messages(chat_id, limit=20):

    conn = db()

    cur = conn.cursor()

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

    return list(reversed(rows))

# ============================================================

# BUSINESS MEMORY

# ============================================================

def save_memory(

    chat_id,

    memory,

    category="general",

    importance=5

):
def save_memory(

    chat_id,

    memory,

    category="general",

    importance=5

):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO business_memory (

            chat_id,

            memory,

            category,

            importance

        )

        VALUES (%s, %s, %s, %s)

        RETURNING *

        """,

        (

            chat_id,

            memory,

            category,

            importance

        )

    )

    result = cur.fetchone()

    conn.commit()

    cur.close()

    conn.close()

    return result

def get_memories(chat_id, limit=20):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        SELECT *

        FROM business_memory

        WHERE chat_id = %s

        ORDER BY importance DESC, updated_at DESC, id DESC

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

def delete_project(chat_id, project_id):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        UPDATE projects

        SET

            status = 'deleted',

            updated_at = CURRENT_TIMESTAMP

        WHERE chat_id = %s

        AND id = %s

        """,

        (chat_id, project_id)

    )

    deleted = cur.rowcount

    conn.commit()

    cur.close()

    conn.close()

    return deleted

# ============================================================

# INVESTOR FUNCTIONS

# ============================================================

def create_investor(

    chat_id,

    name,

    company=None,

    country=None,

    contact=None,

    investment_capacity=None,

    preferred_sector=None,

    status="new",

    notes=None

):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO investors (

            chat_id,

            name,

            company,

            country,

            contact,

            investment_capacity,

            preferred_sector,

            status,

            notes

        )

        VALUES (

            %s, %s, %s, %s, %s,

            %s, %s, %s, %s

        )

        RETURNING *

        """,

        (

            chat_id,

            name,

            company,

            country,

            contact,

            investment_capacity,

            preferred_sector,

            status,

            notes

        )

    )

    investor = cur.fetchone()

    conn.commit()

    cur.close()

    conn.close()

    return investor

def get_investors(chat_id):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        SELECT *

        FROM investors

        WHERE chat_id = %s

        ORDER BY updated_at DESC, id DESC

        """,

        (chat_id,)

    )

    rows = cur.fetchall()

    cur.close()

    conn.close()

    return rows

# ============================================================

# DEAL / CRM FUNCTIONS

# ============================================================

def create_deal(

    chat_id,

    project_id=None,

    investor_id=None,

    stage="new",

    proposed_amount=None,

    proposed_share=None,

    valuation=None,

    notes=None,

    next_step=None

):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO deals (

            chat_id,

            project_id,

            investor_id,

            stage,

            proposed_amount,

            proposed_share,

            valuation,

            notes,

            next_step

        )

        VALUES (

            %s, %s, %s, %s, %s,

            %s, %s, %s, %s

        )

        RETURNING *

        """,

        (

            chat_id,

            project_id,

            investor_id,

            stage,

            proposed_amount,

            proposed_share,

            valuation,

            notes,

            next_step

        )

    )

    deal = cur.fetchone()

    conn.commit()

    cur.close()

    conn.close()

    return deal

def get_deals(chat_id):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        SELECT

            deals.*,

            projects.name AS project_name,

            investors.name AS investor_name

        FROM deals

        LEFT JOIN projects

            ON deals.project_id = projects.id

        LEFT JOIN investors

            ON deals.investor_id = investors.id

        WHERE deals.chat_id = %s

        ORDER BY deals.updated_at DESC, deals.id DESC

        """,

        (chat_id,)

    )

    rows = cur.fetchall()

    cur.close()

    conn.close()

    return rows# ============================================================

# FINANCIAL INTELLIGENCE ENGINE

# ============================================================

def safe_float(value, default=0.0):

    try:

        if value is None:

            return default

        if isinstance(value, (int, float)):

            return float(value)

        value = str(value).replace(",", "").strip()

        if not value:

            return default

        return float(value)

    except Exception:

        return default

def calculate_roi(investment, profit):

    investment = safe_float(investment)

    profit = safe_float(profit)

    if investment == 0:

        return None

    return (profit / investment) * 100

def calculate_margin(revenue, profit):

    revenue = safe_float(revenue)

    profit = safe_float(profit)

    if revenue == 0:

        return None

    return (profit / revenue) * 100

def calculate_payback(investment, annual_cash_flow):

    investment = safe_float(investment)

    annual_cash_flow = safe_float(annual_cash_flow)

    if annual_cash_flow <= 0:

        return None

    return investment / annual_cash_flow

def calculate_break_even(

    fixed_cost,

    selling_price,

    variable_cost

):

    fixed_cost = safe_float(fixed_cost)

    selling_price = safe_float(selling_price)

    variable_cost = safe_float(variable_cost)

    contribution = selling_price - variable_cost

    if contribution <= 0:

        return None

    return fixed_cost / contribution

def calculate_npv(

    initial_investment,

    cash_flows,

    discount_rate

):

    initial_investment = safe_float(

        initial_investment

    )

    discount_rate = safe_float(

        discount_rate

    ) / 100

    if not isinstance(cash_flows, list):

        return None

    npv = -initial_investment

    for period, cash_flow in enumerate(

        cash_flows,

        start=1

    ):

        cash_flow = safe_float(

            cash_flow

        )

        npv += (

            cash_flow /

            ((1 + discount_rate) ** period)

        )

    return npv

def calculate_irr(

    initial_investment,

    cash_flows

):

    initial_investment = safe_float(

        initial_investment

    )

    if not isinstance(cash_flows, list):

        return None

    flows = [

        -initial_investment

    ]

    flows.extend(

        safe_float(x)

        for x in cash_flows

    )

    # --------------------------------------------------------

    # Newton-Raphson approximation

    # --------------------------------------------------------

    rate = 0.10

    for _ in range(100):

        npv = flows[0]

        derivative = 0.0

        for period, cash_flow in enumerate(

            flows[1:],

            start=1

        ):

            denominator = (

                (1 + rate) ** period

            )

            npv += (

                cash_flow /

                denominator

            )

            derivative -= (

                period *

                cash_flow /

                ((1 + rate) ** (period + 1))

            )

        if abs(derivative) < 1e-12:

            break

        new_rate = (

            rate -

            (npv / derivative)

        )

        if abs(new_rate - rate) < 1e-7:

            rate = new_rate

            break

        rate = new_rate

    if rate <= -0.9999:

        return None

    return rate * 100

def calculate_ebitda(

    revenue,

    cogs,

    operating_expenses

):

    revenue = safe_float(revenue)

    cogs = safe_float(cogs)

    operating_expenses = safe_float(

        operating_expenses

    )

    return (

        revenue -

        cogs -

        operating_expenses

    )

def calculate_gross_profit(

    revenue,

    cogs

):

    revenue = safe_float(revenue)

    cogs = safe_float(cogs)

    return revenue - cogs

def calculate_net_profit(

    revenue,

    total_cost

):

    revenue = safe_float(revenue)

    total_cost = safe_float(total_cost)

    return revenue - total_cost

def calculate_debt_service(

    principal,

    annual_interest_rate,

    years

):

    principal = safe_float(principal)

    annual_interest_rate = (

        safe_float(annual_interest_rate)

        / 100

    )

    years = safe_float(years)

    if principal <= 0:

        return 0

    if years <= 0:

        return None

    periods = years * 12

    monthly_rate = (

        annual_interest_rate / 12

    )

    if monthly_rate == 0:

        return principal / periods

    payment = (

        principal *

        monthly_rate *

        ((1 + monthly_rate) ** periods)

        /

        (

            ((1 + monthly_rate) ** periods)

            - 1

        )

    )

    return payment

def calculate_dscr(

    operating_cash_flow,

    debt_service

):

    operating_cash_flow = safe_float(

        operating_cash_flow

    )

    debt_service = safe_float(

        debt_service

    )

    if debt_service <= 0:

        return None

    return (

        operating_cash_flow /

        debt_service

    )

# ============================================================

# SCENARIO ANALYSIS

# ============================================================

def scenario_analysis(

    revenue,

    cost,

    scenarios=None

):

    revenue = safe_float(revenue)

    cost = safe_float(cost)

    if scenarios is None:

        scenarios = {

            "conservative": {

                "revenue_factor": 0.85,

                "cost_factor": 1.10

            },

            "base": {

                "revenue_factor": 1.00,

                "cost_factor": 1.00

            },

            "optimistic": {

                "revenue_factor": 1.15,

                "cost_factor": 0.95

            }

        }

    results = {}

    for name, settings in scenarios.items():

        revenue_factor = safe_float(

            settings.get(

                "revenue_factor",

                1

            )

        )

        cost_factor = safe_float(

            settings.get(

                "cost_factor",

                1

            )

        )

        scenario_revenue = (

            revenue *

            revenue_factor

        )

        scenario_cost = (

            cost *

            cost_factor

        )

        scenario_profit = (

            scenario_revenue -

            scenario_cost

        )

        margin = calculate_margin(

            scenario_revenue,

            scenario_profit

        )

        results[name] = {

            "revenue": scenario_revenue,

            "cost": scenario_cost,

            "profit": scenario_profit,

            "margin": margin

        }

    return results

# ============================================================

# FINANCIAL ANALYSIS STORAGE

# ============================================================

def save_financial_analysis(

    chat_id,

    project_id,

    analysis_type,

    input_data,

    result_data

):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """

        INSERT INTO financial_analyses (

            chat_id,

            project_id,

            analysis_type,

            input_data,

            result_data

        )

        VALUES (

            %s, %s, %s, %s, %s

        )

        RETURNING *

        """,

        (

            chat_id,

            project_id,

            analysis_type,

            json.dumps(

                input_data,

                ensure_ascii=False

            ),

            json.dumps(

                result_data,

                ensure_ascii=False

            )

        )

    )

    result = cur.fetchone()

    conn.commit()

    cur.close()

    conn.close()

    return result

# ============================================================

# FINANCIAL PROJECT SNAPSHOT

# ============================================================

def build_project_financial_snapshot(project):

    if not project:

        return None

    revenue = safe_float(

        project.get("revenue")

    )

    total_cost = safe_float(

        project.get("total_cost")

    )

    operating_cost = safe_float(

        project.get("operating_cost")

    )

    net_profit = safe_float(

        project.get("net_profit")

    )

    investor_capital = safe_float(

        project.get("investor_capital")

    )

    investor_profit = safe_float(

        project.get("investor_profit")

    )

    snapshot = {

        "revenue": revenue,

        "total_cost": total_cost,

        "operating_cost": operating_cost,

        "net_profit": net_profit,

        "gross_margin": (

            calculate_margin(

                revenue,

                revenue - total_cost

            )

            if revenue

            else None

        ),

        "net_margin": (

            calculate_margin(

                revenue,

                net_profit

            )

            if revenue

            else None

        ),

        "investor_capital": investor_capital,

        "investor_profit": investor_profit,

        "investor_roi": (

            calculate_roi(

                investor_capital,

                investor_profit

            )

            if investor_capital

            else None

        )

    }

    return snapshot

# ============================================================

# FORMAT MONEY

# ============================================================

def money(value):

    if value is None:

        return "N/A"

    value = safe_float(value)

    return f"${value:,.2f}"

def percent(value):

    if value is None:

        return "N/A"

    return f"{safe_float(value):.2f}%"

def number(value):

    if value is None:

        return "N/A"

    return f"{safe_float(value):,.2f}"# ============================================================

# GENIOSA 4.0 — PART 4

# PROJECT / INVESTOR / CRM HELPERS

# ============================================================

DEAL_STAGES = [

    "New",

    "Contacted",

    "Interested",

    "NDA",

    "Documents",

    "Due Diligence",

    "Offer",

    "Negotiation",

    "Closed"

]

def format_project(project):

    if not project:

        return "პროექტი ვერ მოიძებნა."

    return (

        f"📌 {project.get('name', '-')}\n"

        f"🏭 ინდუსტრია: {industry_name(project.get('industry'))}\n"

        f"📍 მდებარეობა: {project.get('location') or '-'}\n"

        f"🌍 მიწა: {number(project.get('land_area'))} მ²\n"

        f"🏢 გასაყიდი: {number(project.get('saleable_area'))} მ²\n"

        f"🏗 მშენებლობა: {number(project.get('construction_area'))} მ²\n"

        f"📐 სრული ფართობი: {number(project.get('total_area'))} მ²\n"

        f"💰 შემოსავალი: {money(project.get('revenue'))}\n"

        f"💸 სრული ხარჯი: {money(project.get('total_cost'))}\n"

        f"📈 წმინდა მოგება: {money(project.get('net_profit'))}\n"

        f"👤 ინვესტორის კაპიტალი: {money(project.get('investor_capital'))}\n"

        f"🤝 ინვესტორის წილი: {percent(project.get('investor_share'))}\n"

        f"📊 სტატუსი: {project.get('status') or 'active'}"

    )

def format_investor(investor):

    if not investor:

        return "ინვესტორი ვერ მოიძებნა."

    return (

        f"👤 {investor.get('name', '-')}\n"

        f"🏢 კომპანია: {investor.get('company') or '-'}\n"

        f"🌍 ქვეყანა: {investor.get('country') or '-'}\n"

        f"📧 Email: {investor.get('email') or '-'}\n"

        f"📞 ტელეფონი: {investor.get('phone') or '-'}\n"

        f"🏭 სექტორი: {investor.get('sector') or '-'}\n"

        f"💰 საინვესტიციო შესაძლებლობა: "

        f"{money(investor.get('investment_capacity'))}\n"

        f"📌 სტატუსი: {investor.get('status') or 'New'}\n"

        f"📝 შენიშვნა: {investor.get('notes') or '-'}"

    )

def format_deal(deal):

    if not deal:

        return "გარიგება ვერ მოიძებნა."

    return (

        f"🤝 გარიგება #{deal.get('id')}\n"

        f"👤 ინვესტორი: {deal.get('investor_name') or '-'}\n"

        f"📌 პროექტი: {deal.get('project_name') or '-'}\n"

        f"📊 ეტაპი: {deal.get('stage') or 'New'}\n"

        f"💰 თანხა: {money(deal.get('amount'))}\n"

        f"📅 შემდეგი ნაბიჯი: {deal.get('next_action') or '-'}\n"

        f"📝 შენიშვნა: {deal.get('notes') or '-'}"

    )

def project_summary(chat_id):

    projects = get_projects(chat_id)

    if not projects:

        return "📂 შენახული პროექტები ჯერ არ არის."

    lines = ["📂 შენი პროექტები:\n"]

    for i, project in enumerate(projects, 1):

        lines.append(

            f"{i}. {project.get('name')}\n"

            f"   🏭 {industry_name(project.get('industry'))}\n"

            f"   📍 {project.get('location') or '-'}\n"

            f"   📈 მოგება: {money(project.get('net_profit'))}\n"

        )

    return "\n".join(lines)

def investor_summary(chat_id):

    investors = get_investors(chat_id)

    if not investors:

        return "👤 ინვესტორების ბაზა ჯერ ცარიელია."

    lines = ["👤 ინვესტორები:\n"]

    for i, investor in enumerate(investors, 1):

        lines.append(

            f"{i}. {investor.get('name')}\n"

            f"   🏢 {investor.get('company') or '-'}\n"

            f"   🌍 {investor.get('country') or '-'}\n"

            f"   📊 {investor.get('status') or 'New'}\n"

        )

    return "\n".join(lines)

def deal_summary(chat_id):

    deals = get_deals(chat_id)

    if not deals:

        return "🤝 გარიგებების pipeline ჯერ ცარიელია."

    lines = ["🤝 Investor Deal Pipeline:\n"]

    for deal in deals:

        lines.append(

            f"#{deal.get('id')} — "

            f"{deal.get('investor_name') or '-'} → "

            f"{deal.get('project_name') or '-'}\n"

            f"   📊 {deal.get('stage') or 'New'}\n"

            f"   💰 {money(deal.get('amount'))}\n"

        )

პ

• ხარჯები

• მოგება

• მარჟა

• ROI

• IRR

• NPV

• Cash Flow

• ვალი

• რისკები

• მნიშვნელოვანი დაშვებები

თუ დოკუმენტი საინვესტიციოა:

• Investment Opportunity

• Capital Required

• Investor Return

• Risks

• Strengths

• Weaknesses

• Red Flags

• Due Diligence საკითხები

თუ დოკუმენტი პროექტია:

• ძირითადი პარამეტრები

• ფართობები

• ღირებულებები

• ვადები

• შემოსავლები

• ხარჯები

• მომგებიანობა

არ მოიგონო ინფორმაცია.

რაც დოკუმენტში არ წერია,

მიუთითე როგორც "არ არის მითითებული".

პასუხი დაწერე ქართულად.

"""

    return gemini_generate(prompt)

def process_document_file(

    chat_id,

    file_path,

    filename,

    user_request=""

):

    file_type = detect_file_type(file_path)

    send_message(

        chat_id,

        f"📄 ვამუშავებ ფაილს...\n\n"

        f"ფაილი: {filename}\n"

        f"ტიპი: {file_type}"

    )

    extracted_text = extract_file_text(

        file_path

    )

    save_document_record(

        chat_id=chat_id,

        filename=filename,

        file_type=file_type,

        file_path=str(file_path),

        extracted_text=extracted_text

    )

    answer = analyze_document_with_ai(

        chat_id=chat_id,

        filename=filename,

        extracted_text=extracted_text,

        user_request=user_request

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

def telegram_download_file(

    file_id,

    filename

):

    try:

        response = requests.get(

            telegram_url("getFile"),

            params={

                "file_id": file_id

            },

            timeout=30

        )

        if response.status_code != 200:

            return None

        data = response.json()

        file_info = data.get("result")

        if not file_info:

            return None

        file_path = file_info.get("file_path")

        if not file_path:

            return None

        download_url = (

            f"https://api.telegram.org/file/bot"

            f"{TELEGRAM_BOT_TOKEN}/"

            f"{file_path}"

        )

        local_path = DOWNLOAD_DIR / filename

        file_response = requests.get(

            download_url,

            timeout=120

        )

        if file_response.status_code != 200:

            return None

        local_path.write_bytes(

            file_response.content

        )

        return local_path

    except Exception:

        logging.exception(

            "Telegram file download error"

        )

        return None

def handle_document_update(

    chat_id,

    document,

    caption=""

):

    file_id = document.get("file_id")

    filename = document.get(

        "file_name",

        f"file_{int(time.time())}"

    )

    if not file_id:

        send_message(

            chat_id,

            "❌ ფაილის ID ვერ მოიძებნა."

        )

        return

    suffix = Path(filename).suffix.lower()

    allowed = [

        ".pdf",

        ".docx",

        ".doc",

        ".xlsx",

        ".xlsm",

        ".pptx"

    ]

    if suffix not in allowed:

        send_message(

            chat_id,

            "❌ ამ ეტაპზე მხარდაჭერილია:\n"

            "PDF, Word, Excel და PowerPoint."

        )

        return

    local_path = telegram_download_file(

        file_id,

        filename

    )

    if not local_path:

        send_message(

            chat_id,

            "❌ ფაილის ჩამოტვირთვა ვერ მოხერხდა."

        )

        return

    process_document_file(

        chat_id=chat_id,

        file_path=local_path,

        filename=filename,

        user_request=caption

    )

print("GENIOSA PART 6 LOADED")# ============================================================

# GENIOSA 4.0 — PART 7

# IMAGE / VISION ANALYSIS

# ============================================================

def telegram_download_photo(file_id):

    try:

        response = requests.get(

            telegram_url("getFile"),

            params={

                "file_id": file_id

            },

            timeout=30

        )

        if response.status_code != 200:

            return None

        data = response.json()

        file_info = data.get("result")

        if not file_info:

            return None

        file_path = file_info.get("file_path")

        if not file_path:

            return None

        download_url = (

            f"https://api.telegram.org/file/bot"

            f"{TELEGRAM_BOT_TOKEN}/"

            f"{file_path}"

        )

        image_response = requests.get(

            download_url,

            timeout=120

        )

        if image_response.status_code != 200:

            return None

        extension = Path(file_path).suffix.lower()

        if not extension:

            extension = ".jpg"

        filename = (

            f"image_{int(time.time())}"

            f"{extension}"

        )

        local_path = DOWNLOAD_DIR / filename

        local_path.write_bytes(

            image_response.content

        )

        return local_path

    except Exception:

        logging.exception(

            "Telegram photo download error"

        )

        return None

def image_to_base64(file_path):

    try:

        import base64

        data = Path(file_path).read_bytes()

        return base64.b64encode(data).decode(

            "utf-8"

        )

    except Exception:

        logging.exception(

            "Image encoding error"

        )

        return None

def image_mime_type(file_path):

    suffix = Path(file_path).suffix.lower()

    mime_types = {

        ".jpg": "image/jpeg",

        ".jpeg": "image/jpeg",

        ".png": "image/png",

        ".webp": "image/webp"

    }

    return mime_types.get(

        suffix,

        "image/jpeg"

    )

def gemini_analyze_image(

    chat_id,

    image_path,

    user_request=""

):

    if not GEMINI_API_KEY:

        return "❌ GEMINI_API_KEY არ არის მითითებული."

    image_base64 = image_to_base64(

        image_path

    )

    if not image_base64:

        return "❌ სურათის წაკითხვა ვერ მოხერხდა."

    context = build_business_context(

        chat_id

    )

    prompt = f"""

შენ ხარ Geniosa 4.0-ის Vision / Image Analysis მოდული.

მომხმარებლის ბიზნეს კონტექსტი:

{context}

მომხმარებლის მოთხოვნა:

{user_request or "გაანალიზე ეს სურათი."}

სურათის ანალიზისას:

1. აღწერე რა ჩანს.

2. გამოყავი მნიშვნელოვანი დეტალები.

3. თუ ეს არის სამშენებლო ობიექტი,

   შეაფასე ხილული სამშენებლო მდგომარეობა.

4. თუ ეს არის გეგმა ან პროექტის ნახაზი,

   ახსენი ხილული ინფორმაცია.

5. თუ ეს არის ცხრილი ან ფინანსური დოკუმენტი,

   ამოიღე ხილული მონაცემები.

6. თუ ეს არის პროდუქტი ან კომერციული ობიექტი,

   გააკეთე ბიზნეს-ანალიზი.

7. არ გამოიგონო ის, რაც სურათზე არ ჩანს.

8. თუ რაიმეში დარწმუნებული არ ხარ,

   პირდაპირ მიუთითე.

პასუხი დაწერე ქართულად.

"""

    mime_type = image_mime_type(

        image_path

    )

    payload = {

        "contents": [

            {

                "parts": [

                    {

                        "text": prompt

                    },

                    {

                        "inline_data": {

                            "mime_type": mime_type,

                            "data": image_base64

                        }

                    }

                ]

            }

        ],

        "generationConfig": {

            "temperature": 0.2,

            "maxOutputTokens": 3000

        }

    }

    try:

        response = requests.post(

            GEMINI_URL,

            headers={

                "Content-Type": "application/json"

            },

            params={

                "key": GEMINI_API_KEY

            },

            json=payload,

            timeout=120

        )

        if response.status_code != 200:

            logging.error(

                "Gemini Vision error %s: %s",

                response.status_code,

                response.text[:1000]

            )

            return (

                "❌ სურათის AI ანალიზი ვერ შესრულდა.\n"

                f"კოდი: {response.status_code}"

            )

        data = response.json()

        candidates = data.get(

            "candidates",

            []

        )

        if not candidates:

            return "❌ AI-მ სურათზე პასუხი ვერ დააბრუნა."

        parts = candidates[0].get(

            "content",

            {}

        ).get(

            "parts",

            []

        )

        result = []

        for part in parts:

            if part.get("text"):

                result.append(

                    part["text"]

                )

        answer = "\n".join(

            result

        ).strip()

        if not answer:

            return "❌ AI-მ ცარიელი პასუხი დააბრუნა."

        return answer

    except requests.exceptions.Timeout:

        return "❌ სურათის ანალიზს ძალიან დიდი დრო დასჭირდა."

    except Exception as e:

        logging.exception(

            "Gemini Vision exception"

        )

        return "❌ Vision შეცდომა: " + str(e)

def process_photo_file(

    chat_id,

    image_path,

    caption=""

):

    send_message(

        chat_id,

        "🖼️ სურათს ვაანალიზებ..."

    )

    answer = gemini_analyze_image(

        chat_id=chat_id,

        image_path=image_path,

        user_request=caption

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

def handle_photo_update(

    chat_id,

    photo,

    caption=""

):

    if not photo:

        return

    # Telegram აგზავნის რამდენიმე ზომას.

    # ვიღებთ ყველაზე დიდ ვერსიას.

    largest_photo = photo[-1]

    file_id = largest_photo.get(

        "file_id"

    )

    if not file_id:

        send_message(

            chat_id,

            "❌ სურათის ID ვერ მოიძებნა."

        )

        return

    local_path = telegram_download_photo(

        file_id

    )

    if not local_path:

        send_message(

            chat_id,

            "❌ სურათის ჩამოტვირთვა ვერ მოხერხდა."

        )

        return

    process_photo_file(

        chat_id=chat_id,

        image_path=local_path,

        caption=caption

    )

# ------------------------------------------------------------

# IMAGE COMMAND EXTENSION

# ------------------------------------------------------------

def process_message_media(

    chat_id,

    message

):

    caption = (

        message.get("caption")

        or ""

    ).strip()

    if message.get("photo"):

        handle_photo_update(

            chat_id,

            message["photo"],

            caption

        )

        return True

    if message.get("document"):

        handle_document_update(

            chat_id,

            message["document"],

            caption

        )

        return True

    return False

print("GENIOSA PART 7 LOADED")# ============================================================

# GENIOSA 4.0 — PART 8

# UNIFIED TELEGRAM MESSAGE HANDLER

# ============================================================

def process_update(update):

    try:

        message = update.get("message")

        if not message:

            return

        chat = message.get("chat", {})

        chat_id = chat.get("id")

        if not chat_id:

            return

        # ----------------------------------------------------

        # SECURITY

        # ----------------------------------------------------

        if not user_allowed(chat_id):

            send_message(

                chat_id,

                "⛔ ამ Geniosa-ს გამოყენების უფლება არ გაქვს."

            )

            return

        # ----------------------------------------------------

        # PHOTO

        # ----------------------------------------------------

        if message.get("photo"):

            process_message_media(

                chat_id,

                message

            )

            return

        # ----------------------------------------------------

        # DOCUMENT

        # ----------------------------------------------------

        if message.get("document"):

            process_message_media(

                chat_id,

                message

            )

            return

        # ----------------------------------------------------

        # TEXT

        # ----------------------------------------------------

        text_value = message.get("text")

        if not text_value:

            send_message(

                chat_id,

                "📎 ეს ფაილის ტიპი ჯერ არ არის მხარდაჭერილი."

            )

            return

        text_value = text_value.strip()

        if not text_value:

            return

        # ----------------------------------------------------

        # COMMANDS

        # ----------------------------------------------------

        if text_value.startswith("/"):

            handled = handle_command(

                chat_id,

                text_value

            )

            if handled:

                return

        # ----------------------------------------------------

        # AI CHAT

        # ----------------------------------------------------

        handle_ai_message(

            chat_id,

            text_value

        )

    except Exception:

        logging.exception(

            "Unified update processing error"

        )

# ============================================================

# GENIOSA STATUS

# ============================================================

@app.get("/status")

def status():

    return {

        "service": "Geniosa",

        "version": "4.0",

        "telegram": bool(TELEGRAM_BOT_TOKEN),

        "gemini": bool(GEMINI_API_KEY),

        "database": bool(DATABASE_URL),

        "status": "online"

    }

print("GENIOSA PART 8 LOADED")# ============================================================

# GENIOSA 4.0 — PART 9

# IMAGE / ANIMATION / VIDEO GENERATION ENGINE

# ============================================================

GENERATION_DIR = DOWNLOAD_DIR / "generated"

GENERATION_DIR.mkdir(

    parents=True,

    exist_ok=True

)

GENERATION_TYPES = {

    "image": "Image",

    "visual": "Architectural Visual",

    "animation": "Animation",

    "video": "Video",

    "social": "Social Media Content"

}

def normalize_generation_type(value):

    value = (value or "").strip().lower()

    aliases = {

        "image": "image",

        "სურათი": "image",

        "სურათის": "image",

        "ფოტო": "image",

        "photo": "image",

        "visual": "visual",

        "ვიზუალი": "visual",

        "რენდერი": "visual",

        "render": "visual",

        "არქიტექტურული ვიზუალი": "visual",

        "animation": "animation",

        "ანიმაცია": "animation",

        "ანიმაცია": "animation",

        "video": "video",

        "ვიდეო": "video",

        "ვიდეოს": "video",

        "social": "social",

        "სოციალური": "social",

        "social media": "social"

    }

    return aliases.get(

        value,

        value

    )

def build_generation_prompt(

    chat_id,

    generation_type,

    user_request

):

    generation_type = normalize_generation_type(

        generation_type

    )

    context = build_business_context(

        chat_id

    )

    prompt = f"""

შენ ხარ Geniosa 4.0-ის Creative Director

და AI Content Generation Planner.

მომხმარებლის ბიზნეს კონტექსტი:

{context}

გენერაციის ტიპი:

{generation_type}

მომხმარებლის მოთხოვნა:

{user_request}

შექმენი პროფესიონალური გენერაციის Prompt.

Prompt უნდა იყოს საკმარისად დეტალური, რომ

შემდგომში გამოყენებულ იქნას Image Generation,

Architectural Visualization, Animation ან Video

Generation სისტემაში.

თუ საქმე ეხება უძრავ ქონებას ან მშენებლობას,

გაითვალისწინე:

• არქიტექტურული სტილი

• შენობის მასშტაბი

• მასალები

• ფასადი

• მინა

• განათება

• ლანდშაფტი

• გარემო

• ქუჩა

• ადამიანების მასშტაბი

• მანქანები

• დღის/ღამის სცენა

• კამერის კუთხე

• ფოტორეალისტურობა

თუ საქმე ეხება ვიდეოს:

• სცენის ხანგრძლივობა

• კამერის მოძრაობა

• ობიექტის მოძრაობა

• განათების ცვლილება

• გარემოს მოძრაობა

• transition

• cinematic style

• realistic motion

არ დაამატო ისეთი ფაქტები,

რომლებიც მომხმარებელს არ უთქვამს.

დააბრუნე მხოლოდ გენერაციისთვის

გამოსადეგი პროფესიონალური Prompt.

"""

    return gemini_generate(

        prompt

    )

def create_generation_task(

    chat_id,

    generation_type,

    user_request,

    source_file=None

):

    generation_type = normalize_generation_type(

        generation_type

    )

    prompt = build_generation_prompt(

        chat_id=chat_id,

        generation_type=generation_type,

        user_request=user_request

    )

    conn = get_db_connection()

    try:

        with conn.cursor() as cur:

            cur.execute(

                """

                INSERT INTO generated_assets

                (

                    chat_id,

                    asset_type,

                    prompt,

                    status

                )

                VALUES (%s, %s, %s, %s)

                RETURNING id

                """,

                (

                    chat_id,

                    generation_type,

                    prompt,

                    "prompt_ready"

                )

            )

            asset_id = cur.fetchone()[0]

        conn.commit()

        return asset_id, prompt

    finally:

        conn.close()

def generation_request(

    chat_id,

    generation_type,

    user_request

):

    asset_id, prompt = create_generation_task(

        chat_id=chat_id,

        generation_type=generation_type,

        user_request=user_request

    )

    type_name = GENERATION_TYPES.get(

        generation_type,

        generation_type

    )

    send_long_message(

        chat_id,

        f"""🎨 Geniosa Generation Engine

ტიპი: {type_name}

Task ID: #{asset_id}

მოთხოვნა დამუშავებულია.

AI-მ შექმნა პროფესიონალური გენერაციის Prompt:

{prompt}

📌 სტატუსი:

Prompt Ready

შემდეგ ეტაპზე ამ Task-ს პირდაპირ

Image / Animation / Video Generation API-ს

დავუკავშირებთ."""

    )

def detect_generation_request(text):

    text_lower = (

        text or ""

    ).strip().lower()

    video_words = [

        "ვიდეო",

        "ვიდეოს",

        "video",

        "movie",

        "ფილმი"

    ]

    animation_words = [

        "ანიმაცია",

        "ანიმაცი",

        "animation",

        "animate"

    ]

    visual_words = [

        "რენდერი",

        "რენდერი",

        "ვიზუალი",

        "ვიზუალიზაცია",

        "არქიტექტურული ვიზუალი",

        "render",

        "visual"

    ]

    image_words = [

        "სურათი",

        "სურათის შექმნა",

        "ფოტო",

        "image",

        "generate image",

        "create image"

    ]

    social_words = [

        "instagram",

        "facebook",

        "social media",

        "სოციალური მედია"

    ]

    if any(

        word in text_lower

        for word in video_words

    ):

        return "video"

    if any(

        word in text_lower

        for word in animation_words

    ):

        return "animation"

    if any(

        word in text_lower

        for word in visual_words

    ):

        return "visual"

    if any(

        word in text_lower

        for word in social_words

    ):

        return "social"

    if any(

        word in text_lower

        for word in image_words

    ):

        return "image"

    return None

def handle_generation_request(

    chat_id,

    text

):

    generation_type = detect_generation_request(

        text

    )

    if not generation_type:

        return False

    generation_request(

        chat_id=chat_id,

        generation_type=generation_type,

        user_request=text

    )

    return True

print("GENIOSA PART 9 LOADED")# ============================================================

# GENIOSA 4.0 — PART 10

# GENERATION REQUEST ROUTER

# ============================================================

def route_text_request(chat_id, text):

    """

    ამოწმებს, არის თუ არა მომხმარებლის ტექსტი

    Image / Visual / Animation / Video მოთხოვნა.

    """

    try:

        handled = handle_generation_request(

            chat_id,

            text

        )

        if handled:

            return True

    except Exception:

        logging.exception(

            "Generation routing error"

        )

    return False

# ============================================================

# EXTENDED AI MESSAGE HANDLER

# ============================================================

def handle_ai_message(chat_id, user_text):

    save_message(

        chat_id,

        "user",

        user_text

    )

    # --------------------------------------------------------

    # 1. GENERATION REQUEST

    # --------------------------------------------------------

    if route_text_request(

        chat_id,

        user_text

    ):

        return

    # --------------------------------------------------------

    # 2. NORMAL AI REQUEST

    # --------------------------------------------------------

    prompt = ai_system_prompt(

        chat_id

    )

    prompt += f"""

ახლა მომხმარებელმა დაწერა:

{user_text}

უპასუხე ქართულად, თუ მომხმარებელი სხვა ენას არ იყენებს.

პასუხი იყოს:

• კონკრეტული

• პრაქტიკული

• პროფესიონალური

• ადვილად გასაგები

თუ საჭიროა ფინანსური გათვლა,

აჩვენე ფორმულა და შედეგი.

თუ მომხმარებელი ითხოვს პროექტის ანალიზს,

გამოიყენე შენახული პროექტის მონაცემები.

თუ ინფორმაცია არ არის საკმარისი,

არ გამოიგონო მონაცემები და მიუთითე,

რა ინფორმაციაა საჭირო.

"""

    answer = gemini_generate(

        prompt

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

# ============================================================

# GENERATION COMMANDS

# ============================================================

def generation_help(chat_id):

    send_message(

        chat_id,

        """🎨 GENIOSA CREATIVE ENGINE

შემიძლია დაგეხმარო:

🖼️ Image

🏢 Architectural Visual

🎞️ Animation

🎬 Video

📱 Social Media Content

მაგალითები:

„შექმენი NIKKEA 12-ის დღის ვიზუალი“

„შექმენი NIKKEA 12-ის ღამის არქიტექტურული რენდერი“

„გააკეთე შენობის cinematic ანიმაცია“

„შექმენი 15 წამიანი საინვესტიციო ვიდეო“

„შექმენი Instagram-ის სარეკლამო ვიზუალი“

„შექმენი სასტუმროს ინტერიერის ვიზუალი“

Geniosa ჯერ ამზადებს პროფესიონალურ

AI Prompt-ს, შემდეგ კი მას შესაბამის

გენერაციის სისტემას დავუკავშირებთ."""

    )

def handle_generation_command(

    chat_id,

    text

):

    command = text.split()[0].lower()

    if command == "/generate":

        generation_help(chat_id)

        return True

    if command == "/creative":

        generation_help(chat_id)

        return True

    return False

# ============================================================

# UPDATE COMMAND ROUTER EXTENSION

# ============================================================

_original_handle_command = handle_command

def handle_command(chat_id, text):

    if handle_generation_command(

        chat_id,

        text

    ):

        return True

    return _original_handle_command(

        chat_id,

        text

    )

print("GENIOSA PART 10 LOADED")

            
