"""ScrapFit - scrap-material marketplace and project-based material matching.

Run with:   streamlit run app.py

This is a SINGLE-FILE version: everything lives in this one app.py, in labelled sections:

    CONFIGURATION          file locations, constants, public app URL (for QR codes)
    DEMO DATA              the original 7 demo listings (only used if inventory.json is missing)
    THEME                  your original CSS + new badge/responsive styles
    DATA LAYER             inventory.json repository: upgrade of old records, validation, ownership
    PHOTOS / ACCOUNTS      photo saving, local accounts (users.json)
    MATCHING / SEARCH      project matching engine, filters and sorting
    QR CODES               public listing links and QR PNGs
    STOCK IMAGES / AI      illustrative photos, optional Gemini drafting
    SESSION / SHARED UI    login state, guest mode, the one listing-card renderer
    PAGE: ...              one section per page
    MAIN                   startup, welcome screen, sidebar, routing

Files it uses next to app.py:  inventory.json, scrapfit_images/, scrapfit_hero_photo.png
Files it creates:              users.json (accounts), transactions.json (collections/reviews), backups/, inventory.json.bak
"""

import copy
import hashlib
import hmac
import io
import json
import math
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

import streamlit as st

# Must be the first Streamlit command (same settings as the original app).
st.set_page_config(
    page_title="ScrapFit",
    page_icon="♻️",
    layout="wide",
)


# ==============================================================================
# CONFIGURATION: file locations, constants, public app URL
# ==============================================================================
# ScrapFit configuration: file locations, shared constants and the public app URL.
#
# Nothing in this file needs Streamlit to import, so the data and matching code can be
# tested on its own. Streamlit secrets are only read lazily inside get_public_base_url().

PROJECT_DIR = Path(__file__).resolve().parent


def _path_from_env(variable, default):
    """Allow a file location to be overridden (used by the automated tests)."""
    value = os.environ.get(variable, "").strip()
    return Path(value) if value else default


# -------------------- FILE LOCATIONS --------------------
# inventory.json and scrapfit_images keep the same names/locations as the original app,
# so your existing listings and photos are picked up automatically.
DATA_FILE = _path_from_env("SCRAPFIT_DATA_FILE", PROJECT_DIR / "inventory.json")
USERS_FILE = _path_from_env("SCRAPFIT_USERS_FILE", PROJECT_DIR / "users.json")
TRANSACTIONS_FILE = _path_from_env("SCRAPFIT_TRANSACTIONS_FILE", PROJECT_DIR / "transactions.json")
IMAGE_DIR = _path_from_env("SCRAPFIT_IMAGE_DIR", PROJECT_DIR / "scrapfit_images")
BACKUP_DIR = _path_from_env("SCRAPFIT_BACKUP_DIR", PROJECT_DIR / "backups")
HERO_IMAGE = PROJECT_DIR / "scrapfit_hero_photo.png"

# -------------------- LISTING VOCABULARY --------------------
SCHEMA_VERSION = 2  # version 1 = the original prototype records (no schema_version field)

MATERIALS = ["Wood", "Metal"]

QUALITY_GRADES = ["A", "B", "C"]
GRADE_DESCRIPTIONS = {
    "A": "Good condition, minimal defects",
    "B": "Minor scratches, marks or imperfections",
    "C": "Noticeable defects but still usable for smaller parts",
}
# The original app stored a "condition" text such as "A - Clean". It is still written
# so older versions of the app keep working with newer data files.
LEGACY_CONDITION_TEXT = {
    "A": "A - Clean",
    "B": "B - Minor marks",
    "C": "C - Usable for small parts",
}

STATUS_AVAILABLE = "Available"
STATUSES = ["Available", "Reserved", "Sold", "Unavailable"]
# The original app had a single "Sold / Unavailable" option. It cannot be split reliably,
# so it becomes "Unavailable" (still hidden from search and matching, as before).
LEGACY_STATUS_MAP = {"Sold / Unavailable": "Unavailable"}

# Words used to guess a material type for old records that have none.
# Only used to fill an EMPTY field; a value a seller entered is never overwritten.
MATERIAL_TYPE_KEYWORDS = [
    ("plywood", "Plywood"), ("pine", "Pine"), ("teak", "Teak"), ("mdf", "MDF"),
    ("stainless", "Stainless steel"), ("mild steel", "Mild steel"), ("steel", "Steel"),
    ("aluminium", "Aluminium"), ("aluminum", "Aluminium"), ("brass", "Brass"),
    ("copper", "Copper"), ("iron", "Iron"),
]

# Limits shared by the forms and the validation code.
MAX_DIMENSION_MM = 10000
MAX_THICKNESS_MM = 500
MAX_QUANTITY = 1000
MAX_PRICE = 1_000_000
MAX_PHOTOS_PER_LISTING = 5
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# The old "demo" map pin that the original List Scrap form always saved. It is not a real
# location, so it is ignored when sorting by distance.
DEMO_PIN = (13.0827, 80.2707)


# -------------------- PUBLIC APP URL (for QR codes) --------------------

DEFAULT_LOCAL_URL = "http://localhost:8501"


def get_public_base_url():
    """Return the public ScrapFit address used inside QR codes (no trailing slash).

    Order of precedence: environment variable SCRAPFIT_BASE_URL, then the Streamlit
    secret APP_BASE_URL, then the local default (which only works on your own computer).
    """
    value = os.environ.get("SCRAPFIT_BASE_URL", "").strip()
    if not value:
        try:
            import streamlit as st  # imported lazily so tests do not need Streamlit
            value = str(st.secrets.get("APP_BASE_URL", "")).strip()
        except Exception:
            value = ""  # no secrets file configured
    return (value or DEFAULT_LOCAL_URL).rstrip("/")


def is_local_url(url):
    """True when the URL points at this computer, so other people could not open it."""
    lowered = (url or "").lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))


def get_admin_emails():
    """Read configured admin emails from environment variables or Streamlit secrets.

    Supported Streamlit secrets formats:
      ADMIN_EMAILS = "admin@example.com,owner@example.com"

      [admin]
      id = "admin@example.com"
    """
    raw = os.environ.get("SCRAPFIT_ADMIN_EMAILS", "").strip()
    try:
        import streamlit as st
        secrets = st.secrets
        if not raw:
            raw = str(secrets.get("ADMIN_EMAILS", "")).strip()
        if not raw:
            admin_section = secrets.get("admin", {})
            if hasattr(admin_section, "get"):
                raw = str(admin_section.get("id", "")).strip()
        if not raw:
            raw = str(secrets.get("admin_id", "")).strip()
    except Exception:
        pass
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# ==============================================================================
# DEMO DATA: used only when no inventory.json exists
# ==============================================================================
# Demo inventory created only when no inventory.json exists yet.
#
# Copied unchanged from the original app (same IDs: S001-S007).

SAMPLE_INVENTORY = [
    {
        "id": "S001",
        "material": "Wood",
        "name": "Plywood offcut",
        "length": 800,
        "width": 400,
        "thickness": 18,
        "condition": "A - Clean",
        "price": 120,
        "area": "Area A",
        "notes": "Clean rectangular piece",
    },
    {
        "id": "S002",
        "material": "Wood",
        "name": "Pine wood offcut",
        "length": 620,
        "width": 280,
        "thickness": 18,
        "condition": "B - Minor marks",
        "price": 60,
        "area": "Area A",
        "notes": "Minor surface marks",
    },
    {
        "id": "S003",
        "material": "Wood",
        "name": "Plywood panel",
        "length": 1000,
        "width": 500,
        "thickness": 12,
        "condition": "A - Clean",
        "price": 150,
        "area": "Area B",
        "notes": "Suitable for panels",
    },
    {
        "id": "S004",
        "material": "Wood",
        "name": "Small wood piece",
        "length": 550,
        "width": 300,
        "thickness": 18,
        "condition": "C - Usable for small parts",
        "price": 30,
        "area": "Area A",
        "notes": "Useful for smaller components",
    },
    {
        "id": "S005",
        "material": "Metal",
        "name": "Mild steel flat sheet",
        "length": 700,
        "width": 300,
        "thickness": 3,
        "condition": "A - Clean",
        "price": 180,
        "area": "Area B",
        "notes": "Sample mild steel sheet",
    },
    {
        "id": "S006",
        "material": "Metal",
        "name": "Steel offcut",
        "length": 450,
        "width": 250,
        "thickness": 3,
        "condition": "B - Minor marks",
        "price": 70,
        "area": "Area A",
        "notes": "Small reusable sheet",
    },
    {
        "id": "S007",
        "material": "Metal",
        "name": "Steel plate",
        "length": 900,
        "width": 400,
        "thickness": 5,
        "condition": "A - Clean",
        "price": 250,
        "area": "Area C",
        "notes": "Sample plate; verify material grade",
    },
]


# ==============================================================================
# THEME: original CSS (unchanged) + new badge / responsive CSS
# ==============================================================================
# ScrapFit visual theme.
#
# The CSS below is the original ScrapFit theme, moved here unchanged so app.py stays
# readable. New styles for badges and listing cards are appended in EXTRA_CSS.

ORIGINAL_CSS = r"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@400;500;600;700;800&display=swap');

    :root {
        --forest: #215747;
        --forest-dark: #173F34;
        --mint: #E8F1E9;
        --paper: #F7F8F4;
        --ink: #202B26;
        --muted: #718078;
        --line: #E1E7E0;
    }

    .stApp {
        background: var(--paper);
        color: var(--ink);
        font-family: 'DM Sans', sans-serif;
    }

    [data-testid="stHeader"] {
        background: rgba(247, 248, 244, 0.92);
    }

    [data-testid="stSidebar"] {
        background: #EDF2EB;
        border-right: 1px solid var(--line);
    }

    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 {
        color: var(--forest-dark);
        font-family: 'Manrope', sans-serif;
    }

    h1, h2, h3 {
        color: var(--forest-dark);
        font-family: 'Manrope', sans-serif !important;
        letter-spacing: -0.5px;
    }

    h1 {
        font-weight: 800 !important;
    }

    p, label, .stMarkdown {
        color: var(--ink);
    }

    [data-testid="stMetric"] {
        background: white;
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 14px 16px;
        box-shadow: 0 2px 8px rgba(23, 63, 52, 0.035);
    }

    [data-testid="stMetricLabel"] {
        color: var(--muted);
        font-weight: 600;
    }

    [data-testid="stMetricValue"] {
        color: var(--forest);
        font-family: 'Manrope', sans-serif;
        font-weight: 800;
    }

    .stButton > button,
    .stFormSubmitButton > button {
        background: var(--forest);
        color: white;
        border: 1px solid var(--forest);
        border-radius: 9px;
        min-height: 42px;
        font-weight: 600;
        transition: all 0.2s ease;
    }

    .stButton > button:hover,
    .stFormSubmitButton > button:hover {
        background: var(--forest-dark);
        color: white;
        border-color: var(--forest-dark);
        transform: translateY(-1px);
    }

    /* Home hero action buttons: light background with readable dark text */
    .st-key-home_hero .stButton > button {
        background: #F0F5F0;
        color: #205747;
        border-color: #F0F5F0;
    }

    .st-key-home_hero .stButton > button:hover {
        background: #DCE9DF;
        color: #173F34;
        border-color: #DCE9DF;
    }

    .stTextInput input,
    .stNumberInput input,
    .stTextArea textarea,
    [data-baseweb="input"] input,
    [data-baseweb="textarea"] textarea,
    [data-baseweb="select"] > div {
        border-radius: 9px;
        border-color: #D8E1D8;
    }

    /* Keep typed text legible in the app's default light design. */
    .stTextInput input,
    .stNumberInput input,
    .stTextArea textarea,
    [data-baseweb="input"] input,
    [data-baseweb="textarea"] textarea {
        color: #202B26 !important;
        -webkit-text-fill-color: #202B26 !important;
        background-color: #FFFFFF !important;
        caret-color: #215747 !important;
        opacity: 1 !important;
    }

    .stTextInput input::placeholder,
    .stNumberInput input::placeholder,
    .stTextArea textarea::placeholder,
    [data-baseweb="input"] input::placeholder,
    [data-baseweb="textarea"] textarea::placeholder {
        color: #718078 !important;
        -webkit-text-fill-color: #718078 !important;
        opacity: 1 !important;
    }

    /* Streamlit can inherit dark-theme widget styles even though ScrapFit uses
       a light green page design. Explicit dark-mode widget colours prevent
       typed values and selected dropdown text from disappearing. */
    [data-theme="dark"] .stTextInput input,
    [data-theme="dark"] .stNumberInput input,
    [data-theme="dark"] .stTextArea textarea,
    [data-theme="dark"] [data-baseweb="input"] input,
    [data-theme="dark"] [data-baseweb="textarea"] textarea {
        color: #F3F7F4 !important;
        -webkit-text-fill-color: #F3F7F4 !important;
        background-color: #202B26 !important;
        border-color: #60756A !important;
        caret-color: #B8E3C5 !important;
    }

    [data-theme="dark"] .stTextInput input::placeholder,
    [data-theme="dark"] .stNumberInput input::placeholder,
    [data-theme="dark"] .stTextArea textarea::placeholder,
    [data-theme="dark"] [data-baseweb="input"] input::placeholder,
    [data-theme="dark"] [data-baseweb="textarea"] textarea::placeholder {
        color: #BAC8BF !important;
        -webkit-text-fill-color: #BAC8BF !important;
        opacity: 1 !important;
    }

    [data-theme="dark"] [data-baseweb="select"] [role="combobox"],
    [data-theme="dark"] [data-baseweb="select"] [role="combobox"] *,
    [data-theme="dark"] [data-baseweb="select"] > div {
        color: #F3F7F4 !important;
        -webkit-text-fill-color: #F3F7F4 !important;
        background-color: #202B26 !important;
        border-color: #60756A !important;
    }

    [data-theme="dark"] [data-baseweb="menu"],
    [data-theme="dark"] [role="listbox"],
    [data-theme="dark"] [role="option"] {
        color: #F3F7F4 !important;
        background-color: #202B26 !important;
    }

    [data-baseweb="tab-list"] {
        gap: 8px;
        border-bottom: 1px solid var(--line);
    }

    [data-baseweb="tab"] {
        background: transparent;
        border-radius: 8px 8px 0 0;
        padding: 10px 14px;
        font-weight: 600;
    }

    [aria-selected="true"][data-baseweb="tab"] {
        color: var(--forest) !important;
    }

    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--line);
        border-radius: 14px;
        background: white;
        padding: 10px;
        box-shadow: 0 2px 8px rgba(23, 63, 52, 0.025);
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        color: var(--muted);
    }

    [data-testid="stSidebar"] [role="radiogroup"] {
        gap: 6px;
    }

    [data-testid="stSidebar"] [role="radiogroup"] label {
        padding: 8px 10px;
        border-radius: 9px;
    }

    [data-testid="stSidebar"] [role="radiogroup"] label:hover {
        background: #DDE9DE;
    }

    /* High-contrast sidebar toggle: target Streamlit's collapsed control and
       header-labelled sidebar buttons across Streamlit DOM/theme variations. */
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="stSidebarCollapseButton"] button,
    [data-testid="stSidebarCollapsedControl"] button,
    button[data-testid="stSidebarCollapseButton"],
    button[data-testid="stSidebarCollapsedControl"],
    [data-testid="stHeader"] button[aria-label*="sidebar" i],
    [data-testid="stHeader"] button[title*="sidebar" i] {
        color: #173F34 !important;
        background: #B8E3C5 !important;
        border: 2px solid #215747 !important;
        border-radius: 10px !important;
        opacity: 1 !important;
        visibility: visible !important;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.20) !important;
        min-width: 40px !important;
        min-height: 40px !important;
    }

    [data-testid="stSidebarCollapseButton"] svg,
    [data-testid="stSidebarCollapsedControl"] svg,
    [data-testid="stSidebarCollapseButton"] button svg,
    [data-testid="stSidebarCollapsedControl"] button svg,
    button[data-testid="stSidebarCollapseButton"] svg,
    button[data-testid="stSidebarCollapsedControl"] svg,
    [data-testid="stHeader"] button[aria-label*="sidebar" i] svg,
    [data-testid="stHeader"] button[title*="sidebar" i] svg,
    [data-testid="stSidebarCollapseButton"] svg *,
    [data-testid="stSidebarCollapsedControl"] svg * {
        color: #173F34 !important;
        stroke: #173F34 !important;
        fill: #173F34 !important;
        opacity: 1 !important;
        visibility: visible !important;
    }

    /* Keep the same high contrast in both light and dark Streamlit themes. */
    [data-theme="dark"] [data-testid="stSidebarCollapseButton"],
    [data-theme="dark"] [data-testid="stSidebarCollapsedControl"],
    [data-theme="dark"] [data-testid="stSidebarCollapseButton"] button,
    [data-theme="dark"] [data-testid="stSidebarCollapsedControl"] button,
    [data-theme="dark"] [data-testid="stHeader"] button[aria-label*="sidebar" i],
    [data-theme="dark"] [data-testid="stHeader"] button[title*="sidebar" i] {
        color: #173F34 !important;
        background: #B8E3C5 !important;
        border-color: #215747 !important;
    }

    [data-theme="dark"] [data-testid="stSidebarCollapseButton"] svg,
    [data-theme="dark"] [data-testid="stSidebarCollapsedControl"] svg,
    [data-theme="dark"] [data-testid="stHeader"] button[aria-label*="sidebar" i] svg,
    [data-theme="dark"] [data-testid="stHeader"] button[title*="sidebar" i] svg,
    [data-theme="dark"] [data-testid="stSidebarCollapseButton"] svg *,
    [data-theme="dark"] [data-testid="stSidebarCollapsedControl"] svg * {
        color: #173F34 !important;
        stroke: #173F34 !important;
        fill: #173F34 !important;
    }

    .scrapfit-about-card {
        background: white;
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 18px;
        height: 100%;
    }

    .scrapfit-about-card h3 {
        margin-top: 0;
    }

    [data-testid="stAlert"] {
        border-radius: 12px;
    }

    hr {
        border-color: var(--line);
    }

    .st-key-home_hero {
        background: linear-gradient(120deg, #173F34 0%, #215747 58%, #2F6B52 100%);
        color: white;
        padding: 30px 32px 26px 32px;
        border-radius: 18px;
        margin: 8px 0 24px 0;
        overflow: hidden;
        border: 1px solid rgba(255,255,255,0.08);
    }
    .st-key-home_hero h1, .st-key-home_hero h2, .st-key-home_hero h3,
    .st-key-home_hero p, .st-key-home_hero label,
    .st-key-home_hero [data-testid="stMarkdownContainer"] {
        color: #FFFFFF !important;
    }
    .st-key-home_hero .scrapfit-eyebrow {
        color: #C6E1CD !important;
        font-size: 0.78rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 2px;
        margin-bottom: 12px;
    }
    .st-key-home_hero .scrapfit-hero-art {
        min-height: 180px;
        display: flex;
        align-items: center;
        justify-content: center;
        border: 1px solid rgba(255,255,255,0.16);
        border-radius: 16px;
        background: rgba(232,241,233,0.09);
        padding: 12px;
    }
    .st-key-home_hero .scrapfit-hero-art svg { width: 100%; max-width: 360px; height: auto; display: block; }
    .st-key-home_hero [data-testid="stButton"] > button,
    .st-key-home_hero [data-testid="stButton"] > button * {
        background: #F0F5F0 !important;
        color: #173F34 !important;
        -webkit-text-fill-color: #173F34 !important;
        border-color: #F0F5F0 !important;
        font-weight: 700 !important;
        min-height: 46px;
        opacity: 1 !important;
    }
    .st-key-home_hero [data-testid="stButton"] > button:hover,
    .st-key-home_hero [data-testid="stButton"] > button:hover * {
        background: #DDE9DE !important;
        color: #173F34 !important;
        -webkit-text-fill-color: #173F34 !important;
        border-color: #DDE9DE !important;
    }
    .st-key-home_hero [data-testid="stImage"] img {
        width: 100%;
        height: 220px;
        object-fit: cover;
        border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.2);
    }

    .scrapfit-hero-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.35fr) minmax(220px, 0.8fr);
        align-items: center;
        gap: 24px;
    }

    .scrapfit-hero-copy { min-width: 0; }
    .scrapfit-hero-art {
        min-height: 190px;
        display: flex;
        align-items: center;
        justify-content: center;
        border: 1px solid rgba(255,255,255,0.16);
        border-radius: 16px;
        background: rgba(232,241,233,0.09);
        padding: 12px;
    }
    .scrapfit-hero-art svg { width: 100%; max-width: 360px; height: auto; display: block; }

    @media (max-width: 760px) {
        .scrapfit-hero-grid { grid-template-columns: 1fr; }
        .scrapfit-hero-art { min-height: 130px; }
    }

    .scrapfit-hero h1 {
        color: white !important;
        font-size: 2.2rem;
        line-height: 1.15;
        margin-bottom: 12px;
    }

    .scrapfit-hero p {
        color: #E1EEE4;
        font-size: 1.05rem;
        line-height: 1.7;
        max-width: 650px;
        margin-bottom: 0;
    }

    .scrapfit-eyebrow {
        color: #C6E1CD;
        font-size: 0.78rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 2px;
        margin-bottom: 12px;
    }

    .scrapfit-section-label {
        color: var(--forest);
        font-size: 0.78rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1.4px;
    }

    .scrapfit-note {
        background: var(--mint);
        border-left: 4px solid var(--forest);
        border-radius: 5px 12px 12px 5px;
        padding: 14px 18px;
        margin: 12px 0 20px 0;
        color: var(--forest-dark);
        line-height: 1.6;
    }

    @media (max-width: 768px) {
        .scrapfit-hero {
            padding: 25px 22px;
        }

        .scrapfit-hero h1 {
            font-size: 1.8rem;
        }
    }


    /* Professional ScrapFit design system */
    .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1440px; }
    [data-testid="stSidebar"] { background: #EEF3ED; }
    [data-testid="stSidebar"] [data-testid="stRadio"] label { font-weight: 600; color: #34483D; }
    [data-testid="stSidebar"] [data-testid="stRadio"] label[data-checked="true"] { background: #DCE9DE; }
    .sf-eyebrow { color: #A8D5B7; font-size: .74rem; font-weight: 800; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 14px; }
    .sf-hero-title { font-family: 'Manrope', sans-serif; color: #FFFFFF; font-size: clamp(2.15rem, 3.3vw, 3.5rem); font-weight: 800; line-height: 1.08; letter-spacing: -1.6px; margin: 0 0 18px 0; }
    .sf-hero-copy { color: #E5F0E7; font-size: 1.04rem; line-height: 1.75; max-width: 720px; }
    .sf-hero-kicker { color: #D0E7D5; font-size: .9rem; font-weight: 600; margin-top: 18px; }
    .sf-section-kicker { color: #47745B; text-transform: uppercase; font-size: .73rem; font-weight: 800; letter-spacing: 1.8px; margin-bottom: 6px; }
    .sf-section-title { font-family: 'Manrope', sans-serif; color: #173F34; font-size: 1.8rem; font-weight: 800; letter-spacing: -.6px; margin: 0 0 8px 0; }
    .sf-section-copy { color: #65756B; font-size: .98rem; line-height: 1.7; margin-bottom: 18px; }
    .sf-card { background: #FFFFFF; border: 1px solid #E0E8DF; border-radius: 16px; padding: 22px; height: 100%; box-shadow: 0 5px 18px rgba(23,63,52,.045); }
    .sf-card-icon { display: inline-flex; width: 44px; height: 44px; border-radius: 13px; background: #EAF3E9; color: #215747; align-items: center; justify-content: center; font-size: 1.35rem; margin-bottom: 14px; }
    .sf-card h3 { color: #173F34; font-size: 1.08rem; margin: 0 0 8px 0; letter-spacing: -.2px; }
    .sf-card p { color: #65756B; line-height: 1.65; font-size: .92rem; margin: 0; }
    .sf-step { background: #F0F5EF; border: 1px solid #E0E8DF; border-radius: 14px; padding: 18px; height: 100%; }
    .sf-step-num { font-size: .75rem; color: #47745B; font-weight: 800; letter-spacing: 1.2px; margin-bottom: 8px; }
    .sf-step h3 { font-size: 1rem; color: #173F34; margin: 0 0 8px 0; }
    .sf-step p { color: #65756B; font-size: .9rem; line-height: 1.6; margin: 0; }
    .sf-callout { background: #EAF3E9; border: 1px solid #D7E6D7; border-left: 4px solid #47745B; border-radius: 12px; padding: 16px 18px; color: #254D3B; line-height: 1.65; }
    .sf-flow { display: flex; align-items: center; justify-content: space-between; gap: 8px; background: #173F34; color: white; border-radius: 14px; padding: 20px; }
    .sf-flow span { text-align: center; flex: 1; font-size: .88rem; font-weight: 700; }
    .sf-flow b { color: #A8D5B7; font-size: 1.15rem; }
    .sf-footnote { color: #77857C; font-size: .8rem; line-height: 1.6; }
    .sf-divider { height: 1px; background: #E1E7E0; margin: 28px 0; }
    .st-key-home_hero { background: linear-gradient(125deg, #153D32 0%, #215747 56%, #2D6B51 100%); border-radius: 24px; padding: 34px; border: 1px solid rgba(255,255,255,.1); box-shadow: 0 14px 36px rgba(23,63,52,.12); }
    .st-key-home_hero [data-testid="stImage"] img { height: 310px; object-fit: cover; border-radius: 17px; border: 1px solid rgba(255,255,255,.22); }
    .st-key-home_hero h1, .st-key-home_hero h2, .st-key-home_hero h3, .st-key-home_hero p { color: #FFFFFF; }
    .st-key-home_hero [data-testid="stButton"] > button, .st-key-home_hero [data-testid="stButton"] > button * { background: #F2F6F0 !important; color: #173F34 !important; -webkit-text-fill-color: #173F34 !important; border: 1px solid #F2F6F0 !important; font-weight: 800 !important; opacity: 1 !important; }
    .st-key-home_hero [data-testid="stButton"] > button { min-height: 48px; border-radius: 11px; }
    .st-key-home_hero [data-testid="stButton"] > button:hover, .st-key-home_hero [data-testid="stButton"] > button:hover * { background: #DCE9DE !important; color: #173F34 !important; -webkit-text-fill-color: #173F34 !important; border-color: #DCE9DE !important; }
    @media (max-width: 760px) { .st-key-home_hero { padding: 22px; } .st-key-home_hero [data-testid="stImage"] img { height: 210px; } .sf-flow { flex-direction: column; } }

</style>
"""


# New styles added in this upgrade. They reuse the original palette (forest green, mint, paper).
EXTRA_CSS = """
<style>
    .sf-badge { display: inline-block; padding: 3px 10px; margin: 0 6px 6px 0; border-radius: 999px;
        font-size: .74rem; font-weight: 700; letter-spacing: .2px; line-height: 1.4; white-space: nowrap; }
    .sf-badge-a { background: #DDEFE0; color: #1B5E3A; border: 1px solid #BCDDC4; }
    .sf-badge-b { background: #FFF3D6; color: #8A5A00; border: 1px solid #F1DFAE; }
    .sf-badge-c { background: #F8E3DC; color: #9A3B20; border: 1px solid #EBCABE; }
    .sf-badge-available { background: #EAF3E9; color: #215747; border: 1px solid #D0E3D1; }
    .sf-badge-reserved { background: #FFF3D6; color: #8A5A00; border: 1px solid #F1DFAE; }
    .sf-badge-sold { background: #ECE9E4; color: #5F5A52; border: 1px solid #DAD5CC; }
    .sf-badge-unavailable { background: #EFE6E6; color: #7A3B3B; border: 1px solid #E0CFCF; }
    .sf-badge-neutral { background: #F0F5EF; color: #47745B; border: 1px solid #E0E8DF; }
    .sf-badge-verified { background: #E4F0FF; color: #174A83; border: 1px solid #C5DAF3; }
    .sf-badge-trusted { background: #E5F4EC; color: #175B37; border: 1px solid #BFE2CC; }
    .sf-welcome-note { color: #D9E8DC; font-size: .95rem; line-height: 1.6; }
    .sf-user-chip { background: #EAF3E9; border: 1px solid #D7E6D7; border-radius: 12px; padding: 10px 12px;
        color: #173F34; font-size: .88rem; line-height: 1.5; margin-bottom: 8px; }
    .sf-qr-box { background: #FFFFFF; border: 1px solid #E1E7E0; border-radius: 14px; padding: 12px; }

    /* Listing cards: keep photos a sensible size and let long text wrap on small screens. */
    [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stImage"] img { max-height: 260px; object-fit: cover; border-radius: 10px; }
    [data-testid="stCaptionContainer"], [data-testid="stMarkdownContainer"] p { overflow-wrap: anywhere; }

    /* Tablet and phone: Streamlit stacks columns; make controls comfortable to tap. */
    @media (max-width: 900px) {
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stImage"] img { max-height: 220px; }
        .block-container { padding-left: 1rem !important; padding-right: 1rem !important; }
    }
    @media (max-width: 640px) {
        .sf-badge { font-size: .7rem; padding: 2px 8px; }
        [data-testid="stButton"] > button, [data-testid="stDownloadButton"] > button { min-height: 46px; }
        [data-testid="stMetricValue"] { font-size: 1.4rem; }
        [data-testid="stVerticalBlockBorderWrapper"] [data-testid="stImage"] img { max-height: 190px; }
    }
</style>
"""


# ==============================================================================
# DATA LAYER: listings in inventory.json (normalisation, validation, ownership rules)
# ==============================================================================
# ScrapFit listing storage (JSON file) - no Streamlit code in here.
#
# Design notes for a first-year student:
#
# * The UI never touches inventory.json directly. It calls the repository methods below
#   (list_all, get, create, update, delete, adjust_quantity, claim).
# * Every method that changes data re-reads the file from disk first, changes it, and writes
#   it back atomically while holding a lock. That keeps two browser sessions from
#   overwriting each other's work.
# * Ownership rules (only the owner may edit or delete) are enforced HERE, not just by
#   hiding buttons in the UI.
# * When ScrapFit later moves to Supabase, write a `SupabaseListingRepository` with the same
#   method names and the UI will not need to change.

# One lock for the whole process: Streamlit runs every browser session in the same process.
@st.cache_resource
def _shared_lock(name):
    """One lock per server process.

    Streamlit re-runs this whole file on every click, so a plain module-level lock would be
    created again each time and would protect nothing. cache_resource keeps ONE lock alive.
    """
    return threading.RLock()


_FILE_LOCK = _shared_lock("inventory")


# -------------------- ERRORS --------------------

class ListingError(Exception):
    """Base class for all listing problems the UI can show to a user."""


class ValidationError(ListingError):
    """Input was invalid. `errors` is a list of friendly messages."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__(" ".join(self.errors))


class PermissionDenied(ListingError):
    """The user is not allowed to do this (guest, or not the listing owner)."""


class ListingNotFound(ListingError):
    """No listing has that ID."""


class InventoryFileError(ListingError):
    """inventory.json exists but cannot be read. The file is never overwritten in this case."""


# -------------------- SMALL HELPERS --------------------

def now_iso():
    """Current UTC time as text, e.g. 2026-11-03T09:30:00+00:00."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_listing_id():
    """Same style as the original app (S + 6 hex characters), e.g. S9471E9."""
    return "S" + uuid.uuid4().hex[:6].upper()


def parse_whole_number(value):
    """Return an int for 12, 12.0 or '12' (also '12 mm'); None when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return int(round(float(match.group()))) if match else None


def parse_price(value):
    """Prices keep decimals only when they really have them (150 stays 150, 12.5 stays 12.5)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
        if not match:
            return None
        number = float(match.group())
    return int(number) if number == int(number) else round(number, 2)


def phone_digits(phone):
    return re.sub(r"\D", "", str(phone or ""))


def is_valid_phone(phone):
    """8-15 digits; only digits, spaces, +, - and brackets are allowed."""
    text = str(phone or "").strip()
    if not re.fullmatch(r"[0-9+\-\s()]+", text):
        return False
    return 8 <= len(phone_digits(text)) <= 15


def phones_match(first, second):
    """Compare the last digits of two phone numbers (ignores +91 / spaces / dashes)."""
    a, b = phone_digits(first), phone_digits(second)
    if len(a) < 8 or len(b) < 8:
        return False
    n = min(len(a), len(b), 10)
    return a[-n:] == b[-n:]


def _text(value):
    return "" if value is None else str(value).strip()


def guess_material_type(name):
    lowered = _text(name).casefold()
    for keyword, label in MATERIAL_TYPE_KEYWORDS:
        if keyword in lowered:
            return label
    return "Other"


def grade_from_condition(condition):
    """'B - Minor marks' -> 'B'. Returns None when no grade letter can be found."""
    first = _text(condition)[:1].upper()
    return first if first in QUALITY_GRADES else None


# -------------------- NORMALISATION (old records -> current structure) --------------------

def normalise_record(raw):
    """Return a copy of `raw` with every current field present.

    Rules:
    * Nothing is ever removed or overwritten if it already holds a usable value; unknown
      extra keys (anything your own version added) are kept.
    * Missing fields get safe defaults. Where a default is a guess, a short note is added
      to `review_notes` so a human can check it.
    * Running it twice gives the same result (it is idempotent).
    """
    record = copy.deepcopy(raw)
    notes = [str(n) for n in (record.get("review_notes") or [])]

    record["id"] = _text(record.get("id"))
    if not record["id"]:
        record["id"] = new_listing_id()
        notes.append("Listing ID was missing, so a new one was generated.")

    material = _text(record.get("material"))
    if material.casefold() in {m.casefold() for m in MATERIALS}:
        material = next(m for m in MATERIALS if m.casefold() == material.casefold())
    record["material"] = material or "Unspecified"
    record["name"] = _text(record.get("name")) or "Unnamed offcut"
    record["material_type"] = _text(record.get("material_type")) or guess_material_type(record["name"])

    for key in ("length", "width", "thickness"):
        value = parse_whole_number(record.get(key))
        if value is None or value < 0:
            notes.append(f"{key.capitalize()} was missing or invalid and was set to 0 - please correct it.")
            value = 0
        record[key] = value

    price = parse_price(record.get("price"))
    if price is None or price < 0:
        notes.append("Price was missing or invalid and was set to 0 - please correct it.")
        price = 0
    record["price"] = price

    quantity = parse_whole_number(record.get("quantity")) if "quantity" in record else 1
    record["quantity"] = 1 if quantity is None else max(0, quantity)

    # Availability status
    status = _text(record.get("status")) or STATUS_AVAILABLE
    if status in LEGACY_STATUS_MAP:
        record["legacy_status"] = status
        status = LEGACY_STATUS_MAP[status]
    matched = next((s for s in STATUSES if s.casefold() == status.casefold()), None)
    if matched is None:
        record["legacy_status"] = status
        notes.append(f"Unrecognised status '{status}' was set to Unavailable.")
        matched = "Unavailable"
    record["status"] = matched

    # Quality grade (the old app only had the 'condition' text)
    grade = _text(record.get("quality_grade")).upper()
    if grade not in QUALITY_GRADES:
        grade = grade_from_condition(record.get("condition"))
    if grade is None:
        grade = "C"  # the most cautious grade, so quality is never overstated
        notes.append("Quality grade was not specified and was set to C - please review.")
    record["quality_grade"] = grade
    record["condition"] = _text(record.get("condition")) or LEGACY_CONDITION_TEXT[grade]

    for key in ("area", "pickup_location", "pickup_details", "notes", "seller_name", "contact_phone"):
        record[key] = _text(record.get(key))

    # Ownership: old records have no owner. They are flagged for review, never guessed.
    owner_id = _text(record.get("owner_id")) or None
    record["owner_id"] = owner_id
    record["owner_review_needed"] = owner_id is None

    created = _text(record.get("created_at")) or _text(record.get("date_added")) or None
    record["created_at"] = created
    record["updated_at"] = _text(record.get("updated_at")) or created

    # Photos: the old app stored one file name in image_file. Keep it, and mirror it in a list.
    photos = record.get("photos")
    if isinstance(photos, list):
        photos = [Path(str(p)).name for p in photos if _text(p)]
    else:
        photos = []
    legacy_photo = _text(record.get("image_file"))
    if legacy_photo and Path(legacy_photo).name not in photos:
        photos.insert(0, Path(legacy_photo).name)
    record["photos"] = photos
    if photos:
        record["image_file"] = photos[0]
    else:
        record.pop("image_file", None)

    record["schema_version"] = SCHEMA_VERSION
    if notes:
        record["review_notes"] = notes
    return record


def normalise_inventory(raw_items):
    """Normalise a whole list. Returns (records, report).

    The report lists records that were not dictionaries (`invalid`) and records whose
    duplicate IDs had to be renamed (`duplicate_ids`). Nothing is silently discarded.
    """
    records, invalid, duplicates, upgraded = [], [], [], 0
    seen = set()
    for position, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            invalid.append({"position": position, "value": raw})
            continue
        if raw.get("schema_version") != SCHEMA_VERSION:
            upgraded += 1
        record = normalise_record(raw)
        if record["id"] in seen:
            old_id = record["id"]
            record["id"] = new_listing_id()
            record.setdefault("review_notes", []).append(
                f"Duplicate listing ID {old_id}; this copy was given the new ID {record['id']}."
            )
            duplicates.append({"old_id": old_id, "new_id": record["id"]})
        seen.add(record["id"])
        records.append(record)
    report = {
        "total_in_file": len(raw_items), "kept": len(records), "upgraded": upgraded,
        "invalid": invalid, "duplicate_ids": duplicates,
        "needs_owner_review": sum(r["owner_review_needed"] for r in records),
    }
    return records, report


# -------------------- VALIDATION --------------------

def validate_listing(data, *, require_contact=False, require_pickup=False):
    """Check listing fields and return a list of friendly error messages ([] = valid).

    `data` is a dict of fields. Only the fields that are present are checked, except the
    two `require_*` switches which add "must be provided" rules (used when creating).
    """
    errors = []

    if "name" in data:
        name = _text(data["name"])
        if not name:
            errors.append("Please enter a listing name.")
        elif len(name) > 100:
            errors.append("The listing name must be 100 characters or fewer.")

    if "material" in data and _text(data["material"]) not in MATERIALS:
        errors.append("Please choose a material category (Wood or Metal).")

    limits = {
        "length": ("Length", 1, MAX_DIMENSION_MM),
        "width": ("Width", 1, MAX_DIMENSION_MM),
        "thickness": ("Thickness", 1, MAX_THICKNESS_MM),
    }
    for key, (label, low, high) in limits.items():
        if key in data:
            value = parse_whole_number(data[key])
            if value is None or not low <= value <= high:
                errors.append(f"Invalid dimensions: {label.lower()} must be between {low} and {high} mm.")

    if "quantity" in data:
        quantity = parse_whole_number(data["quantity"])
        if quantity is None or not 0 <= quantity <= MAX_QUANTITY:
            errors.append(f"Quantity must be a whole number from 0 to {MAX_QUANTITY}.")

    if "price" in data:
        price = parse_price(data["price"])
        if price is None or not 0 <= price <= MAX_PRICE:
            errors.append(f"Price must be between 0 and {MAX_PRICE:,}.")

    if "quality_grade" in data and _text(data["quality_grade"]).upper() not in QUALITY_GRADES:
        errors.append("Quality grade must be A, B or C.")

    if "status" in data and _text(data["status"]) not in STATUSES:
        errors.append("Availability must be Available, Reserved, Sold or Unavailable.")

    if require_pickup and not _text(data.get("area")):
        errors.append("Please enter a pickup location (area or neighbourhood).")

    phone = _text(data.get("contact_phone"))
    if require_contact and not phone:
        errors.append("Please enter a contact number so buyers can reach you.")
    elif phone and not is_valid_phone(phone):
        errors.append("The contact number should have 8-15 digits (you may include +, spaces or dashes).")

    coordinates = data.get("coordinates")
    if coordinates:
        try:
            lat, lon = float(coordinates["lat"]), float(coordinates["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append("The map pin coordinates are not valid.")
    return errors


# Fields a seller may change. Everything else (id, owner_id, created_at...) is protected.
EDITABLE_FIELDS = {
    "name", "material", "material_type", "length", "width", "thickness", "quantity", "price",
    "quality_grade", "status", "area", "pickup_location", "pickup_details", "notes",
    "seller_name", "contact_phone", "coordinates", "photos",
}


def _apply_derived_fields(record):
    """Keep fields that depend on other fields consistent."""
    record["quality_grade"] = _text(record.get("quality_grade")).upper()
    record["condition"] = LEGACY_CONDITION_TEXT[record["quality_grade"]]
    for key in ("length", "width", "thickness", "quantity"):
        record[key] = parse_whole_number(record.get(key))
    record["price"] = parse_price(record.get("price"))
    # No pieces left means the listing cannot still be on offer.
    if record["quantity"] == 0 and record["status"] in ("Available", "Reserved"):
        record["status"] = "Sold"
    photos = [Path(str(p)).name for p in record.get("photos", []) if _text(p)]
    record["photos"] = photos
    if photos:
        record["image_file"] = photos[0]
    else:
        record.pop("image_file", None)
    return record


# -------------------- REPOSITORY --------------------

class ListingRepository:
    """The methods the UI relies on. A future SupabaseListingRepository must provide them too."""

    def list_all(self): raise NotImplementedError
    def get(self, listing_id): raise NotImplementedError
    def create(self, data, actor): raise NotImplementedError
    def update(self, listing_id, changes, actor): raise NotImplementedError
    def delete(self, listing_id, actor): raise NotImplementedError
    def adjust_quantity(self, listing_id, delta, actor): raise NotImplementedError
    def claim(self, listing_id, actor, is_admin=False): raise NotImplementedError


class JsonListingRepository(ListingRepository):
    """Listings stored in a local inventory.json file."""

    def __init__(self, data_file=None, image_dir=None, backup_dir=None, sample_inventory=None):
        self.data_file = Path(data_file or DATA_FILE)
        self.image_dir = Path(image_dir or IMAGE_DIR)
        self.backup_dir = Path(backup_dir or BACKUP_DIR)
        self.sample_inventory = sample_inventory

    # ----- reading -----

    def _read_raw(self):
        """Read the JSON list exactly as stored. Creates the demo file if none exists."""
        if not self.data_file.exists():
            if self.sample_inventory is None:
                return []
            records, _ = normalise_inventory(self.sample_inventory)
            self._write(records)
            return copy.deepcopy(records)
        try:
            with self.data_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            raise InventoryFileError(
                f"Could not read {self.data_file.name}. The file was NOT changed. "
                f"Fix it or restore a copy from the backups folder. ({exc})"
            ) from exc
        if not isinstance(data, list):
            raise InventoryFileError(f"{self.data_file.name} must contain a JSON list of listings.")
        return data

    def _read_records(self):
        records, _ = normalise_inventory(self._read_raw())
        return records

    def list_all(self):
        with _FILE_LOCK:
            return self._read_records()

    def get(self, listing_id):
        listing_id = _text(listing_id)
        for record in self.list_all():
            if record["id"] == listing_id:
                return record
        return None

    # ----- writing -----

    def _write(self, records):
        """Atomic write: temp file first, then replace. A rolling .bak copy is kept."""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        if self.data_file.exists():
            shutil.copy2(self.data_file, self.data_file.with_name(self.data_file.name + ".bak"))
        temporary = self.data_file.with_name(self.data_file.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, ensure_ascii=False)
            handle.flush()
        temporary.replace(self.data_file)

    def create_backup(self, label="manual"):
        """Copy inventory.json into the backups folder and return the new path."""
        with _FILE_LOCK:
            if not self.data_file.exists():
                raise InventoryFileError("There is no inventory.json to back up yet.")
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = self.backup_dir / f"inventory_{label}_{stamp}.json"
            shutil.copy2(self.data_file, target)
            return target

    def upgrade_if_needed(self):
        """Upgrade an old-format file in place, after making a timestamped backup.

        Returns a report dict when an upgrade happened, otherwise None.
        """
        with _FILE_LOCK:
            raw = self._read_raw()
            if all(isinstance(r, dict) and r.get("schema_version") == SCHEMA_VERSION for r in raw):
                return None
            backup_path = self.create_backup("pre_upgrade")
            records, report = normalise_inventory(raw)
            if report["invalid"]:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                quarantine = self.backup_dir / f"invalid_records_{stamp}.json"
                quarantine.write_text(json.dumps(report["invalid"], indent=2, default=str), encoding="utf-8")
                report["invalid_records_file"] = str(quarantine)
            self._write(records)
            report["backup_file"] = str(backup_path)
            return report

    # ----- permission helpers -----

    @staticmethod
    def can_manage(listing, actor):
        """True only for the account that owns the listing."""
        return bool(actor and listing and listing.get("owner_id") and listing["owner_id"] == actor.get("id"))

    @staticmethod
    def can_claim(listing, actor, is_admin=False):
        """An ownerless (old) listing may be claimed by an admin or by the account whose
        profile phone number matches the listing's contact number."""
        if not actor or not listing or listing.get("owner_id"):
            return False
        return is_admin or phones_match(actor.get("phone"), listing.get("contact_phone"))

    def _find_for_owner(self, records, listing_id, actor):
        if not actor:
            raise PermissionDenied("Please log in to manage listings.")
        for index, record in enumerate(records):
            if record["id"] == listing_id:
                if not self.can_manage(record, actor):
                    raise PermissionDenied("You can only change your own listings.")
                return index, record
        raise ListingNotFound(f"Listing {listing_id} was not found.")

    # ----- create / update / delete -----

    def create(self, data, actor):
        """Create a listing owned by `actor`. Guests cannot create listings."""
        if not actor:
            raise PermissionDenied("Please log in or create an account to list scrap.")
        errors = validate_listing(data, require_contact=True, require_pickup=True)
        for needed in ("name", "material", "length", "width", "thickness", "price", "quality_grade"):
            if needed not in data:
                errors.append(f"Missing required field: {needed}.")
        if errors:
            raise ValidationError(errors)

        with _FILE_LOCK:
            records = self._read_records()
            taken = {r["id"] for r in records}
            listing_id = new_listing_id()
            while listing_id in taken:
                listing_id = new_listing_id()
            now = now_iso()
            record = {key: copy.deepcopy(value) for key, value in data.items() if key in EDITABLE_FIELDS}
            record.setdefault("quantity", 1)
            record.setdefault("status", STATUS_AVAILABLE)
            record.update({
                "id": listing_id, "owner_id": actor["id"],
                "created_at": now, "updated_at": now,
            })
            record = normalise_record(record)
            record = _apply_derived_fields(record)
            records.append(record)
            self._write(records)
            return copy.deepcopy(record)

    def update(self, listing_id, changes, actor):
        """Change fields of a listing the actor owns. The ID can never change."""
        protected = set(changes) - EDITABLE_FIELDS
        if protected:
            raise ValidationError(f"These fields cannot be changed: {', '.join(sorted(protected))}.")
        errors = validate_listing(changes)
        if errors:
            raise ValidationError(errors)

        with _FILE_LOCK:
            records = self._read_records()
            index, record = self._find_for_owner(records, _text(listing_id), actor)
            old_photos = list(record.get("photos", []))
            updated = copy.deepcopy(record)
            updated.update(copy.deepcopy(changes))
            updated["updated_at"] = now_iso()
            updated = _apply_derived_fields(updated)
            records[index] = updated
            self._write(records)
            self._delete_unreferenced_photos(old_photos, records)
            return copy.deepcopy(updated)

    def delete(self, listing_id, actor):
        """Delete a listing the actor owns. Returns the deleted record."""
        with _FILE_LOCK:
            records = self._read_records()
            index, record = self._find_for_owner(records, _text(listing_id), actor)
            removed = records.pop(index)
            self._write(records)
            self._delete_unreferenced_photos(removed.get("photos", []), records)
            return removed

    def adjust_quantity(self, listing_id, delta, actor):
        """Add or remove pieces safely, e.g. delta=-1 when one piece is sold.

        The current quantity is read from disk inside the lock, so two quick updates can
        never both start from the same old number.
        """
        delta = parse_whole_number(delta)
        if delta is None or delta == 0:
            raise ValidationError("The quantity change must be a non-zero whole number.")
        with _FILE_LOCK:
            records = self._read_records()
            index, record = self._find_for_owner(records, _text(listing_id), actor)
            new_quantity = record["quantity"] + delta
            if new_quantity < 0:
                raise ValidationError(f"Only {record['quantity']} piece(s) are left, so {abs(delta)} cannot be removed.")
            if new_quantity > MAX_QUANTITY:
                raise ValidationError(f"Quantity cannot exceed {MAX_QUANTITY}.")
            was_sold_out = record["quantity"] == 0
            record["quantity"] = new_quantity
            if new_quantity > 0 and was_sold_out and record["status"] == "Sold":
                record["status"] = STATUS_AVAILABLE  # restocked
            record["updated_at"] = now_iso()
            record = _apply_derived_fields(record)  # sets status to Sold at zero pieces
            records[index] = record
            self._write(records)
            return copy.deepcopy(record)

    def claim(self, listing_id, actor, is_admin=False):
        """Attach an ownerless old listing to the actor (see can_claim for the rules)."""
        if not actor:
            raise PermissionDenied("Please log in to claim a listing.")
        with _FILE_LOCK:
            records = self._read_records()
            for index, record in enumerate(records):
                if record["id"] == _text(listing_id):
                    if not self.can_claim(record, actor, is_admin):
                        raise PermissionDenied(
                            "This listing cannot be claimed by your account. The contact number on "
                            "your profile must match the listing's contact number."
                        )
                    record["owner_id"] = actor["id"]
                    record["owner_review_needed"] = False
                    record["updated_at"] = now_iso()
                    records[index] = record
                    self._write(records)
                    return copy.deepcopy(record)
            raise ListingNotFound(f"Listing {listing_id} was not found.")

    # ----- photo clean-up -----

    def _delete_unreferenced_photos(self, candidate_names, remaining_records):
        """Delete photo files that no listing refers to any more (only inside image_dir)."""
        still_used = {name for r in remaining_records for name in r.get("photos", [])}
        for name in candidate_names:
            safe_name = Path(str(name)).name
            if safe_name in still_used:
                continue
            path = self.image_dir / safe_name
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass  # a photo that cannot be deleted must not break the listing update

    # ----- export -----

    def export_json_bytes(self):
        """The whole inventory as pretty JSON, for download/backup."""
        return json.dumps(self.list_all(), indent=2, ensure_ascii=False).encode("utf-8")


# ==============================================================================
# PHOTOS: validation and saving
# ==============================================================================
# Saving and locating listing photos (local folder scrapfit_images/).
#
# Kept separate from data_store.py so the later move to Supabase Storage only changes
# this file. Existing photo names such as S9471E9.jpg keep working unchanged.

class PhotoError(Exception):
    """A photo could not be accepted. The message is safe to show to the user."""


def _check_image_bytes(data, filename):
    """Make sure the bytes are really an image (not just a renamed file)."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:
        raise PhotoError(f"'{filename}' is not a valid image file.") from exc


def save_uploaded_photos(uploaded_files, image_dir=None):
    """Validate and save listing photos locally or to Supabase Storage."""
    image_dir = Path(image_dir or IMAGE_DIR)
    files = list(uploaded_files or [])
    if len(files) > MAX_PHOTOS_PER_LISTING:
        raise PhotoError(f"You can upload at most {MAX_PHOTOS_PER_LISTING} photos per listing.")

    prepared = []
    for uploaded in files:
        suffix = Path(uploaded.name).suffix.lower()
        if suffix not in ALLOWED_PHOTO_SUFFIXES:
            raise PhotoError(f"'{uploaded.name}' must be a PNG, JPG or WEBP image.")
        data = uploaded.getvalue()
        if len(data) > MAX_PHOTO_BYTES:
            raise PhotoError(f"'{uploaded.name}' is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB.")
        _check_image_bytes(data, uploaded.name)
        prepared.append((suffix, data))

    saved = []
    if supabase_is_configured():
        bucket = get_supabase_client().storage.from_(SUPABASE_PHOTO_BUCKET)
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        try:
            for suffix, data in prepared:
                name = f"photo_{uuid.uuid4().hex[:12]}{suffix}"
                bucket.upload(f"photos/{name}", data, file_options={
                    "content-type": mime.get(suffix, "application/octet-stream"),
                    "cache-control": "3600", "upsert": "false",
                })
                saved.append(name)
        except Exception as exc:
            if saved:
                try:
                    bucket.remove([f"photos/{name}" for name in saved])
                except Exception:
                    pass
            raise PhotoError(f"Photos could not be uploaded to Supabase: {exc}") from exc
        return saved

    image_dir.mkdir(parents=True, exist_ok=True)
    try:
        for suffix, data in prepared:
            name = f"photo_{uuid.uuid4().hex[:12]}{suffix}"
            (image_dir / name).write_bytes(data)
            saved.append(name)
    except OSError as exc:
        remove_photo_files(saved, image_dir)
        raise PhotoError(f"Photos could not be saved: {exc}") from exc
    return saved

def remove_photo_files(names, image_dir=None):
    """Delete local files and, in cloud mode, their matching Storage objects."""
    image_dir = Path(image_dir or IMAGE_DIR)
    safe_names = [Path(str(name)).name for name in names]
    for name in safe_names:
        try:
            (image_dir / name).unlink()
        except OSError:
            pass
    if safe_names and supabase_is_configured():
        try:
            get_supabase_client().storage.from_(SUPABASE_PHOTO_BUCKET).remove(
                [f"photos/{name}" for name in safe_names]
            )
        except Exception:
            pass


def local_photo_paths(piece, image_dir=None):
    """Return local image paths or public Supabase URLs for listing photos."""
    image_dir = Path(image_dir or IMAGE_DIR)
    names = list(piece.get("photos") or [])
    if not names and piece.get("image_file"):
        names = [piece["image_file"]]
    paths = []
    for name in names:
        safe_name = Path(str(name)).name
        path = image_dir / safe_name
        if path.is_file() and path.suffix.lower() in ALLOWED_PHOTO_SUFFIXES:
            if supabase_is_configured():
                try:
                    # Existing local photos are copied to Storage on first use so other
                    # visitors can see them after the one-time JSON migration.
                    _upload_legacy_local_photo(safe_name, path.stat().st_mtime_ns)
                    public_url = get_supabase_client().storage.from_(SUPABASE_PHOTO_BUCKET).get_public_url(f"photos/{safe_name}")
                    if isinstance(public_url, dict):
                        public_url = public_url.get("publicUrl") or public_url.get("public_url")
                    if public_url:
                        paths.append(str(public_url))
                        continue
                except Exception:
                    pass
            paths.append(str(path))
            continue
        if supabase_is_configured() and Path(safe_name).suffix.lower() in ALLOWED_PHOTO_SUFFIXES:
            try:
                public_url = get_supabase_client().storage.from_(SUPABASE_PHOTO_BUCKET).get_public_url(f"photos/{safe_name}")
                if isinstance(public_url, dict):
                    public_url = public_url.get("publicUrl") or public_url.get("public_url")
                if public_url:
                    paths.append(str(public_url))
            except Exception:
                pass
    return paths


@st.cache_data(ttl=3600, show_spinner=False)
def _upload_legacy_local_photo(filename, modified_ns):
    """Upload an existing local photo to the cloud bucket once per file version."""
    path = IMAGE_DIR / Path(filename).name
    if not path.is_file():
        return False
    suffix = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix, "application/octet-stream")
    get_supabase_client().storage.from_(SUPABASE_PHOTO_BUCKET).upload(
        f"photos/{Path(filename).name}", path.read_bytes(),
        file_options={"content-type": mime, "cache-control": "3600", "upsert": "true"},
    )
    return True

# ==============================================================================
# ACCOUNTS: local users in users.json, password hashing
# ==============================================================================
# Local accounts for this development phase (users.json) - no Streamlit code in here.
#
# IMPORTANT - what this is and is not:
# * Passwords are never stored. Only a salted PBKDF2-SHA256 hash is saved.
# * It is meant for the local prototype. Online sign-in, e-mail verification and the secure
#   password-reset e-mail flow are planned for the Supabase phase and are NOT part of this
#   file. A password reset is deliberately not faked here.
# * The method names (create_user, authenticate, ...) are the contract a future
#   SupabaseAuth class would follow.

_USERS_LOCK = _shared_lock("users")

PBKDF2_ITERATIONS = 600_000
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 5
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class AuthError(Exception):
    """Login/registration problem. The message is safe to show to the user."""


# -------------------- PASSWORD HASHING --------------------

def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    """Return 'pbkdf2_sha256$iterations$salt$hash' (all hex). Never store the password."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored_hash):
    try:
        scheme, iterations, salt_hex, digest_hex = stored_hash.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)  # constant-time comparison
    except (ValueError, AttributeError):
        return False


# -------------------- INPUT VALIDATION --------------------

def normalise_email(email):
    return str(email or "").strip().lower()


def validate_password_strength(password, email=""):
    """Return a list of problems (empty when the password is acceptable)."""
    problems = []
    if len(password) < 8:
        problems.append("Password must be at least 8 characters long.")
    if len(password) > 128:
        problems.append("Password must be 128 characters or fewer.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        problems.append("Password must contain at least one letter and one number.")
    if email and password.strip().lower() == normalise_email(email):
        problems.append("Password must not be the same as your email address.")
    return problems


def validate_registration(name, email, password, confirm_password, phone=""):
    """Return a list of friendly error messages for the registration form."""
    errors = []
    name = str(name or "").strip()
    if len(name) < 2:
        errors.append("Please enter your full name (at least 2 characters).")
    elif len(name) > 80:
        errors.append("Your name must be 80 characters or fewer.")
    if not EMAIL_PATTERN.match(normalise_email(email)):
        errors.append("Please enter a valid email address.")
    errors.extend(validate_password_strength(password or "", email))
    if password != confirm_password:
        errors.append("The two passwords do not match.")
    if str(phone or "").strip() and not is_valid_phone(phone):
        errors.append("The contact number should have 8-15 digits (you may include +, spaces or dashes).")
    return errors


def public_profile(user):
    """The user fields that are safe to keep in the session and show on screen.

    Password hashes and login-attempt counters are never included.
    """
    verification = user.get("shop_verification") or {}
    status = verification.get("status", "not_requested")
    return {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "phone": user.get("phone", ""), "created_at": user.get("created_at"),
        # Only approved shop details are exposed in public profiles.
        "shop_verification_status": status,
        "verified_shop_name": verification.get("shop_name") if status == "verified" else None,
    }


# -------------------- USER REPOSITORY --------------------

class JsonUserRepository:
    """Users and their saved listings, stored in users.json."""

    def __init__(self, users_file=None):
        self.users_file = Path(users_file or USERS_FILE)

    # ----- storage -----

    def _load(self):
        if not self.users_file.exists():
            return []
        try:
            data = json.loads(self.users_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise AuthError(f"The accounts file {self.users_file.name} could not be read and was not changed.") from exc
        return data.get("users", []) if isinstance(data, dict) else []

    def _save(self, users):
        self.users_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.users_file.with_name(self.users_file.name + ".tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "users": users}, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)  # only the owner of the file may read it (where supported)
        except OSError:
            pass
        temporary.replace(self.users_file)

    # ----- registration and login -----

    def create_user(self, name, email, password, confirm_password, phone=""):
        """Register a new account and return its public profile."""
        errors = validate_registration(name, email, password, confirm_password, phone)
        if errors:
            raise AuthError(" ".join(errors))
        email = normalise_email(email)
        # Hash before taking the lock (hashing is slow and does not need the file).
        password_hash = hash_password(password)
        with _USERS_LOCK:
            users = self._load()
            if any(u["email"] == email for u in users):
                raise AuthError("An account with this email already exists. Please log in instead.")
            user = {
                "id": uuid.uuid4().hex, "name": name.strip(), "email": email,
                "phone": str(phone or "").strip(), "password_hash": password_hash,
                "created_at": now_iso(), "saved_ids": [],
                "failed_logins": 0, "locked_until": None,
            }
            users.append(user)
            self._save(users)
            return public_profile(user)

    def authenticate(self, email, password):
        """Return the public profile for a correct email/password, otherwise raise AuthError."""
        email = normalise_email(email)
        generic_error = "Incorrect email or password."
        if not email or not password:
            raise AuthError("Please enter your email and password.")
        with _USERS_LOCK:
            users = self._load()
            user = next((u for u in users if u["email"] == email), None)
            if user is None:
                hash_password(password)  # same work as a real check, so timing reveals nothing
                raise AuthError(generic_error)
            locked_until = user.get("locked_until")
            if locked_until and datetime.fromisoformat(locked_until) > datetime.now(timezone.utc):
                raise AuthError(f"Too many failed attempts. Please wait {LOCKOUT_MINUTES} minutes and try again.")
            if not verify_password(password, user["password_hash"]):
                user["failed_logins"] = user.get("failed_logins", 0) + 1
                if user["failed_logins"] >= MAX_FAILED_LOGINS:
                    user["locked_until"] = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                    user["failed_logins"] = 0
                self._save(users)
                raise AuthError(generic_error)
            user["failed_logins"] = 0
            user["locked_until"] = None
            self._save(users)
            return public_profile(user)

    # ----- profile -----

    def get_by_id(self, user_id):
        with _USERS_LOCK:
            user = next((u for u in self._load() if u["id"] == user_id), None)
        return public_profile(user) if user else None

    def get_by_email(self, email):
        email = normalise_email(email)
        with _USERS_LOCK:
            user = next((u for u in self._load() if u.get("email") == email), None)
        return public_profile(user) if user else None

    def update_profile(self, user_id, name, phone):
        """Change the display name and contact number."""
        name = str(name or "").strip()
        phone = str(phone or "").strip()
        errors = []
        if len(name) < 2 or len(name) > 80:
            errors.append("Please enter a name between 2 and 80 characters.")
        if phone and not is_valid_phone(phone):
            errors.append("The contact number should have 8-15 digits (you may include +, spaces or dashes).")
        if errors:
            raise AuthError(" ".join(errors))
        with _USERS_LOCK:
            users = self._load()
            user = next((u for u in users if u["id"] == user_id), None)
            if user is None:
                raise AuthError("Account not found.")
            user["name"], user["phone"] = name, phone
            self._save(users)
            return public_profile(user)

    def change_password(self, user_id, current_password, new_password, confirm_password):
        with _USERS_LOCK:
            users = self._load()
            user = next((u for u in users if u["id"] == user_id), None)
            if user is None:
                raise AuthError("Account not found.")
            if not verify_password(current_password or "", user["password_hash"]):
                raise AuthError("Your current password is incorrect.")
            problems = validate_password_strength(new_password or "", user["email"])
            if new_password != confirm_password:
                problems.append("The two new passwords do not match.")
            if problems:
                raise AuthError(" ".join(problems))
            user["password_hash"] = hash_password(new_password)
            self._save(users)

    # ----- shop verification -----

    def get_shop_verification(self, user_id):
        """Return the account owner's full verification request, including private notes."""
        with _USERS_LOCK:
            user = next((u for u in self._load() if u["id"] == user_id), None)
        return copy.deepcopy((user or {}).get("shop_verification") or {"status": "not_requested"})

    def request_shop_verification(self, user_id, shop_name, shop_area, proof_details):
        """Submit/update a manual shop-verification request. Approval is admin-only."""
        shop_name = str(shop_name or "").strip()
        shop_area = str(shop_area or "").strip()
        proof_details = str(proof_details or "").strip()
        problems = []
        if len(shop_name) < 2 or len(shop_name) > 100:
            problems.append("Enter a shop name between 2 and 100 characters.")
        if len(shop_area) < 4 or len(shop_area) > 200:
            problems.append("Enter the shop address or area (4-200 characters).")
        if len(proof_details) < 8 or len(proof_details) > 1000:
            problems.append("Add a short description of the business proof an admin can check (8-1000 characters).")
        if problems:
            raise AuthError(" ".join(problems))
        with _USERS_LOCK:
            users = self._load()
            user = next((u for u in users if u["id"] == user_id), None)
            if user is None:
                raise AuthError("Account not found.")
            current = user.get("shop_verification") or {}
            if current.get("status") == "verified":
                raise AuthError("This shop is already verified. Contact an admin if its details need changing.")
            user["shop_verification"] = {
                "status": "pending", "shop_name": shop_name, "shop_area": shop_area,
                "proof_details": proof_details, "requested_at": now_iso(),
                "reviewed_at": None, "reviewed_by": None, "admin_note": "",
            }
            self._save(users)
            return copy.deepcopy(user["shop_verification"])

    def list_pending_shop_verifications(self):
        """Return pending requests for the admin queue without exposing password data."""
        with _USERS_LOCK:
            users = self._load()
        pending = []
        for user in users:
            request = user.get("shop_verification") or {}
            if request.get("status") == "pending":
                pending.append({
                    "user_id": user.get("id"), "name": user.get("name"),
                    "email": user.get("email"), "phone": user.get("phone", ""),
                    **copy.deepcopy(request),
                })
        return pending

    def set_shop_verification_status(self, user_id, status, admin_user, admin_note=""):
        """Approve/reject a shop request. Only configured admin accounts may do this."""
        if not is_admin(admin_user):
            raise AuthError("Only a configured ScrapFit admin can review shop verification requests.")
        if status not in ("verified", "rejected"):
            raise AuthError("Invalid shop verification decision.")
        with _USERS_LOCK:
            users = self._load()
            user = next((u for u in users if u["id"] == user_id), None)
            if user is None:
                raise AuthError("Account not found.")
            request = user.get("shop_verification") or {}
            if request.get("status") != "pending":
                raise AuthError("This request is no longer pending.")
            request["status"] = status
            request["reviewed_at"] = now_iso()
            request["reviewed_by"] = admin_user.get("email")
            request["admin_note"] = str(admin_note or "").strip()[:500]
            user["shop_verification"] = request
            self._save(users)
            return copy.deepcopy(request)

    # ----- saved listings (favourites) -----

    def get_saved_ids(self, user_id):
        with _USERS_LOCK:
            user = next((u for u in self._load() if u["id"] == user_id), None)
        return list(user.get("saved_ids", [])) if user else []

    def set_saved(self, user_id, listing_id, saved):
        """Add (saved=True) or remove (saved=False) a favourite for this user only."""
        with _USERS_LOCK:
            users = self._load()
            user = next((u for u in users if u["id"] == user_id), None)
            if user is None:
                raise AuthError("Account not found.")
            ids = user.setdefault("saved_ids", [])
            if saved and listing_id not in ids:
                ids.append(listing_id)
            if not saved and listing_id in ids:
                ids.remove(listing_id)
            self._save(users)
            return list(ids)


# ==============================================================================
# COLLECTIONS, BUYER REVIEWS AND SELLER TRUST
# ==============================================================================
# Reviews are only possible for a buyer account linked to a seller-recorded collection,
# and the buyer must confirm that the handover happened before the review form unlocks.
# In this local JSON MVP, identity and contact details are not independently verified;
# a future cloud version should add a one-time pickup code or verified contact channel.

_TRANSACTIONS_LOCK = _shared_lock("transactions")


class TrustFeatureError(Exception):
    """A collection, review or trust-record action could not be completed."""


class JsonTransactionRepository:
    """Completed collections and buyer reviews stored in transactions.json."""

    def __init__(self, transactions_file=None):
        self.transactions_file = Path(transactions_file or TRANSACTIONS_FILE)

    def _load(self):
        if not self.transactions_file.exists():
            return {"schema_version": 1, "transactions": []}
        try:
            data = json.loads(self.transactions_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise TrustFeatureError(
                f"The {self.transactions_file.name} file could not be read. It was not changed."
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("transactions"), list):
            raise TrustFeatureError(f"{self.transactions_file.name} must contain a transactions list.")
        return data

    def _save(self, data):
        self.transactions_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.transactions_file.with_name(self.transactions_file.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.transactions_file)

    def record_collection(self, listing_id, seller, buyer_email, quantity, listing_repo, user_repo):
        """Seller records a completed handover; quantity is deducted and buyer is linked."""
        if not seller:
            raise TrustFeatureError("Please log in as the listing owner to record a collection.")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise TrustFeatureError("Collection quantity must be a whole number.")
        if quantity < 1:
            raise TrustFeatureError("Collection quantity must be at least one.")

        buyer_email = normalise_email(buyer_email)
        if not buyer_email:
            raise TrustFeatureError("Enter the buyer's registered email address.")
        buyer = user_repo.get_by_email(buyer_email)
        if not buyer:
            raise TrustFeatureError("No ScrapFit account uses that email. The buyer must register before a review can be linked.")
        if buyer["id"] == seller["id"]:
            raise TrustFeatureError("A seller cannot record themselves as the buyer.")

        with _TRANSACTIONS_LOCK:
            listing = listing_repo.get(listing_id)
            if not listing:
                raise TrustFeatureError("This listing no longer exists.")
            if listing.get("owner_id") != seller.get("id"):
                raise TrustFeatureError("Only the account that owns this listing can record its collection.")
            if listing.get("status") not in ("Available", "Reserved"):
                raise TrustFeatureError("Only an available or reserved listing can be collected.")
            if quantity > int(listing.get("quantity") or 0):
                raise TrustFeatureError(f"Only {listing.get('quantity', 0)} piece(s) are currently listed.")

            data = self._load()
            buyer_listing_records = [t for t in data["transactions"]
                                     if t.get("listing_id") == listing_id and t.get("buyer_id") == buyer["id"]]
            if any(t.get("review") for t in buyer_listing_records):
                raise TrustFeatureError("This buyer has already reviewed this listing. Only one review per buyer and listing is allowed.")
            if any(t.get("status") != "Disputed by buyer" for t in buyer_listing_records):
                raise TrustFeatureError("This buyer already has a collection record for this listing. Resolve it first; a new record is allowed only after a disputed entry.")
            if any(t.get("listing_id") == listing_id and t.get("status") == "Awaiting buyer confirmation"
                   for t in data["transactions"]):
                raise TrustFeatureError("Another collection for this listing is waiting for buyer confirmation. Complete or resolve it first.")

            transaction = {
                "id": uuid.uuid4().hex,
                "listing_id": listing_id,
                "listing_name": listing.get("name", "Scrap listing"),
                "listing_snapshot": {
                    "material": listing.get("material"),
                    "material_type": listing.get("material_type"),
                    "length": listing.get("length"),
                    "width": listing.get("width"),
                    "thickness": listing.get("thickness"),
                    "quality_grade": listing.get("quality_grade"),
                    "condition": listing.get("condition"),
                    "area": listing.get("area"),
                },
                "seller_id": seller["id"],
                "seller_name": listing.get("seller_name") or seller.get("verified_shop_name") or seller.get("name"),
                "buyer_id": buyer["id"],
                "buyer_name": buyer.get("name"),
                "buyer_email": buyer.get("email"),
                "quantity": quantity,
                "quantity_before_collection": int(listing.get("quantity") or 0),
                "listing_status_before_collection": listing.get("status"),
                "status": "Awaiting buyer confirmation",
                "recorded_at": now_iso(),
                "recorded_by": seller.get("id"),
                "review": None,
            }
            data["transactions"].append(transaction)
            self._save(data)
            try:
                # Reserve the listing while waiting for the buyer; do not deduct stock yet.
                if listing.get("status") == "Available":
                    listing_repo.update(listing_id, {"status": "Reserved"}, seller)
            except Exception:
                rollback = self._load()
                rollback["transactions"] = [t for t in rollback["transactions"] if t.get("id") != transaction["id"]]
                self._save(rollback)
                raise
            return copy.deepcopy(transaction)

    def pending_collection_confirmations(self, buyer_id):
        """Collections entered by a seller that the buyer has not confirmed yet."""
        with _TRANSACTIONS_LOCK:
            items = copy.deepcopy(self._load()["transactions"])
        return [t for t in items if t.get("buyer_id") == buyer_id
                and t.get("status") == "Awaiting buyer confirmation"]

    def confirm_collection(self, transaction_id, buyer, listing_repo, collected=True):
        """Only the recorded buyer can confirm/dispute; quantity is deducted on confirmation."""
        if not buyer:
            raise TrustFeatureError("Please log in with the buyer account recorded for this collection.")
        with _TRANSACTIONS_LOCK:
            data = self._load()
            transaction = next((t for t in data["transactions"] if t.get("id") == transaction_id), None)
            if not transaction:
                raise TrustFeatureError("The collection record could not be found.")
            if transaction.get("buyer_id") != buyer.get("id"):
                raise TrustFeatureError("Only the buyer recorded for this collection can confirm it.")
            if transaction.get("status") != "Awaiting buyer confirmation":
                raise TrustFeatureError("This collection has already been confirmed or disputed.")

            seller = get_user_repo().get_by_id(transaction.get("seller_id"))
            if not seller:
                raise TrustFeatureError("The seller account for this collection could not be found.")
            listing = listing_repo.get(transaction.get("listing_id"))
            if not listing or listing.get("owner_id") != seller.get("id"):
                raise TrustFeatureError("The original listing is no longer available to update. Contact the seller.")

            if collected:
                if listing.get("status") not in ("Available", "Reserved"):
                    raise TrustFeatureError("The listing is no longer available for collection confirmation. Contact the seller to resolve it.")
                quantity = int(transaction.get("quantity") or 0)
                if quantity < 1 or int(listing.get("quantity") or 0) < quantity:
                    raise TrustFeatureError("The listing quantity changed and is no longer sufficient. Contact the seller before confirming.")
                transaction["status"] = "Buyer confirmed"
                transaction["buyer_confirmed_at"] = now_iso()
                self._save(data)
                try:
                    listing_repo.adjust_quantity(transaction["listing_id"], -quantity, seller)
                except Exception as exc:
                    rollback = self._load()
                    for item in rollback["transactions"]:
                        if item.get("id") == transaction_id:
                            item["status"] = "Awaiting buyer confirmation"
                            item.pop("buyer_confirmed_at", None)
                    self._save(rollback)
                    raise TrustFeatureError(f"Could not update the listing stock, so the confirmation was not completed: {exc}") from exc
            else:
                # If this request placed an Available listing on hold, restore it when denied.
                if (transaction.get("listing_status_before_collection") == "Available"
                        and listing.get("status") == "Reserved"
                        and int(listing.get("quantity") or 0) == int(transaction.get("quantity_before_collection") or 0)):
                    listing_repo.update(transaction["listing_id"], {"status": "Available"}, seller)
                transaction["status"] = "Disputed by buyer"
                transaction["buyer_confirmed_at"] = now_iso()
                self._save(data)
            return copy.deepcopy(transaction)

    def pending_reviews_for_buyer(self, buyer_id):
        with _TRANSACTIONS_LOCK:
            items = copy.deepcopy(self._load()["transactions"])
        return [t for t in items if t.get("buyer_id") == buyer_id
                and t.get("status") == "Buyer confirmed" and not t.get("review")]

    def submit_review(self, transaction_id, buyer, material_match, dimensions_match,
                      condition_match, pickup_smooth, overall_rating, comment=""):
        if not buyer:
            raise TrustFeatureError("Please log in with the buyer account that collected this item.")
        if material_match not in ("Yes", "Mostly", "No"):
            raise TrustFeatureError("Choose how closely the material matched the listing.")
        if dimensions_match not in ("As listed", "Slightly off", "Way off"):
            raise TrustFeatureError("Choose how closely the dimensions matched the listing.")
        if condition_match not in ("Yes", "No") or pickup_smooth not in ("Yes", "No"):
            raise TrustFeatureError("Complete the condition and pickup questions.")
        try:
            stars = int(overall_rating)
        except (TypeError, ValueError):
            stars = 0
        if stars not in range(1, 6):
            raise TrustFeatureError("Select an overall rating from 1 to 5 stars.")
        comment = str(comment or "").strip()
        if len(comment) > 500:
            raise TrustFeatureError("Keep the comment under 500 characters.")

        with _TRANSACTIONS_LOCK:
            data = self._load()
            transaction = next((t for t in data["transactions"] if t.get("id") == transaction_id), None)
            if not transaction:
                raise TrustFeatureError("The collection record could not be found.")
            if transaction.get("buyer_id") != buyer.get("id"):
                raise TrustFeatureError("Only the buyer recorded for this collection can review it.")
            if transaction.get("status") != "Buyer confirmed":
                raise TrustFeatureError("A review is available only after you confirm the collection in your account.")
            if transaction.get("review"):
                raise TrustFeatureError("A review has already been submitted for this collection.")
            transaction["review"] = {
                "material_match": material_match,
                "dimensions_match": dimensions_match,
                "condition_match": condition_match,
                "pickup_smooth": pickup_smooth,
                "overall_rating": stars,
                "comment": comment,
                "submitted_at": now_iso(),
            }
            self._save(data)
            return copy.deepcopy(transaction["review"])

    def seller_summary(self, seller_id):
        with _TRANSACTIONS_LOCK:
            items = copy.deepcopy(self._load()["transactions"])
        reviews = [t["review"] for t in items
                   if t.get("seller_id") == seller_id and isinstance(t.get("review"), dict)]
        count = len(reviews)
        if not count:
            return {"count": 0, "average": None, "accuracy": None, "badge": "New seller", "reviews": []}
        average = sum(r.get("overall_rating", 0) for r in reviews) / count
        # A review counts as fully accurate only when all three listing facts were exact.
        exact = sum(1 for r in reviews if r.get("material_match") == "Yes"
                    and r.get("dimensions_match") == "As listed"
                    and r.get("condition_match") == "Yes")
        accuracy = 100.0 * exact / count
        if count >= 10 and average >= 4.5 and accuracy >= 90:
            label = "Top Trusted"
        elif count >= 3 and average >= 4.0 and accuracy >= 80:
            label = "Verified Reliable"
        elif count < 3:
            label = "New seller"
        else:
            label = "Building trust"
        return {"count": count, "average": average, "accuracy": accuracy,
                "badge": label, "reviews": reviews}

    def reviews_for_seller(self, seller_id, limit=10):
        with _TRANSACTIONS_LOCK:
            items = copy.deepcopy(self._load()["transactions"])
        reviewed = [t for t in items if t.get("seller_id") == seller_id and t.get("review")]
        reviewed.sort(key=lambda t: (t.get("review") or {}).get("submitted_at", ""), reverse=True)
        return reviewed[:limit]



# ==============================================================================
# SUPABASE CLOUD STORAGE (JSON-compatible migration layer)
# ==============================================================================
# This first cloud migration keeps the existing Streamlit UI and repository contract.
# The three existing JSON documents (inventory, users, transactions) are stored as
# separate JSONB rows in Supabase. On first use, any existing local JSON file is
# imported automatically. The app's existing PBKDF2 login remains in use for now;
# this is NOT Supabase Auth and is intended as a migration-friendly prototype.

SUPABASE_DOCUMENTS_TABLE = "scrapfit_documents"
SUPABASE_PHOTO_BUCKET = "listing-photos"


def _supabase_config():
    """Read server-only Supabase settings from Streamlit secrets or environment."""
    try:
        section = st.secrets.get("supabase", {})
        url = str(section.get("url", "") or st.secrets.get("SUPABASE_URL", "")).strip()
        secret = str(
            section.get("secret_key", "")
            or section.get("service_role_key", "")
            or st.secrets.get("SUPABASE_SECRET_KEY", "")
            or st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
        ).strip()
    except Exception:
        url = os.environ.get("SUPABASE_URL", "").strip()
        secret = (os.environ.get("SUPABASE_SECRET_KEY", "")
                  or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
    return url, secret


def supabase_is_configured():
    url, secret = _supabase_config()
    return bool(url and secret)


@st.cache_resource(show_spinner=False)
def get_supabase_client():
    """One server-side client. The secret key must never be exposed to browsers."""
    url, secret = _supabase_config()
    if not url or not secret:
        raise RuntimeError(
            "Supabase is not configured. Add [supabase] url and secret_key to "
            ".streamlit/secrets.toml, or remove the section to use local JSON files."
        )
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("Install the Supabase Python package with: pip install supabase") from exc
    return create_client(url, secret)


class SupabaseDocumentStore:
    """Small persistence adapter for the app's existing JSON-shaped repositories."""

    def __init__(self, client=None):
        self.client = client or get_supabase_client()

    def read(self, key):
        try:
            response = (self.client.table(SUPABASE_DOCUMENTS_TABLE)
                        .select("payload").eq("doc_key", key).limit(1).execute())
            rows = response.data or []
            return copy.deepcopy(rows[0]["payload"]) if rows else None
        except Exception as exc:
            raise RuntimeError(
                f"Could not read '{key}' from Supabase. Check that the SQL setup script "
                f"has been run and the secret key is correct. Details: {exc}"
            ) from exc

    def write(self, key, payload):
        try:
            self.client.table(SUPABASE_DOCUMENTS_TABLE).upsert({
                "doc_key": key,
                "payload": payload,
                "updated_at": now_iso(),
            }, on_conflict="doc_key").execute()
        except Exception as exc:
            raise RuntimeError(
                f"Could not save '{key}' to Supabase. Check the table and API settings. Details: {exc}"
            ) from exc


@st.cache_resource(show_spinner=False)
def get_supabase_document_store():
    return SupabaseDocumentStore(get_supabase_client())


class SupabaseListingRepository(JsonListingRepository):
    """Cloud-backed implementation of the current listing repository contract."""

    def _read_raw(self):
        payload = get_supabase_document_store().read("inventory")
        if payload is not None:
            if not isinstance(payload, list):
                raise InventoryFileError("The Supabase inventory document must contain a JSON list.")
            return payload
        # One-time import from the local inventory.json (or demo inventory if absent).
        payload = super()._read_raw()
        get_supabase_document_store().write("inventory", payload)
        return payload

    def _write(self, records):
        get_supabase_document_store().write("inventory", records)

    def create_backup(self, label="manual"):
        records = self._read_raw()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.backup_dir / f"inventory_{label}_{stamp}.json"
        target.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    def _delete_unreferenced_photos(self, candidate_names, remaining_records):
        still_used = {name for r in remaining_records for name in r.get("photos", [])}
        to_remove = [Path(str(name)).name for name in candidate_names if Path(str(name)).name not in still_used]
        # Continue cleaning this machine's local cache/files as well.
        super()._delete_unreferenced_photos(candidate_names, remaining_records)
        if to_remove and supabase_is_configured():
            try:
                get_supabase_client().storage.from_(SUPABASE_PHOTO_BUCKET).remove(
                    [f"photos/{name}" for name in to_remove]
                )
            except Exception:
                pass  # A failed photo cleanup must not undo a listing update.


class SupabaseUserRepository(JsonUserRepository):
    """Cloud-backed users document; password hashes remain server-side."""

    def _load(self):
        payload = get_supabase_document_store().read("users")
        if payload is None:
            payload = super()._load()  # migrate the existing users.json on first use
            get_supabase_document_store().write("users", payload)
        if not isinstance(payload, list):
            raise AuthError("The Supabase users document has an invalid format.")
        return payload

    def _save(self, users):
        get_supabase_document_store().write("users", users)


class SupabaseTransactionRepository(JsonTransactionRepository):
    """Cloud-backed collections and reviews document."""

    def _load(self):
        payload = get_supabase_document_store().read("transactions")
        if payload is None:
            payload = super()._load()  # migrate transactions.json on first use
            get_supabase_document_store().write("transactions", payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("transactions"), list):
            raise TrustFeatureError("The Supabase transactions document has an invalid format.")
        return payload

    def _save(self, data):
        get_supabase_document_store().write("transactions", data)

def get_transaction_repo():
    """Return the cloud repository when configured; otherwise retain local JSON mode."""
    return SupabaseTransactionRepository() if supabase_is_configured() else JsonTransactionRepository()


@st.cache_data(ttl=5, show_spinner=False)
def _cached_seller_summary(seller_id, file_mtime_ns, file_size):
    """Avoid re-reading and re-scoring the same review file for every listing card."""
    return get_transaction_repo().seller_summary(seller_id)


def get_seller_trust_summary(seller_id):
    """Cache trust summaries briefly; file metadata invalidates the cache after updates."""
    try:
        stat = TRANSACTIONS_FILE.stat()
        modified, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        modified, size = 0, 0
    return _cached_seller_summary(str(seller_id), modified, size)


# ============================================================================== 
# MATCHING ENGINE: fit checks, cut-plan allocation, explanations, cost estimate
# ==============================================================================
# ScrapFit matching engine - plain Python, no Streamlit, easy to unit test.
#
# What the matcher does (and does not do)
# ---------------------------------------
# * A part fits a piece when material and thickness match EXACTLY and the piece is at least
#   part-size + allowance on every edge. Rotation by 90 degrees is always allowed.
# * Only listings with status "Available" and quantity >= 1 are considered.
# * A listing with quantity 3 is treated as THREE separate physical pieces. A physical piece
#   is never given to the same plan twice over, but several small parts may share one piece
#   when a simple "shelf" layout fits them.
# * Smaller suitable offcuts are preferred, and pieces already in use are reused first.
#
# This is a simple, explainable heuristic. It does NOT guarantee a globally optimal cutting
# plan, and it ignores blade width (kerf) and grain direction.

# Quality grades from best to worst. A higher number is better.
GRADE_RANK = {"A": 3, "B": 2, "C": 1}


def grade_rank(piece):
    return GRADE_RANK.get(str(piece.get("quality_grade", "")).upper(), 0)


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def listing_is_available(piece):
    """True for listings that normal search and matching may use."""
    return piece.get("status", "Available") == "Available" and _int(piece.get("quantity", 1)) >= 1


def meets_minimum_grade(piece, min_grade):
    """min_grade None/'Any' accepts everything; 'B' accepts grades A and B; 'A' only A."""
    if not min_grade or min_grade == "Any":
        return True
    return grade_rank(piece) >= GRADE_RANK[min_grade]


# -------------------- SINGLE-PART FIT CHECK --------------------

def check_fit(piece, material, length, width, thickness, allowance_per_side=5,
              min_grade=None, require_available=True):
    """Decide whether ONE required part could be cut from ONE listing.

    Returns a dict:
        fits       - True/False
        rotated    - True when the part only fits after turning it 90 degrees
        reason     - short code explaining a rejection (None when it fits)
        waste_area - unused surface area in mm2 (None when it does not fit)
    Reason codes: status, material, thickness, too_small, grade.
    """
    if require_available and not listing_is_available(piece):
        return {"fits": False, "rotated": False, "reason": "status", "waste_area": None}
    if piece.get("material") != material:
        return {"fits": False, "rotated": False, "reason": "material", "waste_area": None}
    if _int(piece.get("thickness")) != _int(thickness):
        return {"fits": False, "rotated": False, "reason": "thickness", "waste_area": None}

    cut_length = _int(length) + 2 * allowance_per_side
    cut_width = _int(width) + 2 * allowance_per_side
    piece_length, piece_width = _int(piece.get("length")), _int(piece.get("width"))
    fits_normal = piece_length >= cut_length and piece_width >= cut_width
    fits_rotated = piece_length >= cut_width and piece_width >= cut_length
    if not fits_normal and not fits_rotated:
        return {"fits": False, "rotated": False, "reason": "too_small", "waste_area": None}
    if not meets_minimum_grade(piece, min_grade):
        return {"fits": False, "rotated": False, "reason": "grade", "waste_area": None}
    return {
        "fits": True, "rotated": not fits_normal, "reason": None,
        # Waste is measured against the footprint reserved for the part plus allowance.
        "waste_area": piece_length * piece_width - cut_length * cut_width,
    }


def find_matches(material, length, width, thickness, inventory, allowance_per_side=5,
                 min_grade=None, require_available=True):
    """Find listings that could supply one part, smallest suitable offcut first.

    `allowance_per_side` is added on every edge, so the piece must be at least
    length + 2*allowance by width + 2*allowance (or the same turned 90 degrees).
    Each result is a copy of the listing with `rotated`, `waste_area`,
    `allowance_per_side` and `match_notes` (a plain-English explanation) added.
    """
    matches = []
    for piece in inventory:
        result = check_fit(piece, material, length, width, thickness, allowance_per_side,
                           min_grade, require_available)
        if not result["fits"]:
            continue
        match = piece.copy()
        match["rotated"] = result["rotated"]
        match["waste_area"] = result["waste_area"]
        match["allowance_per_side"] = allowance_per_side
        match["match_notes"] = explain_fit(piece, material, length, width, thickness,
                                           allowance_per_side, result["rotated"], min_grade)
        matches.append(match)
    # Least remaining area first: the smallest suitable offcut is preferred.
    matches.sort(key=lambda m: (m["waste_area"], _int(m.get("length")) * _int(m.get("width"))))
    return matches


# -------------------- EXPLANATIONS --------------------

def explain_fit(piece, material, length, width, thickness, allowance, rotated, min_grade=None):
    """Plain-English reasons why `piece` can supply a part of the given size."""
    need_l, need_w = _int(length) + 2 * allowance, _int(width) + 2 * allowance
    notes = [
        f"Material matches ({material}).",
        f"Thickness matches exactly ({_int(thickness)} mm).",
        f"The {_int(piece.get('length'))} × {_int(piece.get('width'))} mm piece is large enough for the "
        f"{_int(length)} × {_int(width)} mm part plus {allowance} mm on every edge "
        f"({need_l} × {need_w} mm needed)"
        + (", when the part is turned 90°." if rotated else "."),
    ]
    grade = str(piece.get("quality_grade", "?"))
    if min_grade and min_grade != "Any":
        notes.append(f"Quality Grade {grade} meets your minimum of Grade {min_grade}.")
    else:
        notes.append(f"Quality: Grade {grade}.")
    notes.append(f"Available quantity: {_int(piece.get('quantity', 1))} piece(s).")
    return notes


def explain_unmatched(part, material, inventory, allowance_per_side=5, min_grade=None):
    """Explain why NO piece was assigned to `part`, using the listings that were checked.

    Works by trying every listing against the part on its own and counting the reasons.
    """
    counts = {"status": 0, "material": 0, "thickness": 0, "too_small": 0, "grade": 0, "fits": 0}
    thicknesses, largest_area, largest = set(), 0, None
    for piece in inventory:
        result = check_fit(piece, material, part["length"], part["width"], part["thickness"],
                           allowance_per_side, min_grade, require_available=True)
        counts[result["reason"] or "fits"] += 1
        if listing_is_available(piece) and piece.get("material") == material:
            thicknesses.add(_int(piece.get("thickness")))
            if _int(piece.get("thickness")) == _int(part["thickness"]):
                area = _int(piece.get("length")) * _int(piece.get("width"))
                if area > largest_area:
                    largest_area, largest = area, piece

    reasons = []
    if counts["fits"]:
        reasons.append(
            f"{counts['fits']} listing(s) are big enough for this part, but every physical piece "
            "was already assigned to other parts in this plan."
        )
    if counts["grade"]:
        reasons.append(f"{counts['grade']} listing(s) fit but are below your minimum quality (Grade {min_grade}).")
    if counts["too_small"]:
        detail = ""
        if largest is not None:
            detail = f" The largest one is {_int(largest.get('length'))} × {_int(largest.get('width'))} mm."
        reasons.append(
            f"{counts['too_small']} {material.lower()} listing(s) have the right thickness but are too small "
            f"for {_int(part['length'])} × {_int(part['width'])} mm plus {allowance_per_side} mm per edge.{detail}"
        )
    if not (counts["too_small"] or counts["grade"] or counts["fits"]):
        if thicknesses:
            options = ", ".join(f"{t} mm" for t in sorted(thicknesses))
            reasons.append(
                f"No available {material.lower()} listing is {_int(part['thickness'])} mm thick "
                f"(thickness must match exactly). Available thicknesses: {options}."
            )
        else:
            reasons.append(f"There are no available {material.lower()} listings.")
    if counts["status"]:
        reasons.append(f"{counts['status']} listing(s) were skipped because they are Reserved, Sold or Unavailable.")
    return reasons


# -------------------- PROJECT ALLOCATION (multiple parts) --------------------

def expand_part_quantities(parts):
    """Expand quantity into individual required pieces for the one-piece-at-a-time allocator."""
    expanded = []
    for part in parts:
        quantity = int(part.get("quantity", 1) or 1)
        for index in range(quantity):
            item = {key: value for key, value in part.items() if key != "id"}
            item["quantity"] = 1
            if quantity > 1:
                item["name"] = f"{part['name']} ({index + 1}/{quantity})"
            expanded.append(item)
    return expanded


def allocate_project_parts(material, parts, inventory, allowance_per_side=5, min_grade=None, pickup_strategy="smallest"):
    """Plan a simple 2-D shelf cutting layout across available offcuts.

    Several required parts may be cut from one physical offcut. Listing quantity means
    the number of separate physical offcuts available. Parts must match material and
    thickness exactly; rotation is allowed. Each part gets allowance on all edges.
    Only Available listings that meet `min_grade` are used. `pickup_strategy` may be
    `smallest` (original waste-first behaviour) or `fewest_locations` (prefer a location
    that can cover the most requirements, then keep assignments within already-selected
    pickup locations where possible).

    This deterministic shelf-packing heuristic is intentionally explainable; it is not
    a guarantee of globally optimal nesting and does not model blade kerf or grain.
    Returns (required_part, [assigned_stock_piece]) pairs, one per required part.
    Each assigned piece is a copy of the listing with cut-plan fields added.
    """
    # Create one bin per physical offcut. A listing with quantity=3 creates three bins.
    bins = []
    for listing_index, source in enumerate(inventory):
        if not listing_is_available(source) or not meets_minimum_grade(source, min_grade):
            continue
        quantity = max(0, min(100, int(source.get("quantity", 1) or 0)))
        for unit_index in range(quantity):
            source_id = source.get("id", f"listing-{listing_index}")
            unit_key = f"{source_id}::unit-{unit_index + 1}"
            bins.append({
                "unit_key": unit_key,
                "listing_index": listing_index,
                "unit_index": unit_index + 1,
                "piece": source.copy(),
                "rows": [],
                "used_width": 0,
                "assigned": [],
            })

    def location_key(piece):
        """Normalised pickup area used to reduce the number of collection stops."""
        area = str(piece.get("area") or "").strip()
        place = str(piece.get("pickup_location") or "").strip()
        return (area or place or "Location not specified").casefold()

    # In location-aware mode, start with the area that can independently supply
    # the greatest number of different requirements. This avoids choosing an
    # arbitrary tiny offcut's area as the first collection point.
    preferred_location = None
    if pickup_strategy == "fewest_locations" and bins:
        location_coverage = {}
        for bin_state in bins:
            key = location_key(bin_state["piece"])
            coverage = location_coverage.setdefault(key, set())
            for part_index, part in enumerate(parts):
                stock = bin_state["piece"]
                if (stock.get("material") == material
                        and int(stock.get("thickness", 0) or 0) == int(part.get("thickness", 0) or 0)):
                    need_l = int(part.get("length", 0) or 0) + 2 * int(allowance_per_side)
                    need_w = int(part.get("width", 0) or 0) + 2 * int(allowance_per_side)
                    stock_l = int(stock.get("length", 0) or 0)
                    stock_w = int(stock.get("width", 0) or 0)
                    if ((stock_l >= need_l and stock_w >= need_w)
                            or (stock_l >= need_w and stock_w >= need_l)):
                        coverage.add(part_index)
        if location_coverage:
            preferred_location = max(
                location_coverage,
                key=lambda key: (len(location_coverage[key]), key),
            )

    def fit_options(bin_state, part):
        stock = bin_state["piece"]
        if stock.get("material") != material:
            return []
        if int(stock.get("thickness", 0) or 0) != int(part.get("thickness", 0) or 0):
            return []
        stock_length = int(stock.get("length", 0) or 0)
        stock_width = int(stock.get("width", 0) or 0)
        need_l = int(part.get("length", 0) or 0) + 2 * int(allowance_per_side)
        need_w = int(part.get("width", 0) or 0) + 2 * int(allowance_per_side)
        if need_l <= 0 or need_w <= 0:
            return []

        orientations = [(need_l, need_w, False), (need_w, need_l, True)]
        # Try placing into existing shelves first, preferring a close fit.
        options = []
        for row_index, row in enumerate(bin_state["rows"]):
            for use_l, use_w, rotated in orientations:
                if use_l <= stock_length - row["used_length"] and use_w <= row["height"]:
                    options.append({
                        "kind": "existing", "row_index": row_index,
                        "length": use_l, "width": use_w, "rotated": rotated,
                        "score": (0, row["height"] - use_w,
                                  stock_length - row["used_length"] - use_l),
                    })
        if options:
            return sorted(options, key=lambda option: option["score"])

        # Otherwise create another shelf across the stock width.
        for use_l, use_w, rotated in orientations:
            if use_l <= stock_length and bin_state["used_width"] + use_w <= stock_width:
                options.append({
                    "kind": "new", "row_index": len(bin_state["rows"]),
                    "length": use_l, "width": use_w, "rotated": rotated,
                    "score": (1, use_w, stock_width - bin_state["used_width"] - use_w),
                })
        return sorted(options, key=lambda option: option["score"])

    def apply_placement(bin_state, part, option, part_index):
        if option["kind"] == "new":
            bin_state["rows"].append({
                "height": option["width"], "used_length": 0, "parts": []
            })
            bin_state["used_width"] += option["width"]
        row = bin_state["rows"][option["row_index"]]
        placement = {
            "part_index": part_index,
            "part_name": part.get("name", "Part"),
            "length_with_allowance": option["length"],
            "width_with_allowance": option["width"],
            "rotated": option["rotated"],
            "row_index": option["row_index"] + 1,
        }
        row["parts"].append(placement)
        row["used_length"] += option["length"]
        bin_state["assigned"].append(placement)

    # Largest pieces first improves the chance of fitting demanding parts, while
    # already-opened stock is preferred for smaller pieces so its cost is shared.
    indexed_parts = list(enumerate(parts))
    indexed_parts.sort(
        key=lambda pair: (
            -(int(pair[1].get("length", 0) or 0) * int(pair[1].get("width", 0) or 0)),
            pair[0],
        )
    )

    assigned_by_index = {}
    for part_index, part in indexed_parts:
        eligible = []
        for bin_state in bins:
            options = fit_options(bin_state, part)
            if not options:
                continue
            stock = bin_state["piece"]
            stock_area = int(stock.get("length", 0) or 0) * int(stock.get("width", 0) or 0)
            # Reusing an already-purchased physical offcut avoids buying another listing.
            # If opening a new offcut, prefer smaller area then lower price.
            opened = bool(bin_state["assigned"])
            if pickup_strategy == "fewest_locations":
                used_locations = {location_key(item["piece"]) for item in bins if item["assigned"]}
                location = location_key(stock)
                score = (
                    0 if location in used_locations else 1,
                    0 if location == preferred_location else 1,
                    0 if opened else 1,
                    options[0]["score"],
                    0 if opened else stock_area,
                    0 if opened else int(stock.get("price", 0) or 0),
                    bin_state["listing_index"],
                    bin_state["unit_index"],
                )
            else:
                score = (
                    0 if opened else 1,
                    options[0]["score"],
                    0 if opened else stock_area,
                    0 if opened else int(stock.get("price", 0) or 0),
                    bin_state["listing_index"],
                    bin_state["unit_index"],
                )
            eligible.append((score, bin_state, options[0]))
        if not eligible:
            assigned_by_index[part_index] = None
            continue
        _, chosen_bin, chosen_option = min(eligible, key=lambda item: item[0])
        apply_placement(chosen_bin, part, chosen_option, part_index)
        assigned_by_index[part_index] = chosen_bin

    # Decorate assignment copies with a stable physical-offcut key and cut-plan notes.
    results = []
    for part_index, part in enumerate(parts):
        chosen_bin = assigned_by_index.get(part_index)
        if chosen_bin is None:
            results.append((part, []))
            continue
        stock_copy = chosen_bin["piece"].copy()
        stock_copy["cut_plan_unit_key"] = chosen_bin["unit_key"]
        stock_copy["cut_plan_unit_number"] = chosen_bin["unit_index"]
        stock_copy["cut_plan_part_count"] = len(chosen_bin["assigned"])
        stock_copy["cut_plan_part_names"] = [entry["part_name"] for entry in chosen_bin["assigned"]]
        stock_copy["cut_plan_placement"] = next(
            entry for entry in chosen_bin["assigned"] if entry["part_index"] == part_index
        )
        stock_copy["rotated"] = stock_copy["cut_plan_placement"]["rotated"]
        stock_copy["waste_area"] = max(
            0,
            int(stock_copy.get("length", 0) or 0) * int(stock_copy.get("width", 0) or 0)
            - stock_copy["cut_plan_placement"]["length_with_allowance"]
            * stock_copy["cut_plan_placement"]["width_with_allowance"],
        )
        stock_copy["cut_plan_remaining_width"] = max(
            0,
            int(stock_copy.get("width", 0) or 0) - chosen_bin["used_width"],
        )
        stock_copy["match_notes"] = explain_fit(
            chosen_bin["piece"], material, part.get("length", 0), part.get("width", 0),
            part.get("thickness", 0), int(allowance_per_side),
            stock_copy["rotated"], min_grade,
        ) + (
            [f"This is physical piece {chosen_bin['unit_index']} of "
             f"{_int(chosen_bin['piece'].get('quantity', 1))} in the listing."]
        ) + (
            [f"It is shared with: {', '.join(n for n in stock_copy['cut_plan_part_names'] if n != part.get('name', 'Part'))}."]
            if len(chosen_bin["assigned"]) > 1 else []
        )
        results.append((part, [stock_copy]))
    return results


def summarize_allocation(results):
    """Count how many required pieces were assigned and how many physical pieces are used."""
    assigned = [(part, pieces[0]) for part, pieces in results if pieces]
    unmatched = [part for part, pieces in results if not pieces]
    physical_units = {piece["cut_plan_unit_key"] for _, piece in assigned}
    return {
        "assigned": assigned, "unmatched": unmatched,
        "assigned_count": len(assigned), "unmatched_count": len(unmatched),
        "physical_pieces_used": len(physical_units),
    }


# -------------------- COST ESTIMATE --------------------

def project_cost_summary(parts, inventory, material, allowance=5, min_grade=None):
    """Estimate new-material area cost and whole-offcut cost after shared-stock cutting.

    New-material equivalent is a rough area-based proxy from listing prices.
    Scrap cost counts each physical offcut used once, even when several parts are cut
    from it. Cutting patterns use a simple shelf-packing heuristic, not industrial nesting.
    """
    # Expand per-part quantities so cost and cutting estimates cover every required piece.
    parts = expand_part_quantities(parts)
    available_inventory = [item for item in inventory if listing_is_available(item)]
    unit_rates = []
    for item in available_inventory:
        if item.get("material") == material:
            item_length = int(item.get("length", 0) or 0)
            item_width = int(item.get("width", 0) or 0)
            if item_length > 0 and item_width > 0:
                unit_rates.append(float(item.get("price", 0) or 0) / (item_length * item_width))
    rate = min(unit_rates) if unit_rates else 0

    new_total = sum(
        round(int(part.get("length", 0)) * int(part.get("width", 0)) * rate)
        for part in parts
    )
    allocations = allocate_project_parts(
        material, parts, available_inventory, allowance_per_side=allowance, min_grade=min_grade
    )

    scrap_total = 0
    counted_units = set()
    breakdown = []
    for part, assigned in allocations:
        area = int(part.get("length", 0)) * int(part.get("width", 0))
        estimate = round(area * rate)
        match = assigned[0] if assigned else None
        scrap_price = None
        scrap_note = "No suitable cut plan" if match is None else ""
        if match is not None:
            unit_key = match.get("cut_plan_unit_key")
            if unit_key not in counted_units:
                scrap_price = int(match.get("price", 0) or 0)
                scrap_total += scrap_price
                counted_units.add(unit_key)
                scrap_note = "Whole offcut cost counted once"
            else:
                scrap_price = 0
                scrap_note = "Same physical offcut; no extra listing cost"
        breakdown.append({
            "part": part.get("name", "Part"),
            "new_estimate": estimate,
            "scrap_price": scrap_price,
            "scrap_note": scrap_note,
            "match": match,
        })
    return breakdown, new_total, scrap_total


# ==============================================================================
# SEARCH: filters and sorting for Find Scrap
# ==============================================================================
# Filtering and sorting for the Find Scrap page - plain Python, easy to test.
#
# All filters are combined with AND, so adding another filter can only narrow the results.

SORT_RECENT = "Recently added"
SORT_PRICE_LOW = "Lowest price"
SORT_PRICE_HIGH = "Highest price"
SORT_BEST_FIT = "Best dimensional fit"
SORT_QUALITY = "Best quality"
SORT_NEAREST = "Nearest location"
SORT_OPTIONS = [SORT_RECENT, SORT_PRICE_LOW, SORT_PRICE_HIGH, SORT_BEST_FIT, SORT_QUALITY, SORT_NEAREST]


def listing_places(piece):
    """The pickup place labels a listing can be found under (area and pickup address)."""
    return {str(v).strip().casefold() for v in (piece.get("area"), piece.get("pickup_location")) if v and str(v).strip()}


def filter_listings(inventory, *, statuses=("Available",), material=None, material_type=None,
                    grades=None, min_price=0, max_price=None, place=None,
                    min_length=0, max_length=0, min_width=0, max_width=0,
                    min_thickness=0, max_thickness=0):
    """Return the listings that satisfy EVERY filter that was given.

    statuses       - which availability statuses to include (default: Available only)
    material       - category, e.g. "Wood"; material_type - e.g. "Plywood"
    grades         - list such as ["A", "B"]; None means any grade
    place          - exact pickup place label (case-insensitive); None = everywhere
    max_* = 0      - means "no upper limit"
    """
    results = []
    for piece in inventory:
        status = piece.get("status", "Available")
        if status not in statuses:
            continue
        if status == "Available" and not listing_is_available(piece):
            continue  # "Available" with zero pieces left is not really available
        if material and str(piece.get("material", "")).casefold() != material.casefold():
            continue
        if material_type and str(piece.get("material_type", "")).casefold() != material_type.casefold():
            continue
        if grades is not None and str(piece.get("quality_grade", "")).upper() not in grades:
            continue
        price = piece.get("price", 0) or 0
        if price < min_price or (max_price is not None and price > max_price):
            continue
        if place and place.casefold() not in listing_places(piece):
            continue
        length, width, thickness = _int(piece.get("length")), _int(piece.get("width")), _int(piece.get("thickness"))
        if length < min_length or width < min_width or thickness < min_thickness:
            continue
        if (max_length and length > max_length) or (max_width and width > max_width) or (max_thickness and thickness > max_thickness):
            continue
        results.append(piece)
    return results


# -------------------- DISTANCE --------------------

def listing_coordinates(piece):
    """(lat, lon) of a listing, or None when there is no reliable pin.

    The original app always saved a default 'demo' pin, which says nothing about where
    the scrap really is, so that pin is treated as 'no location'.
    """
    coords = piece.get("coordinates")
    if not isinstance(coords, dict):
        return None
    try:
        lat, lon = float(coords["lat"]), float(coords["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if (round(lat, 4), round(lon, 4)) == DEMO_PIN:
        return None
    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2):
    """Straight-line distance over the Earth's surface in kilometres."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi, d_lambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def count_listings_with_location(inventory):
    return sum(listing_coordinates(p) is not None for p in inventory)


# -------------------- SORTING --------------------

def sort_listings(results, sort_by, user_location=None):
    """Return a new, sorted list. Items that lack the sort data are placed last.

    'Best dimensional fit' needs `waste_area`, which only exists after a Dimensions
    search; 'Nearest location' needs `user_location=(lat, lon)` and listings with real pins.
    """
    items = [dict(piece) for piece in results]  # copies, so the stored results stay untouched

    if sort_by == SORT_PRICE_LOW:
        items.sort(key=lambda p: (p.get("price") or 0, p.get("name", "")))
    elif sort_by == SORT_PRICE_HIGH:
        items.sort(key=lambda p: (-(p.get("price") or 0), p.get("name", "")))
    elif sort_by == SORT_BEST_FIT:
        items.sort(key=lambda p: (p.get("waste_area") is None, p.get("waste_area") or 0))
    elif sort_by == SORT_QUALITY:
        items.sort(key=lambda p: (-grade_rank(p), p.get("price") or 0))
    elif sort_by == SORT_NEAREST and user_location:
        for piece in items:
            coords = listing_coordinates(piece)
            piece["distance_km"] = haversine_km(*user_location, *coords) if coords else None
        items.sort(key=lambda p: (p["distance_km"] is None, p["distance_km"] or 0))
    else:  # Recently added (also the fallback)
        # Newest first; listings without a created date (old records) go last.
        items.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
        items.sort(key=lambda p: p.get("created_at") is None)
    return items


# ==============================================================================
# QR CODES: public listing links and QR PNGs
# ==============================================================================
# Public listing links and QR codes.
#
# A QR code simply contains the public address of one listing:
#
#     <APP_BASE_URL>/?listing=<LISTING_ID>
#
# Listing IDs never change when a listing is edited, so a printed QR code keeps working.
# The base URL must be the DEPLOYED address (not localhost) before codes are printed.

def build_listing_url(listing_id, base_url=None):
    """The public link for one listing, e.g. https://scrapfit.example.com/?listing=S001"""
    base = (base_url or get_public_base_url()).rstrip("/")
    return f"{base}/?listing={quote(str(listing_id), safe='')}"


def _add_caption(image, caption):
    """Place a small text caption (the listing ID) under the QR image for printing."""
    from PIL import Image, ImageDraw
    caption_height = 34
    canvas = Image.new("RGB", (image.width, image.height + caption_height), "white")
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)
    text = str(caption)
    left, top, right, bottom = draw.textbbox((0, 0), text)  # default font; works without extra files
    draw.text(((canvas.width - (right - left)) / 2, image.height + (caption_height - (bottom - top)) / 2 - 2),
              text, fill="black")
    return canvas


def make_qr_png(url, caption=None, box_size=10):
    """Return PNG bytes of a QR code for `url`, with an optional caption underneath."""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise RuntimeError("The 'qrcode' package is missing. Run: pip install -r requirements.txt") from exc

    builder = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box_size, border=4)
    builder.add_data(url)
    builder.make(fit=True)
    qr_image = builder.make_image(fill_color="black", back_color="white")
    # Newer versions wrap the Pillow image; get_image() unwraps it.
    pil_image = (qr_image.get_image() if hasattr(qr_image, "get_image") else qr_image).convert("RGB")
    if caption:
        pil_image = _add_caption(pil_image, caption)
    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG")
    return buffer.getvalue()


@st.cache_data(show_spinner=False)
def cached_qr_png(url, caption):
    """QR images are cached so they are not rebuilt on every click."""
    return make_qr_png(url, caption=caption)


def render_listing_qr(listing_id):
    """Show the listing's QR code and public link. Used inside every listing card, so the QR is
    visible to everyone (guests included). Downloading the PNG is in the owner's management panel."""
    url = build_listing_url(listing_id)
    try:
        png = cached_qr_png(url, str(listing_id))
    except RuntimeError as exc:  # the qrcode package is not installed
        st.warning(f"QR code unavailable: {exc}")
    else:
        st.image(png, width=160)  # 160 px keeps the code and its ID caption fully visible on phones
        st.caption("Scan this QR code to open this listing.")
    if is_local_url(url):
        st.caption("Note: this link points to this computer only. Set APP_BASE_URL (see README) so other people can scan it.")
    st.caption("Public link to this listing:")
    st.code(url, language=None)


# ==============================================================================
# STOCK IMAGES: illustrative photos used when a listing has none
# ==============================================================================
# Illustrative stock photos used when a listing has no photo of its own.
#
# URLs and mappings are copied unchanged from the original app (including the photos
# assigned to listings S001-S007, S9471E9 and S1537AE).

WOOD_PLYWOOD_URL = "https://c.pxhere.com/photos/92/19/workshop_work_tool_wood_business-20011.jpg!d"
WOOD_PINE_URL = "https://c.pxhere.com/photos/3b/70/workshop_lumber_iron_metal_wood_tool_old_dirty-900855.jpg!d"
WOOD_PANEL_URL = "https://c.pxhere.com/photos/92/19/workshop_work_tool_wood_business-20011.jpg!d"
METAL_SHEET_URL = "https://images.unsplash.com/photo-1656285523078-b77363624310?auto=format&fit=crop&w=1000&q=80"
METAL_OFFCUT_URL = "https://images.unsplash.com/photo-1656285523078-b77363624310?auto=format&fit=crop&w=1000&q=80"
METAL_PLATE_URL = "https://c.pxhere.com/photos/86/86/tools_old_oxide_wood_iron-665653.jpg!d"
METAL_RODS_URL = "https://tiimg.tistatic.com/fp/1/008/429/reliable-12-mm-solid-galvanized-finished-hot-rolled-iron-rods-329.jpg"

LISTING_IMAGES = {
    "S001": WOOD_PLYWOOD_URL,
    "S002": WOOD_PINE_URL,
    "S003": WOOD_PANEL_URL,
    "S004": WOOD_PINE_URL,
    "S005": METAL_SHEET_URL,
    "S006": METAL_OFFCUT_URL,
    "S007": METAL_PLATE_URL,
    "S9471E9": WOOD_PLYWOOD_URL,
    "S1537AE": METAL_RODS_URL,
}

STOCK_IMAGES = {"Wood": WOOD_PLYWOOD_URL, "Metal": METAL_SHEET_URL}

IDEA_IMAGES = {
    "Wall shelf": "https://media.homeboxstores.com/i/homebox/160595463-WS-04NSONOMA_02-2100.jpg?%24prodimg-m-sqr-pdp-2x%24=&%24quality-standard%24=&fmt=auto&sm=c",
    "Desk organiser": "https://www.kasahara-net.jp/media/001/202506/mode3_w820-%E3%83%9A%E3%83%B3%E7%AB%8B%E3%81%A6%E4%BB%98%E3%81%8D%E5%B0%8F%E7%89%A9%E5%85%A5%E3%82%8C.jpg",
    "Metal tool stand": "https://i.ebayimg.com/images/g/yyoAAOSwBuRjKVxH/s-l960.jpg",
    "Small planter stand": "https://media2.castorama.pl/is/image/CastoramaPL/stojak-na-doniczke-goodhome-drewniany-12-cm~3663602442356_21c?%24MOB_PREV%24=&%24height=600&%24width=600",
    "Metal parts tray": "https://eclipsemagnetics.shop/cdn/shop/articles/ChatGPT_Image_Dec_22_2025_12_17_46_PM_aeed89d1-e34f-44b1-9805-c85182f51a0b.png?v=1771849753",
}


def material_image(piece_or_material):
    """Return the image to show for a listing or a material name.

    Order: the seller's own uploaded photo (first one), then the illustrative photo
    assigned to that listing ID, then a generic stock photo for the material.
    """
    if isinstance(piece_or_material, dict):
        own_photos = local_photo_paths(piece_or_material)
        if own_photos:
            return own_photos[0]
        return LISTING_IMAGES.get(
            str(piece_or_material.get("id", "")),
            STOCK_IMAGES.get(piece_or_material.get("material", "Wood"), STOCK_IMAGES["Wood"]),
        )
    return STOCK_IMAGES.get(piece_or_material, STOCK_IMAGES["Wood"])


def has_own_photo(piece):
    return bool(local_photo_paths(piece))


# ==============================================================================
# OPTIONAL AI PROJECT DRAFTING (Gemini)
# ==============================================================================
# Optional Gemini-powered project drafting (unchanged from the original app, apart from reading
# secrets lazily). Needs GEMINI_API_KEY; without it the manual planner still works.

GEMINI_DEFAULT_MODEL = "gemini-3.1-flash-lite"


def get_gemini_api_key():
    """Read the Gemini key from Streamlit secrets or the environment; never hard-code it."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    try:
        import streamlit as st
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
        if secret_key:
            key = str(secret_key).strip()
    except Exception:
        # st.secrets raises when no local secrets file exists; environment variables still work.
        pass
    return key


def generate_ai_project_draft(description, material):
    """Ask Gemini for an editable, approximate rectangular-parts draft (not engineering advice)."""
    api_key = get_gemini_api_key()
    if not api_key:
        raise ValueError("Gemini API key is not configured.")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("The google-genai package is missing. Install the updated requirements.txt.") from exc

    model = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL
    prompt = f"""You help draft simple maker projects for a material-reuse app. Produce a preliminary cut list, not a certified engineering design.
User's project description: {description}
Primary material selected by the user: {material}

Return ONLY valid JSON with this structure:
{{
  "title": "short project title",
  "parts": [
    {{"name": "clear part name", "length_mm": 300, "width_mm": 100, "thickness_mm": 18, "quantity": 2, "reason": "brief purpose"}}
  ],
  "design_notes": ["short practical consideration"],
  "assumptions": ["dimensions are approximate starter estimates"]
}}

Requirements:
- Use only the selected primary material ({material}) for every part.
- Keep the cut list to 1–10 distinct rectangular pieces that could plausibly be cut from sheet/board/flat-stock offcuts.
- Use millimetres and integer dimensions. Length and width must be 1–10000 mm; thickness 1–500 mm; quantity 1–20.
- Choose sensible beginner-level, approximate dimensions where the user did not specify them. Include key panels, supports, legs or brackets where plausible, but do not overcomplicate.
- Do not claim the draft is structurally safe, code-compliant, or construction-ready. Add a note to verify stability, joinery, material grade, tool suitability and safety before building.
- Do not invent prices, inventory matches, seller details, or availability. ScrapFit's deterministic matcher handles inventory fits separately.
- Keep notes concise. JSON only; no Markdown fences or extra text."""

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
            max_output_tokens=1800,
        ),
    )
    response_text = getattr(response, "text", None)
    if not response_text:
        raise ValueError("Gemini returned an empty response.")
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        # Defensive handling for SDK/model responses that wrap JSON in code fences.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text.strip(), flags=re.IGNORECASE)
        payload = json.loads(cleaned)

    if not isinstance(payload, dict) or not isinstance(payload.get("parts"), list):
        raise ValueError("The AI response did not contain a valid parts list.")
    if not 1 <= len(payload["parts"]) <= 10:
        raise ValueError("The AI draft must contain between 1 and 10 distinct parts. Please try a simpler description.")

    validated_parts = []
    total_pieces = 0
    for raw in payload["parts"]:
        if not isinstance(raw, dict):
            raise ValueError("One of the AI-generated parts was malformed. Generate the draft again.")
        name = str(raw.get("name", "")).strip()[:100]
        if not name:
            raise ValueError("A generated part did not have a name. Generate the draft again.")
        try:
            length = int(raw.get("length_mm"))
            width = int(raw.get("width_mm"))
            thickness = int(raw.get("thickness_mm"))
            quantity = int(raw.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValueError("The AI returned non-numeric dimensions. Generate the draft again.")
        if not (1 <= length <= 10000 and 1 <= width <= 10000 and 1 <= thickness <= 500 and 1 <= quantity <= 20):
            raise ValueError("The AI returned dimensions or quantity outside the allowed range. Generate the draft again.")
        total_pieces += quantity
        validated_parts.append({
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "length": length,
            "width": width,
            "thickness": thickness,
            "quantity": quantity,
            "reason": str(raw.get("reason", "")).strip()[:240],
        })
    if total_pieces > 30:
        raise ValueError("The AI draft contains too many physical pieces. Reduce the project scope and try again.")

    return {
        "title": str(payload.get("title", "Custom project")).strip()[:100] or "Custom project",
        "material": material,
        "parts": validated_parts,
        "design_notes": [str(x).strip()[:240] for x in payload.get("design_notes", []) if str(x).strip()][:6],
        "assumptions": [str(x).strip()[:240] for x in payload.get("assumptions", []) if str(x).strip()][:6],
        "source_description": description.strip(),
    }


# ==============================================================================
# SESSION HELPERS: current user, guest mode, flash messages, saved listings
# ==============================================================================
# Streamlit session helpers: repositories, current user, guest mode, flash messages.
#
# This is the only module (besides the views) that touches st.session_state for login, so
# the rules "who is logged in" and "what may a guest do" live in one place.

PAGES = ["Home", "Find Scrap", "Project Planner", "Project Ideas", "List Scrap",
         "Inventory", "Save for Later", "Account / Profile"]


# -------------------- REPOSITORIES (one shared instance per server) --------------------

def get_listing_repo():
    """Use Supabase when configured, otherwise keep the original local JSON backend."""
    repo_class = SupabaseListingRepository if supabase_is_configured() else JsonListingRepository
    return repo_class(sample_inventory=SAMPLE_INVENTORY)


def get_user_repo():
    """Use shared cloud accounts when Supabase is configured."""
    return SupabaseUserRepository() if supabase_is_configured() else JsonUserRepository()


def get_inventory():
    """Fresh copy of all listings from inventory.json (so every session sees current data)."""
    return get_listing_repo().list_all()


# -------------------- WHO IS USING THE APP --------------------

def current_user():
    """The logged-in user's public profile, or None for a guest.

    Only the user ID is kept in the session. The profile is re-read every run, so profile
    edits show up immediately and a deleted account is logged out automatically.
    """
    user_id = st.session_state.get("user_id")
    if not user_id:
        return None
    profile = get_user_repo().get_by_id(user_id)
    if profile is None:
        st.session_state.pop("user_id", None)
    return profile


def is_admin(user):
    return bool(user and user["email"].lower() in get_admin_emails())


def log_in(profile):
    """Start a logged-in session and keep any listings the person saved while a guest."""
    st.session_state["user_id"] = profile["id"]
    st.session_state["guest_mode"] = False
    repo = get_user_repo()
    for listing_id in st.session_state.get("saved_ids", []):
        repo.set_saved(profile["id"], listing_id, True)
    st.session_state["saved_ids"] = []


def log_out():
    """End the session and return to the welcome screen. Temporary planner results are cleared."""
    for key in list(st.session_state.keys()):
        if key not in ("main_navigation",):
            del st.session_state[key]
    st.session_state["main_navigation"] = "Home"


def continue_as_guest():
    st.session_state["guest_mode"] = True


def navigate_to(target_page):
    """Button callback: switch page. Runs before widgets are created, so it is safe."""
    st.session_state["main_navigation"] = target_page


def open_account_page(tab="login"):
    st.session_state["account_tab_hint"] = tab
    st.session_state["main_navigation"] = "Account / Profile"


# -------------------- FEEDBACK MESSAGES --------------------

def flash(kind, message):
    """Queue a message to show on the next run (survives st.rerun()). kind: success|info|warning|error."""
    st.session_state.setdefault("flash_messages", []).append((kind, message))


def render_flashes():
    for kind, message in st.session_state.pop("flash_messages", []):
        {"success": st.success, "info": st.info, "warning": st.warning, "error": st.error}.get(kind, st.info)(message)


def require_login(action_text):
    """Friendly prompt for guests. Returns True when the user is logged in and may continue."""
    if current_user():
        return True
    with st.container(border=True):
        st.markdown(f"### 🔒 Log in to {action_text}")
        st.write(
            "You can keep browsing, searching and planning projects as a guest. "
            f"To {action_text}, please log in or create a free account."
        )
        left, right = st.columns(2)
        left.button("Log in", key=f"gate_login_{action_text}", use_container_width=True,
                    on_click=open_account_page, args=("login",))
        right.button("Create account", key=f"gate_register_{action_text}", use_container_width=True,
                     on_click=open_account_page, args=("register",))
    return False


# -------------------- SAVED LISTINGS (favourites) --------------------

def get_saved_ids():
    """Registered users: saved in users.json. Guests: temporary, for this visit only."""
    user = current_user()
    if user:
        return get_user_repo().get_saved_ids(user["id"])
    return list(st.session_state.get("saved_ids", []))


def set_saved(listing_id, saved):
    user = current_user()
    if user:
        get_user_repo().set_saved(user["id"], listing_id, saved)
        return
    ids = st.session_state.setdefault("saved_ids", [])
    if saved and listing_id not in ids:
        ids.append(listing_id)
    if not saved and listing_id in ids:
        ids.remove(listing_id)


def toggle_saved(listing_id):
    """Button callback for the Save / Saved button on listing cards."""
    now_saved = listing_id not in get_saved_ids()
    set_saved(listing_id, now_saved)
    if not now_saved:
        st.toast("Removed from saved listings")
    elif current_user():
        st.toast("Saved to your account")
    else:
        st.toast("Saved for this visit only. Log in to keep favourites permanently.")


# ==============================================================================
# SHARED UI: the single listing-card renderer used on every page
# ==============================================================================
# Reusable Streamlit pieces. The listing card lives here so EVERY page shows listings the
# same way: Find Scrap, Inventory, Project Planner, Project Ideas, Saved listings and the
# public listing page all call render_scrap_listing().

GRADE_CLASS = {"A": "sf-badge-a", "B": "sf-badge-b", "C": "sf-badge-c"}
STATUS_CLASS = {"Available": "sf-badge-available", "Reserved": "sf-badge-reserved",
                "Sold": "sf-badge-sold", "Unavailable": "sf-badge-unavailable"}


def whatsapp_url(phone):
    """Build a WhatsApp URL only when a seller supplied a usable phone number."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 8:
        return None
    return f"https://wa.me/{digits}"


def tel_url(phone):
    cleaned = re.sub(r"[^\d+]", "", phone or "")
    return f"tel:{cleaned}" if len(re.sub(r"\D", "", cleaned)) >= 8 else None


def badge(text, css_class):
    return f'<span class="sf-badge {css_class}">{escape(str(text))}</span>'


def grade_badge(piece):
    grade = str(piece.get("quality_grade", "?"))
    return badge(f"Grade {grade}", GRADE_CLASS.get(grade, "sf-badge-c"))


def status_badge(piece):
    status = str(piece.get("status", "Available"))
    return badge(status, STATUS_CLASS.get(status, "sf-badge-unavailable"))


def grade_help_text(grade):
    return f"Grade {grade}: {GRADE_DESCRIPTIONS.get(grade, '')}"


def dimensions_text(piece):
    return f"{piece.get('length', '?')} × {piece.get('width', '?')} × {piece.get('thickness', '?')} mm"


def format_price(value):
    if isinstance(value, float):
        return f"₹{value:,.2f}"
    if isinstance(value, int):
        return f"₹{value:,}"
    return f"₹{value if value not in (None, '') else '—'}"


def pickup_text(piece):
    return piece.get("area") or piece.get("pickup_location") or "Not specified"


def render_contact_links(piece):
    """Seller contact options. Guests may see these too."""
    phone = piece.get("contact_phone", "")
    links = []
    if whatsapp_url(phone):
        links.append(f"[💬 WhatsApp](<{whatsapp_url(phone)}>)")
    if tel_url(phone):
        links.append(f"[📞 Call]({tel_url(phone)})")
    if links:
        seller = piece.get("seller_name")
        st.markdown(("**" + seller + "** · " if seller else "") + " · ".join(links))
    else:
        st.caption("Seller contact not provided.")


def render_scrap_listing(piece, key_prefix="scrap", show_save=False, show_match=False,
                         expanded=False):
    """Render one listing card.

    Shows: photo, name, dimensions + thickness, quality grade, availability, quantity,
    price, pickup location, listing ID and seller contact. The expander adds the full
    description, all photos and the public link.

    show_save   - show the Save for Later button
    show_match  - show the "why this matches" explanation when the piece carries match_notes
    """
    piece_id = str(piece.get("id", piece.get("name", "listing")))
    with st.container(border=True):
        photo_col, info_col, price_col = st.columns([1.1, 3.2, 1.0], gap="medium")
        with photo_col:
            st.image(material_image(piece), use_container_width=True)
            st.caption("Seller photo" if has_own_photo(piece) else "Illustrative photo")
        with info_col:
            st.markdown(f"**{piece.get('name', 'Unnamed offcut')}**")
            st.markdown(
                grade_badge(piece) + status_badge(piece)
                + badge(f"Qty {piece.get('quantity', 1)}", "sf-badge-neutral"),
                unsafe_allow_html=True,
            )
            if piece.get("owner_id"):
                seller_profile = get_user_repo().get_by_id(piece.get("owner_id"))
                if seller_profile and seller_profile.get("shop_verification_status") == "verified":
                    st.markdown(badge("Verified Shop", "sf-badge-verified"), unsafe_allow_html=True)
                    if seller_profile.get("verified_shop_name"):
                        st.caption(f"Shop: {seller_profile['verified_shop_name']}")
                trust = get_seller_trust_summary(piece.get("owner_id"))
                if trust["count"]:
                    trust_badges = badge(trust["badge"], "sf-badge-trusted" if trust["badge"] in ("Verified Reliable", "Top Trusted") else "sf-badge-neutral")
                    st.markdown(trust_badges, unsafe_allow_html=True)
                    st.caption(f"★ {trust['average']:.1f}/5 · Listing accuracy: {trust['accuracy']:.0f}% · {trust['count']} buyer-confirmed review(s)")
                else:
                    st.caption("New seller · no buyer reviews yet")
            kind = f" · {piece['material_type']}" if piece.get("material_type") else ""
            st.caption(f"{piece.get('material', 'Material unspecified')}{kind} · {dimensions_text(piece)}")
            st.caption(f"Thickness: {piece.get('thickness', '?')} mm · Pickup: {pickup_text(piece)}")
            if piece.get("distance_km") is not None:
                st.caption(f"About {piece['distance_km']:.1f} km away (straight line)")
            render_contact_links(piece)
        with price_col:
            st.metric("Price", format_price(piece.get("price", "—")))
            st.caption(f"ID: {piece_id}")

        if show_match and piece.get("match_notes"):
            with st.expander("Why this listing matches", expanded=False):
                for note in piece["match_notes"]:
                    st.write(f"- {note}")
                if piece.get("rotated"):
                    st.caption("Fits when the part is rotated 90°.")

        with st.expander("View full listing details", expanded=expanded):
            st.write(f"**Material:** {piece.get('material', 'Not specified')}"
                     + (f" ({piece['material_type']})" if piece.get("material_type") else ""))
            st.write(f"**Dimensions:** {dimensions_text(piece)}")
            st.write(f"**Quality:** {grade_help_text(piece.get('quality_grade', '?'))}")
            st.write(f"**Availability:** {piece.get('status', 'Available')}")
            st.write(f"**Quantity:** {piece.get('quantity', 1)} piece(s)")
            if piece.get("notes"):
                st.write(f"**Description:** {piece['notes']}")
            if piece.get("seller_name"):
                st.write(f"**Seller / shop:** {piece['seller_name']}")
            st.write(f"**Pickup area:** {piece.get('area') or 'Not provided'}")
            if piece.get("pickup_location"):
                st.write(f"**Pickup address / landmark:** {piece['pickup_location']}")
            if piece.get("pickup_details"):
                st.write(f"**Pickup details:** {piece['pickup_details']}")
            if piece.get("contact_phone"):
                st.write(f"**Contact:** {piece['contact_phone']}")
            own = local_photo_paths(piece)
            if len(own) > 1:
                st.image(own, width=160)
            listed = piece.get("created_at") or piece.get("date_added")
            st.caption(f"Listed: {str(listed)[:10] if listed else 'date not recorded'}"
                       + (f" · Updated: {str(piece['updated_at'])[:10]}" if piece.get("updated_at") and piece.get("created_at") else ""))
            render_listing_qr(piece_id)

        if show_save:
            saved = piece_id in get_saved_ids()
            st.button("♥ Saved" if saved else "♡ Save for Later", key=f"{key_prefix}_save_{piece_id}",
                      on_click=toggle_saved, args=(piece_id,))


# ==============================================================================
# PAGE: sign-in forms (shared)
# ==============================================================================
# Login and registration forms, shared by the welcome screen and the Account page.

def _after_login(profile, message):
    log_in(profile)
    flash("success", message)
    st.session_state["pending_navigation"] = "Home"
    st.rerun()


def render_login_form(key_prefix):
    with st.form(f"{key_prefix}_login_form"):
        email = st.text_input("Email address", key=f"{key_prefix}_login_email")
        password = st.text_input("Password", type="password", key=f"{key_prefix}_login_password")
        submitted = st.form_submit_button("Log in", type="primary", use_container_width=True)
    if submitted:
        try:
            profile = get_user_repo().authenticate(email, password)
        except AuthError as exc:
            st.error(str(exc))  # e.g. "Incorrect email or password."
        else:
            _after_login(profile, f"Login successful. Welcome back, {profile['name']}!")
    st.caption(
        "Forgot your password? Secure password recovery by email will be added together with "
        "online accounts (Supabase Authentication) in the next development phase. It is not "
        "available in this local version."
    )


def render_register_form(key_prefix):
    with st.form(f"{key_prefix}_register_form"):
        name = st.text_input("Full name", key=f"{key_prefix}_reg_name")
        email = st.text_input("Email address", key=f"{key_prefix}_reg_email")
        phone = st.text_input(
            "Contact number (optional)", placeholder="Include country code, e.g. +91...",
            key=f"{key_prefix}_reg_phone",
            help="Buyers can use this to reach you when you list scrap. It also lets you claim older listings that use the same number.",
        )
        password = st.text_input("Password", type="password", key=f"{key_prefix}_reg_password",
                                 help="At least 8 characters, with a letter and a number.")
        confirm = st.text_input("Confirm password", type="password", key=f"{key_prefix}_reg_confirm")
        submitted = st.form_submit_button("Create account", type="primary", use_container_width=True)
    if submitted:
        try:
            profile = get_user_repo().create_user(name, email, password, confirm, phone)
        except AuthError as exc:
            st.error(str(exc))
        else:
            _after_login(profile, "Account created successfully. You are now logged in.")


# ==============================================================================
# PAGE: Welcome (Continue as Guest / Log in / Create Account)
# ==============================================================================
# Landing screen: Continue as Guest, Log in or Create Account.

def _choose(mode):
    st.session_state["welcome_mode"] = mode


def render_welcome_page():
    with st.container(key="home_hero"):  # reuses the original hero styling
        copy_col, art_col = st.columns([1.2, 0.9], vertical_alignment="center")
        with copy_col:
            st.markdown('<div class="sf-eyebrow">♻ Project-based matching for reusable materials</div>', unsafe_allow_html=True)
            st.markdown('<div class="sf-hero-title">Welcome to ScrapFit</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="sf-hero-copy">Find the next use for leftover wood and metal. Browse listings and plan '
                'projects right away as a guest, or create an account to list your own scrap.</div>',
                unsafe_allow_html=True,
            )
        with art_col:
            if HERO_IMAGE.exists():
                st.image(str(HERO_IMAGE), use_container_width=True)

        st.markdown("### How would you like to start?")
        guest_col, login_col, register_col = st.columns(3)
        guest_col.button("Continue as Guest", key="welcome_guest", use_container_width=True, on_click=continue_as_guest)
        login_col.button("Log in", key="welcome_login", use_container_width=True, on_click=_choose, args=("login",))
        register_col.button("Create Account", key="welcome_register", use_container_width=True, on_click=_choose, args=("register",))

    mode = st.session_state.get("welcome_mode")
    if mode == "login":
        with st.container(border=True):
            st.subheader("Log in")
            render_login_form("welcome")
    elif mode == "register":
        with st.container(border=True):
            st.subheader("Create your account")
            render_register_form("welcome")

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown('<div class="sf-card"><h3>As a guest you can</h3><p>• Browse and search all available listings<br>• Use the Project Planner and see matching results<br>• View listing details, photos and seller contact information<br>• Open public listing links and scanned QR codes</p></div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="sf-card"><h3>With an account you can also</h3><p>• Create, edit and delete your own scrap listings<br>• Update quantities, availability and photos<br>• Download QR codes for your listings<br>• Keep saved favourites permanently</p></div>', unsafe_allow_html=True)


# ==============================================================================
# PAGE: Home
# ==============================================================================
# Home page. Content and layout are the original ScrapFit home page; only the figures and
# the "current build / next steps" text were updated to match the upgraded app.

# -------------------- HOME PAGE --------------------

def render_home_page():
    inventory = get_inventory()
    with st.container(key="home_hero"):
        hero_copy, hero_art = st.columns([1.2, 0.9], vertical_alignment="center")
        with hero_copy:
            st.markdown('<div class="sf-eyebrow">♻ Project-based matching for reusable materials</div>', unsafe_allow_html=True)
            st.markdown('<div class="sf-hero-title">Good materials deserve a second life.</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="sf-hero-copy">ScrapFit helps people find a next use for leftover wood and metal. Describe the parts a project needs, compare them with available offcuts, and explore local pickup options before buying new material.</div>',
                unsafe_allow_html=True,
            )
            st.markdown('<div class="sf-hero-kicker">Project-first matching · Cutting-plan assistance · Local reuse</div>', unsafe_allow_html=True)
        with hero_art:
            hero_image = HERO_IMAGE
            if hero_image.exists():
                st.image(str(hero_image), use_container_width=True)
            else:
                st.warning("Place scrapfit_hero_photo.png beside app.py to display the workshop photo.")

        st.markdown("### Start with what you need")
        st.markdown("Search for a suitable piece, plan a project from its parts list, or list leftover material for someone else to use.")
        action1, action2, action3 = st.columns(3)
        with action1:
            st.button("⌕  Find Scrap", key="home_find_materials", use_container_width=True,
                      on_click=navigate_to, args=("Find Scrap",))
        with action2:
            st.button("▱  Plan a Project", key="home_plan_project", use_container_width=True,
                      on_click=navigate_to, args=("Project Planner",))
        with action3:
            st.button("＋  List Scrap", key="home_add_listing", use_container_width=True,
                      on_click=navigate_to, args=("List Scrap",))

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-kicker">Material workspace</div><div class="sf-section-title">See what is available to explore</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-copy">These figures count the listings currently available in this app. Sample listings that came with the demo data are illustrative; they are not verified live offers from real sellers.</div>', unsafe_allow_html=True)
    open_listings = [p for p in inventory if listing_is_available(p)]
    total_pieces = len(open_listings)
    wood_pieces = sum(p.get("material") == "Wood" for p in open_listings)
    metal_pieces = sum(p.get("material") == "Metal" for p in open_listings)
    metric1, metric2, metric3 = st.columns(3)
    metric1.metric("Available listings", total_pieces, help="Number of available listing records, not the total quantity of physical pieces.")
    metric2.metric("Wood listings", wood_pieces)
    metric3.metric("Metal listings", metal_pieces)

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-kicker">Why ScrapFit</div><div class="sf-section-title">The hard part is finding the right leftover</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-copy">A usable offcut can be difficult to discover when leftover materials are stored without searchable dimensions. Meanwhile, a maker or workshop may need only one or two pieces—not a full board or sheet. ScrapFit is designed to connect those two sides through project requirements and clearer listing details.</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">⌕</div><h3>Search for a fit</h3><p>Filter by material, dimensions, thickness and pickup area to find offcuts that could meet a part requirement.</p></div>', unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">▤</div><h3>Plan more than one part</h3><p>Enter the parts a project needs. ScrapFit checks suitable pieces and can try grouping multiple parts onto the same offcut using a prototype cutting-layout heuristic.</p></div>', unsafe_allow_html=True)
    with c3:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">⌖</div><h3>Keep reuse local</h3><p>Pickup-area details and approximate map locations help people explore nearby options and contact the listing owner to confirm the details.</p></div>', unsafe_allow_html=True)

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-kicker">How it works</div><div class="sf-section-title">From a project idea to a possible offcut</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-copy">The app keeps project suggestions and material-fit decisions separate: AI can help draft a parts list, while ScrapFit’s matching rules check the requirements against the inventory.</div>', unsafe_allow_html=True)
    step1, step2, step3 = st.columns(3)
    with step1:
        st.markdown('<div class="sf-step"><div class="sf-step-num">STEP 01</div><h3>List or describe</h3><p>A listing records material, dimensions, thickness, condition, price, quantity and pickup details. A buyer enters an individual part or a project cut list; Custom Project can generate an editable AI draft.</p></div>', unsafe_allow_html=True)
    with step2:
        st.markdown('<div class="sf-step"><div class="sf-step-num">STEP 02</div><h3>Check dimensions and quantity</h3><p>Rule-based matching checks the required material and dimensions, includes the configured cutting allowance, and ranks suitable available offcuts. The cutting layout is a prototype estimate, not a factory-validated nesting plan.</p></div>', unsafe_allow_html=True)
    with step3:
        st.markdown('<div class="sf-step"><div class="sf-step-num">STEP 03</div><h3>Review and arrange pickup</h3><p>Compare the match and rough cost estimates, inspect the listing details, and contact the seller to confirm the actual material, availability, price and collection.</p></div>', unsafe_allow_html=True)

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-kicker">Who it can help</div><div class="sf-section-title">Small quantities, practical projects</div>', unsafe_allow_html=True)
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">⚙</div><h3>Workshops</h3><p>Make reusable wood and metal leftovers easier to list and potentially recover value from.</p></div>', unsafe_allow_html=True)
    with b2:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">▦</div><h3>Small businesses</h3><p>Look for smaller quantities that fit a job without automatically buying a full sheet or board.</p></div>', unsafe_allow_html=True)
    with b3:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">✎</div><h3>Makers and students</h3><p>Explore material options for prototypes, creative builds and practical projects.</p></div>', unsafe_allow_html=True)
    with b4:
        st.markdown('<div class="sf-card"><div class="sf-card-icon">⌁</div><h3>Repair providers</h3><p>Find a usable piece for small repairs where an offcut may be enough.</p></div>', unsafe_allow_html=True)

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-kicker">Circular value</div><div class="sf-section-title">Keep useful material in circulation</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-copy">When an unused piece meets a real project requirement, material that might otherwise sit in storage or be discarded can continue to be useful.</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-flow"><span>Workshop leftovers</span><b>→</b><span>Project-based matching</span><b>→</b><span>Local collection</span><b>→</b><span>Useful new product</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-callout"><strong>Our aim</strong><br>Make reuse a practical option by making leftover materials easier to describe, compare and discover before new material is purchased. ScrapFit starts with wood and metal, local pickup and a straightforward matching workflow.</div>', unsafe_allow_html=True)

    st.markdown('<div class="sf-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-kicker">Current build and next steps</div><div class="sf-section-title">A working prototype with room to grow</div>', unsafe_allow_html=True)
    st.markdown('<div class="sf-section-copy">The current app demonstrates the core workflow with local accounts and a local inventory file. The next improvements focus on shared online storage, smarter cutting plans and easier participation.</div>', unsafe_allow_html=True)
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown('<div class="sf-card"><h3>Available in this prototype</h3><p>• Continue as a guest, or create a local account to list scrap<br>• Browse listings with quality grades (A/B/C) and availability status<br>• Filter by category, type, size, grade, price, place and availability, then sort the results<br>• Create a manual project cut list and set quantity for each part<br>• Generate and edit an AI-assisted draft parts list in Custom Project<br>• Match requirements using rules that explain why a listing fits (or why a part has no match)<br>• View a prototype cutting plan and rough cost estimates<br>• Create, edit and delete your own listings, update quantity and photos<br>• Download a QR code that opens each listing<br>• Save favourites (kept permanently when logged in)<br>• Record completed collections and let buyers confirm the handover before they can review<br>• Display buyer-rated listing accuracy and seller trust levels<br>• Request admin-approved Verified Shop status</p><p class="sf-footnote">When Supabase is configured, listings, accounts and collection/review records are stored in shared cloud documents, and listing photos use Supabase Storage. Without Supabase settings, this app falls back to local JSON files. Demo inventory and locations should be treated as illustrative.</p></div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="sf-card"><h3>Planned enhancements</h3><p><strong>Online accounts</strong><br>Secure online sign-in, e-mail based password recovery and database-level ownership rules will arrive with the planned move to Supabase. The current local accounts are for development and testing.<br><br><strong>Stronger trust and safety</strong><br>Add buyer-side pickup codes or verified contact channels, dispute handling and more robust evidence checks as ScrapFit moves from local JSON storage to online accounts.<br><br><strong>Smarter cutting plans</strong><br>Improve layout optimisation and account for blade kerf, grain direction and usable defects.<br><br><strong>Broader local reuse</strong><br>Shared cloud inventory and photo storage, more material types, and ways to group nearby pickups.<br><br><strong>Sustainable business model</strong><br>Explore clearly labelled promoted shop listings or advertising; consider transaction commissions only when completed sales can be tracked reliably.</p><p class="sf-footnote">The review and shop-verification workflow is available in this local prototype. Supabase accounts, cloud storage and the other items are roadmap ideas. Public QR links only work for other people once the app is deployed at a public web address.</p></div>', unsafe_allow_html=True)

    st.markdown('<p class="sf-footnote">Demo notice: sample listing details and approximate pickup locations are for demonstration and are not verified offers. Illustrative material photographs may not show the exact item listed. Before purchasing or building, confirm actual dimensions, material grade, condition, price, availability and collection with the seller. AI-generated part lists and cutting plans are estimates that require review.</p>', unsafe_allow_html=True)


# ==============================================================================
# PAGE: Find Scrap
# ==============================================================================
# Find Scrap page: search by pickup place and/or required dimensions, with combined filters
# and sorting. Open to guests. The filtering/matching rules live in search.py and matching.py.

MAX_RESULTS_SHOWN = 50


def _render_focused_listing(inventory):
    """Original feature: show one selected listing above the search form."""
    focused_listing_id = st.session_state.get("pending_listing_focus")
    if not focused_listing_id:
        return
    focused_piece = next((x for x in inventory if str(x.get("id", "")) == focused_listing_id), None)
    st.subheader("Selected recommended listing")
    if focused_piece:
        render_scrap_listing(focused_piece, key_prefix="focused_recommendation", show_save=True)
    else:
        st.info("This listing is no longer present in the current inventory.")
    if st.button("Clear selected listing", key="clear_focused_listing"):
        st.session_state.pop("pending_listing_focus", None)
        st.rerun()
    st.divider()


def _render_map(results):
    """Original map view of the results (hover tooltip with listing details)."""
    st.markdown("### Map view (demo locations)")
    st.caption("Hover over a pin to see listing details. Pins are illustrative demo locations, not verified seller addresses.")
    demo_points = []
    area_coords = {
        "Area A": (80.225, 13.055),
        "Area B": (80.245, 13.075),
        "Area C": (80.265, 13.045),
        "Chennai": (80.270, 13.082),
        "Padur": (80.2258, 12.8077),
    }
    for idx, item in enumerate(results):
        area = item.get("area") or item.get("pickup_location") or "Chennai"
        coords = item.get("coordinates") or {}
        if isinstance(coords, dict) and coords.get("lat") is not None and coords.get("lon") is not None:
            lat, lon = float(coords["lat"]), float(coords["lon"])
        else:
            lon, lat = area_coords.get(area, area_coords["Chennai"])
            lon += (idx % 4) * 0.0025
            lat += (idx % 3) * 0.002
        demo_points.append({
            "name": item.get("name", "Scrap"),
            "id": item.get("id", ""),
            "material": item.get("material", ""),
            "dimensions": f"{item.get('length', '?')} × {item.get('width', '?')} × {item.get('thickness', '?')} mm",
            "price": f"₹{item.get('price', '—')} per piece",
            "condition": f"Grade {item.get('quality_grade', '?')}",
            "area": area,
            "lon": lon,
            "lat": lat,
        })
    if not demo_points:
        return
    try:
        import pydeck as pdk
        layer = pdk.Layer(
            "ScatterplotLayer", data=demo_points,
            get_position="[lon, lat]", get_radius=100,
            get_fill_color=[33, 87, 71, 220], get_line_color=[255, 255, 255],
            line_width_min_pixels=2, stroked=True, pickable=True,
        )
        tooltip = {
            "html": "<b>{name}</b><br/>ID: {id}<br/>{material} · {dimensions}<br/>{price}<br/>Quality: {condition}<br/>Pickup area: {area}",
            "style": {"backgroundColor": "#173F34", "color": "white", "padding": "10px", "borderRadius": "8px"},
        }
        view_state = pdk.ViewState(latitude=13.07, longitude=80.245, zoom=11, pitch=0)
        st.pydeck_chart(
            pdk.Deck(layers=[layer], initial_view_state=view_state, tooltip=tooltip, map_style=None),
            use_container_width=True,
        )
    except Exception:
        st.warning("Hoverable map needs pydeck. Showing the locations as a basic map instead.")
        st.map(demo_points, latitude="lat", longitude="lon", size=80)


def render_find_scrap_page():
    inventory = get_inventory()
    _render_focused_listing(inventory)

    st.header("🔎 Find a piece for your project")
    st.write(
        "Choose how you want to search, then narrow the results with the optional filters. "
        "Only available listings are shown unless you choose otherwise."
    )

    # First control on the page (unchanged from the original app).
    filter_mode = st.selectbox(
        "Filter by",
        ["Pickup place", "Dimensions", "Pickup place + Dimensions"],
        key="find_filter_mode_v3",
        help="Choose whether to browse by pickup place, match dimensions, or do both.",
    )
    use_place = filter_mode in ("Pickup place", "Pickup place + Dimensions")
    use_dimensions = filter_mode in ("Dimensions", "Pickup place + Dimensions")

    # One consistent dropdown of saved pickup places (exact, case-insensitive labels).
    place_labels = {}
    for piece in inventory:
        for value in (piece.get("pickup_location"), piece.get("area")):
            if value and str(value).strip():
                label = str(value).strip()
                place_labels.setdefault(label.casefold(), label)
    place_options = ["All places"] + sorted(place_labels.values(), key=str.casefold)

    selected_place = "All places"
    if use_place:
        selected_place = st.selectbox(
            "Choose pickup place", place_options, key="find_filter_place_v3",
            help="Select a saved pickup place; there is no free-text location field.",
        )

    # Required part dimensions (only used by the two Dimensions modes).
    required_material = None
    required_length = required_width = required_thickness = None
    cutting_allowance = 5
    if use_dimensions:
        st.markdown("#### Dimensions required for your project part")
        st.caption(
            "These are the dimensions of the part you need. They control material "
            "matching; they are not the optional listing-size filter fields."
        )
        dim_col1, dim_col2 = st.columns(2)
        with dim_col1:
            required_material = st.selectbox("Material", MATERIALS, key="find_required_material_v3")
            required_length = st.number_input(
                "Required length (mm)", min_value=1, max_value=MAX_DIMENSION_MM,
                value=500, step=10, key="find_required_length_v3",
            )
            required_thickness = st.number_input(
                "Required thickness (mm)", min_value=1, max_value=MAX_THICKNESS_MM,
                value=18 if required_material == "Wood" else 3, step=1, key="find_required_thickness_v3",
            )
        with dim_col2:
            required_width = st.number_input(
                "Required width (mm)", min_value=1, max_value=MAX_DIMENSION_MM,
                value=250, step=10, key="find_required_width_v3",
            )
            cutting_allowance = st.number_input(
                "Extra cutting allowance per side (mm)", min_value=0, max_value=100, value=5, step=1,
                key="find_required_allowance_v3", help="Adds this allowance to each edge of the required part.",
            )
            st.caption("Thickness must match exactly. Confirm grain direction and cutting requirements with the seller.")

    # ---------- optional filters (all combine with each other) ----------
    with st.expander("More filters", expanded=False):
        if not use_dimensions:
            filter_material = st.selectbox(
                "Material category", ["All materials"] + MATERIALS, key="find_listing_material_v4",
            )
        else:
            filter_material = "All materials"  # the required material above is the category filter

        type_pool = [p for p in inventory if filter_material in ("All materials", p.get("material"))
                     and (not use_dimensions or p.get("material") == required_material)]
        material_types = sorted({str(p.get("material_type", "")).strip() for p in type_pool
                                 if str(p.get("material_type", "")).strip()}, key=str.casefold)
        filter_type = st.selectbox("Material type", ["All types"] + material_types, key="find_listing_type_v4")

        grade_choices = st.multiselect(
            "Quality grade", QUALITY_GRADES, default=QUALITY_GRADES,
            format_func=lambda g: f"Grade {g} – {GRADE_DESCRIPTIONS[g]}", key="find_listing_grades_v4",
            help="Grade A is best. Untick a grade to leave it out.",
        )
        status_choices = st.multiselect(
            "Availability", STATUSES, default=[STATUS_AVAILABLE], key="find_listing_status_v4",
            help="Only Available listings can be bought or matched. Add Reserved/Sold/Unavailable to see them anyway.",
        )

        all_prices = [int(p.get("price", 0) or 0) for p in inventory]
        price_cap = max(100, max(all_prices, default=100))
        min_price, max_price = st.slider(
            "Price range (₹ per piece)", 0, price_cap, (0, price_cap),
            step=max(1, price_cap // 100), key="find_listing_price_v3",
        )

        st.markdown("**Listing size limits** (independent of the required part dimensions; 0 = no limit)")
        size_left, size_right = st.columns(2)
        with size_left:
            min_length = st.number_input("Minimum length (mm)", min_value=0, max_value=MAX_DIMENSION_MM, value=0, step=50, key="find_min_listing_length_v3")
            min_width = st.number_input("Minimum width (mm)", min_value=0, max_value=MAX_DIMENSION_MM, value=0, step=50, key="find_min_listing_width_v3")
            min_thickness = st.number_input("Minimum thickness (mm)", min_value=0, max_value=MAX_THICKNESS_MM, value=0, step=1, key="find_min_listing_thickness_v3")
        with size_right:
            max_length = st.number_input("Maximum length (mm)", min_value=0, max_value=MAX_DIMENSION_MM, value=0, step=50, key="find_max_listing_length_v3")
            max_width = st.number_input("Maximum width (mm)", min_value=0, max_value=MAX_DIMENSION_MM, value=0, step=50, key="find_max_listing_width_v3")
            max_thickness = st.number_input("Maximum thickness (mm)", min_value=0, max_value=MAX_THICKNESS_MM, value=0, step=1, key="find_max_listing_thickness_v3")

    button_label = "Find Matching Scrap" if use_dimensions else "Show Scrap at Selected Place"
    search_submitted = st.button(button_label, type="primary", use_container_width=True, key="find_apply_filters_v3")

    if search_submitted:
        size_errors = [
            label for label, low, high in (
                ("length", min_length, max_length), ("width", min_width, max_width),
                ("thickness", min_thickness, max_thickness),
            ) if high and low > high
        ]
        if size_errors:
            st.session_state.pop("find_results_v3", None)
            st.error("Invalid dimensions: the minimum " + " and ".join(size_errors)
                     + " cannot be larger than the maximum. Please correct the listing size limits.")
        elif not status_choices:
            st.session_state.pop("find_results_v3", None)
            st.error("Please choose at least one availability status.")
        elif not grade_choices:
            st.session_state.pop("find_results_v3", None)
            st.error("Please choose at least one quality grade.")
        else:
            category = required_material if use_dimensions else (None if filter_material == "All materials" else filter_material)
            candidates = filter_listings(
                inventory,
                statuses=tuple(status_choices),
                material=category,
                material_type=None if filter_type == "All types" else filter_type,
                grades=None if set(grade_choices) == set(QUALITY_GRADES) else grade_choices,
                min_price=min_price, max_price=max_price,
                place=selected_place if (use_place and selected_place != "All places") else None,
                min_length=min_length, max_length=max_length, min_width=min_width,
                max_width=max_width, min_thickness=min_thickness, max_thickness=max_thickness,
            )
            if use_dimensions:
                # Status was already handled by the availability filter, so do not filter it twice.
                search_results = find_matches(
                    required_material, int(required_length), int(required_width), int(required_thickness),
                    candidates, allowance_per_side=int(cutting_allowance), require_available=False,
                )
            else:
                # Place-only browsing: no dimension matching, so no waste figure.
                search_results = []
                for source_piece in candidates:
                    piece = source_piece.copy()
                    piece["rotated"] = False
                    piece["waste_area"] = None
                    search_results.append(piece)

            st.session_state.find_results_v3 = search_results
            st.session_state.find_search_mode_v3 = filter_mode
            st.session_state.find_search_place_v3 = selected_place
            st.session_state.find_search_dimensions_v3 = (
                (required_material, int(required_length), int(required_width),
                 int(required_thickness), int(cutting_allowance)) if use_dimensions else None
            )

    # Hide stale results if the user switches search mode (as in the original app).
    if "find_results_v3" not in st.session_state or st.session_state.get("find_search_mode_v3") != filter_mode:
        return

    results = st.session_state.find_results_v3
    saved_place = st.session_state.get("find_search_place_v3", "All places")
    saved_dimensions = st.session_state.get("find_search_dimensions_v3")
    st.subheader("Matching results")

    if saved_dimensions:
        mat_q, len_q, wid_q, thick_q, allowance_q = saved_dimensions
        st.caption(f"Requirement: {mat_q} | {len_q} × {wid_q} × {thick_q} mm (cutting allowance: {allowance_q} mm per side)")
    elif saved_place == "All places":
        st.caption("Showing listings across all pickup places; dimensions were not used to match this search.")
    else:
        st.caption(f"Showing listings at {saved_place}; dimensions were not used to match this search.")

    if not results:
        st.warning("No matching materials found for the selected filters. Try another place, widen the price or size range, or include more grades.")
        if saved_dimensions:
            part = {"length": len_q, "width": wid_q, "thickness": thick_q}
            st.markdown("**Why nothing matched (checked against all listings):**")
            for reason in explain_unmatched(part, mat_q, inventory, allowance_q):
                st.write(f"- {reason}")
        return

    # ---------- sorting (applied on screen, so changing it needs no new search) ----------
    sort_choices = [option for option in SORT_OPTIONS if option != SORT_BEST_FIT or saved_dimensions]
    sort_choice = st.selectbox(
        "Sort by", sort_choices, index=sort_choices.index(SORT_BEST_FIT) if saved_dimensions else 0,
        key=f"find_sort_v4_{'dims' if saved_dimensions else 'browse'}",
        help="Best dimensional fit is available for Dimensions searches (least unused material first).",
    )
    user_location = None
    if sort_choice == SORT_NEAREST:
        if count_listings_with_location(results) == 0:
            st.info("None of these listings has a real map pin, so distance cannot be calculated reliably. Showing newest first.")
            sort_choice = SORT_RECENT
        else:
            loc_left, loc_right = st.columns(2)
            my_lat = loc_left.number_input("Your latitude", min_value=-90.0, max_value=90.0, value=13.0827, format="%.6f", key="find_my_lat_v4")
            my_lon = loc_right.number_input("Your longitude", min_value=-180.0, max_value=180.0, value=80.2707, format="%.6f", key="find_my_lon_v4")
            user_location = (float(my_lat), float(my_lon))
            st.caption("Straight-line distance from the coordinates above. Only listings whose seller entered a real map pin can be ranked; the rest are shown last.")
    ordered = sort_listings(results, sort_choice, user_location)

    st.success(f"Found {len(results)} listing(s) for the selected search.")
    if len(ordered) > MAX_RESULTS_SHOWN:
        st.caption(f"Showing the first {MAX_RESULTS_SHOWN} results. Use the filters to narrow the list.")
    for index, piece in enumerate(ordered[:MAX_RESULTS_SHOWN], start=1):
        st.markdown(f"**Search result {index}**")
        render_scrap_listing(piece, key_prefix="find", show_save=True, show_match=bool(saved_dimensions))
        if piece.get("waste_area") is not None:
            st.caption(f"Unused surface area after matching: {piece['waste_area']:,} mm²")
        else:
            st.caption("Dimensions are shown for reference; they were not used to match this Place-only search.")

    _render_map(ordered[:MAX_RESULTS_SHOWN])


# ==============================================================================
# PAGE: Project Planner
# ==============================================================================
# Project Planner page (open to guests). Same form and cut-plan display as the original app,
# plus a minimum quality option, explanations for matches and reasons for unmatched parts.

GRADE_CHOICES = {"Any grade": None, "Grade A only": "A", "Grade B or better": "B", "Grade C or better": "C"}


def render_planner_page():
    inventory = get_inventory()
    st.header("Project Planner")
    planner_available = [p for p in inventory if listing_is_available(p)]
    st.write(
        "Add the rectangular parts required for a project. ScrapFit leaves a cutting "
        "allowance and plans multiple parts from one physical offcut when a simple "
        "cutting layout fits. Set the quantity for repeated parts, such as four table legs. "
        "Only available listings are used."
    )

    with st.form("cut_list_form"):
        project_name = st.text_input("Project name", value="My Project")
        part_count = st.number_input("Number of different part requirements", min_value=1, max_value=10, value=2, step=1)
        st.caption("All dimensions are in millimetres.")

        parts = []
        for i in range(int(part_count)):
            st.markdown(f"**Part {i + 1}**")
            c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 0.9])
            with c1:
                part_name = st.text_input("Part name", value=f"Part {i + 1}", key=f"part_name_{i}")
            with c2:
                part_length = st.number_input("Length (mm)", min_value=1, max_value=MAX_DIMENSION_MM, value=500, step=10, key=f"part_length_{i}")
            with c3:
                part_width = st.number_input("Width (mm)", min_value=1, max_value=MAX_DIMENSION_MM, value=250, step=10, key=f"part_width_{i}")
            with c4:
                part_thickness = st.number_input("Thickness (mm)", min_value=1, max_value=MAX_THICKNESS_MM, value=18, step=1, key=f"part_thickness_{i}")
            with c5:
                part_quantity = st.number_input(
                    "Quantity", min_value=1, max_value=30, value=1, step=1, key=f"part_quantity_{i}",
                    help="How many identical pieces does the project need? For example, a table usually needs 4 legs.",
                )
            parts.append({
                "name": part_name.strip() or f"Part {i + 1}",
                "length": int(part_length), "width": int(part_width),
                "thickness": int(part_thickness), "quantity": int(part_quantity),
            })

        option_left, option_right, option_third = st.columns(3)
        with option_left:
            project_material = st.selectbox("Material for these parts", MATERIALS, key="project_material")
        with option_right:
            project_allowance = st.number_input(
                "Extra cutting allowance per side (mm)", min_value=0, max_value=100, value=5, step=1,
                help="Each offcut must be at least this much larger on every edge. Adjust for your cutting method and material.",
            )
        with option_third:
            grade_label = st.selectbox(
                "Minimum quality", list(GRADE_CHOICES), key="project_min_grade",
                help="Grade A is best. 'Grade B or better' accepts Grade A and B listings.",
            )
        pickup_area_filter = st.text_input(
            "Pickup area filter (optional)", key="project_pickup_area",
            placeholder="e.g. Guindy, Chennai",
            help="Only consider listings whose pickup area or pickup location contains this text.",
        )
        pickup_strategy_label = st.radio(
            "Matching preference",
            ["Smallest suitable offcuts", "Fewer pickup locations"],
            horizontal=True, key="project_pickup_strategy",
            help="Fewer pickup locations favours a place that can supply more parts, then tries to keep the remaining collection stops low.",
        )

        cut_list_submitted = st.form_submit_button("Search for Project Materials", type="primary", use_container_width=True)

    if cut_list_submitted:
        total_required_pieces = sum(int(part.get("quantity", 1)) for part in parts)
        if total_required_pieces > 30:
            st.session_state.pop("project_results", None)
            st.warning("This planner supports up to 30 physical pieces per search. Reduce the quantities and try again.")
        else:
            min_grade = GRADE_CHOICES[grade_label]
            # Optional area filter narrows the inventory before allocation.
            if pickup_area_filter.strip():
                area_query = pickup_area_filter.strip().casefold()
                planner_available = [p for p in planner_available if area_query in (
                    str(p.get("area", "")).casefold() + " " + str(p.get("pickup_location", "")).casefold()
                )]
            project_results = allocate_project_parts(
                project_material, expand_part_quantities(parts), planner_available,
                allowance_per_side=int(project_allowance), min_grade=min_grade,
                pickup_strategy=("fewest_locations" if pickup_strategy_label == "Fewer pickup locations" else "smallest"),
            )
            st.session_state.project_results = project_results
            st.session_state.project_title = project_name
            st.session_state.project_parts = parts
            st.session_state.project_material_saved = project_material
            st.session_state.project_allowance_saved = int(project_allowance)
            st.session_state.project_min_grade_saved = min_grade
            st.session_state.project_pickup_area_saved = pickup_area_filter.strip()
            st.session_state.project_pickup_strategy_saved = pickup_strategy_label

    if "project_results" not in st.session_state:
        return

    project_results = st.session_state.project_results
    saved_parts = st.session_state.get("project_parts", [p for p, _ in project_results])
    saved_material = st.session_state.get("project_material_saved", "Wood")
    saved_allowance = st.session_state.get("project_allowance_saved", 5)
    saved_min_grade = st.session_state.get("project_min_grade_saved")
    st.subheader(f"Matching results for: {st.session_state.project_title}")
    saved_strategy_label = st.session_state.get("project_pickup_strategy_saved", "Smallest suitable offcuts")
    used_areas = sorted({str(piece.get("area") or piece.get("pickup_location") or "Location not specified").strip()
                         for _, pieces in project_results for piece in pieces})
    st.caption(f"Matching preference: {saved_strategy_label} · Pickup stops in this plan: {len(used_areas)}")
    if st.session_state.get("project_pickup_area_saved"):
        st.caption(f"Pickup area filter: {st.session_state['project_pickup_area_saved']}")

    summary = summarize_allocation(project_results)
    assigned_results, unmatched_parts = summary["assigned"], summary["unmatched"]

    # Group required parts by physical offcut so a shared cut plan and its cost show once.
    cut_groups = {}
    for part, piece in assigned_results:
        unit_key = piece.get("cut_plan_unit_key", f"legacy-{piece.get('id', piece.get('name'))}")
        cut_groups.setdefault(unit_key, {"piece": piece, "parts": []})["parts"].append(part)
    cut_groups = sorted(
        cut_groups.values(),
        key=lambda group: (
            int(group["piece"].get("length", 0) or 0) * int(group["piece"].get("width", 0) or 0),
            int(group["piece"].get("price", 0) or 0),
        ),
    )

    planner_cost_inventory = [p for p in inventory if listing_is_available(p)]
    saved_area_filter = st.session_state.get("project_pickup_area_saved", "")
    if saved_area_filter:
        area_query = saved_area_filter.casefold()
        planner_cost_inventory = [p for p in planner_cost_inventory if area_query in (
            str(p.get("area", "")).casefold() + " " + str(p.get("pickup_location", "")).casefold()
        )]
    cost_rows, estimated_new_cost, matched_scrap_cost = project_cost_summary(
        saved_parts, planner_cost_inventory, saved_material, saved_allowance, min_grade=saved_min_grade)
    st.markdown("### Approximate material cost")
    st.caption(
        "Planning estimate only. New-material estimate uses the lowest listing price per square millimetre as an "
        "area-based proxy. Scrap cost counts each whole physical offcut used once. Labour, transport and blade kerf "
        "are excluded; the cutting layout is a simple heuristic, not industrial nesting."
    )
    st.write(f"**Estimated new-material equivalent:** ₹{estimated_new_cost:,}")
    st.write(f"**Whole-offcut scrap estimate:** ₹{matched_scrap_cost:,}")
    st.dataframe([{
        "Part": r["part"], "Estimated new material (₹)": r["new_estimate"],
        "Scrap cost contribution (₹)": (r["scrap_price"] if r["scrap_price"] not in (None, 0)
                                        else ("Same offcut — cost counted once" if r["scrap_price"] == 0 else "No match")),
    } for r in cost_rows], use_container_width=True, hide_index=True)

    st.info(
        f"{summary['assigned_count']} of {len(project_results)} required pieces can be cut from the current plan, "
        f"using {summary['physical_pieces_used']} physical piece(s). The same physical offcut can serve multiple parts "
        "if the simple shelf-layout heuristic finds a fit; each offcut is counted once in the estimate. "
        "This is a heuristic plan, not a guaranteed optimal cutting layout."
    )

    if assigned_results:
        st.markdown("### Which listing supplies which part")
        st.dataframe([{
            "Part": part["name"], "Size (mm)": f"{part['length']} × {part['width']} × {part['thickness']}",
            "Listing ID": piece["id"], "Physical piece": f"{piece['cut_plan_unit_number']} of {piece.get('quantity', 1)}",
            "Rotated 90°": "Yes" if piece.get("rotated") else "No",
            "Grade": piece.get("quality_grade", "?"),
        } for part, piece in assigned_results], use_container_width=True, hide_index=True)

    if cut_groups:
        st.markdown("### Offcuts used in the cutting plan")
        for rank, group in enumerate(cut_groups, start=1):
            piece = group["piece"]
            st.markdown(f"**{rank}. Listing selected for this cutting plan**")
            st.caption(f"Physical offcut unit {piece.get('cut_plan_unit_key', '')} · Cost counted once: ₹{piece.get('price', 0):,}")
            render_scrap_listing(piece, key_prefix=f"planner_{rank}", show_save=True, show_match=True)
            st.markdown("**Parts planned from this offcut:**")
            for part in group["parts"]:
                st.write(f"- {part['name']} — {part['length']} × {part['width']} × {part['thickness']} mm")

    if unmatched_parts:
        st.markdown("### Parts without an assigned offcut")
        st.warning(
            "No unused matching offcut could be assigned to these requirements. "
            "A suitable listing may not exist, or a suitable offcut may already be assigned to another part."
        )
        for part in unmatched_parts:
            st.write(f"- **{part['name']}** — {part['length']} × {part['width']} × {part['thickness']} mm")
            for reason in explain_unmatched(part, saved_material, inventory, saved_allowance, saved_min_grade):
                st.caption(f"↳ {reason}")
    elif assigned_results:
        st.success("Every required piece has been assigned in the current cutting plan.")


# ==============================================================================
# PAGE: Project Ideas
# ==============================================================================
# Project Ideas page (standard ideas + AI custom project). Open to guests.
#
# The page body is the original code. Changes: it imports its helpers from the new modules,
# reads the current inventory from the repository, and the listing cards now show quality grade etc.

# -------------------- PROJECT IDEAS --------------------

def render_project_ideas_page():
    inventory = get_inventory()
    st.header("💡 Project Ideas")
    st.write("Explore simple ways to give leftover materials a second life. These are starter ideas for a prototype, not certified engineering designs.")

    standard_tab, custom_tab = st.tabs(["Standard Projects", "Custom Project"])

    with custom_tab:
        # AI-powered custom project drafting is deliberately separate from the existing
        # standard project ideas and manual Project Planner.
        with st.container(border=True):
            st.subheader("✨ Custom Project — describe what you want to make")
            st.write("Describe your idea in everyday language. AI will suggest an editable starter cut list; ScrapFit's existing matching engine will then check available offcuts.")
            custom_description = st.text_area(
                "Project description",
                placeholder="e.g. A compact wooden study table with a top, four legs, and a shelf underneath. It should fit in a small room.",
                key="custom_ai_description",
                height=105,
            )
            custom_material_choice = st.selectbox(
                "Primary material for this draft", ["Wood", "Metal"], key="custom_ai_material"
            )
            st.caption("Gemini API setup is required for AI generation. The generated dimensions are estimates and must be reviewed before use.")
            has_ai_key = bool(get_gemini_api_key())
            if not has_ai_key:
                st.info("AI is not configured yet. Set GEMINI_API_KEY in .streamlit/secrets.toml or as an environment variable. You can still build a parts list manually in Project Planner.")
            if st.button("Generate draft parts list with AI", type="primary", key="custom_ai_generate", use_container_width=True):
                if len(custom_description.strip()) < 10:
                    st.warning("Describe your project in a little more detail before generating a draft.")
                elif not has_ai_key:
                    st.error("Gemini API key not found. Follow the setup instructions provided with this app, then restart Streamlit.")
                else:
                    try:
                        with st.spinner("Generating a starter cut list…"):
                            draft_payload = generate_ai_project_draft(custom_description, custom_material_choice)
                        st.session_state["custom_ai_draft"] = draft_payload
                        st.session_state.pop("custom_ai_match_results", None)
                        st.session_state.pop("custom_ai_match_signature", None)
                        st.success("Draft generated. Review every part and edit the dimensions before matching.")
                    except Exception as exc:
                        api_key = get_gemini_api_key()
                        error_detail = str(exc).replace(api_key, "[REDACTED]") if api_key else str(exc)
                    
                        print(
                            f"[ScrapFit Gemini Error] "
                            f"{type(exc).__name__}: {error_detail}"
                        )
                    
                        st.error(
                            f"Gemini request failed ({type(exc).__name__}). "
                            "Check the Streamlit Cloud logs for details."
                        )
            custom_draft = st.session_state.get("custom_ai_draft")
            if custom_draft:
                st.divider()
                st.markdown(f"### Review draft: {custom_draft.get('title', 'Custom project')}")
                st.caption(f"Primary material: {custom_draft.get('material', 'Wood')} · These are AI-generated estimates, not certified build dimensions.")
                if custom_draft.get("assumptions"):
                    st.markdown("**AI assumptions**")
                    for note in custom_draft["assumptions"]:
                        st.write(f"- {note}")
                edited_custom_parts = []
                for idx, part in enumerate(custom_draft["parts"]):
                    st.markdown(f"**Part {idx + 1}: {part['name']}**")
                    ca, cb, cc, cd, ce = st.columns([2, 1.2, 1.2, 1.1, 0.8])
                    with ca:
                        part_name = st.text_input("Part name", value=part["name"], key=f"custom_ai_name_{part['id']}")
                    with cb:
                        part_length = st.number_input("Length (mm)", min_value=1, max_value=10000, value=int(part["length"]), step=10, key=f"custom_ai_len_{part['id']}")
                    with cc:
                        part_width = st.number_input("Width (mm)", min_value=1, max_value=10000, value=int(part["width"]), step=10, key=f"custom_ai_wid_{part['id']}")
                    with cd:
                        part_thickness = st.number_input("Thickness (mm)", min_value=1, max_value=500, value=int(part["thickness"]), step=1, key=f"custom_ai_thick_{part['id']}")
                    with ce:
                        part_quantity = st.number_input("Quantity", min_value=1, max_value=20, value=int(part.get("quantity", 1)), step=1, key=f"custom_ai_qty_{part['id']}")
                    if part.get("reason"):
                        st.caption(part["reason"])
                    edited_custom_parts.append({
                        "id": part["id"], "name": part_name.strip() or f"Part {idx + 1}",
                        "length": int(part_length), "width": int(part_width),
                        "thickness": int(part_thickness), "quantity": int(part_quantity),
                    })

                cc1, cc2 = st.columns(2)
                with cc1:
                    custom_pickup_area = st.text_input("Pickup area filter (optional)", key="custom_ai_pickup_area", placeholder="e.g. Guindy, Chennai")
                with cc2:
                    custom_allowance = st.number_input("Cutting allowance per side (mm)", min_value=0, max_value=100, value=5, step=1, key="custom_ai_allowance")
                custom_pickup_strategy = st.radio(
                    "Matching preference", ["Smallest suitable offcuts", "Fewer pickup locations"],
                    horizontal=True, key="custom_ai_pickup_strategy",
                    help="Fewer pickup locations tries to source more parts from the same area, even if some offcuts are slightly larger.",
                )

                physical_piece_count = sum(int(part["quantity"]) for part in edited_custom_parts)
                if physical_piece_count > 30:
                    st.warning("This draft currently requires more than 30 individual pieces. Reduce the quantities before matching.")

                expanded_custom_parts = expand_part_quantities(edited_custom_parts)
                estimate_inventory = [item for item in inventory if item.get("status", "Available") == "Available"]
                if custom_pickup_area.strip():
                    area_query = custom_pickup_area.strip().casefold()
                    estimate_inventory = [item for item in estimate_inventory if area_query in (
                        str(item.get("area", "")).casefold() + " " + str(item.get("pickup_location", "")).casefold()
                    )]
                cost_rows, custom_new_estimate, custom_scrap_estimate = project_cost_summary(
                    expanded_custom_parts, estimate_inventory, custom_draft["material"], int(custom_allowance)
                )
                st.markdown("### Cost estimate")
                st.caption("Rough prototype estimate from sample inventory prices, not a live supplier quote. The new-material figure is area-based; the scrap figure counts each whole physical offcut used once. Labour, transport and blade kerf are not included.")
                st.write(f"**Estimated new-material equivalent:** ₹{custom_new_estimate:,} · **Whole-offcut scrap estimate:** ₹{custom_scrap_estimate:,}")
                st.dataframe(
                    [{"Part requirement": r["part"], "New-material estimate (₹)": r["new_estimate"], "Scrap cost contribution (₹)": (r["scrap_price"] if r["scrap_price"] not in (None, 0) else ("Same offcut — cost counted once" if r["scrap_price"] == 0 else "No match"))} for r in cost_rows],
                    use_container_width=True, hide_index=True
                )

                if st.button("Add another part", key="custom_ai_add_part"):
                    if len(edited_custom_parts) >= 10:
                        st.warning("A custom draft can contain up to 10 distinct part types.")
                    else:
                        draft_copy = dict(custom_draft)
                        draft_copy["parts"] = edited_custom_parts + [{
                            "id": uuid.uuid4().hex[:10], "name": f"Additional part {len(edited_custom_parts) + 1}",
                            "length": 200, "width": 100, "thickness": 18 if custom_draft["material"] == "Wood" else 3,
                            "quantity": 1, "reason": "Added by the user; adjust dimensions to suit your design."
                        }]
                        st.session_state["custom_ai_draft"] = draft_copy
                        st.session_state.pop("custom_ai_match_results", None)
                        st.session_state.pop("custom_ai_match_signature", None)
                        st.rerun()

                if edited_custom_parts and st.button("Remove a part…", key="custom_ai_toggle_remove"):
                    st.session_state["custom_ai_remove_mode"] = not st.session_state.get("custom_ai_remove_mode", False)
                    st.rerun()
                if st.session_state.get("custom_ai_remove_mode", False) and edited_custom_parts:
                    remove_index = st.selectbox(
                        "Choose the part to remove", options=list(range(len(edited_custom_parts))),
                        format_func=lambda i: f"{i + 1}. {edited_custom_parts[i]['name']}",
                        key="custom_ai_remove_index",
                    )
                    if st.button("Confirm remove part", key="custom_ai_confirm_remove"):
                        if len(edited_custom_parts) <= 1:
                            st.warning("Keep at least one part in the draft.")
                        else:
                            draft_copy = dict(custom_draft)
                            draft_copy["parts"] = edited_custom_parts[:remove_index] + edited_custom_parts[remove_index + 1:]
                            st.session_state["custom_ai_draft"] = draft_copy
                            st.session_state["custom_ai_remove_mode"] = False
                            st.session_state.pop("custom_ai_match_results", None)
                            st.session_state.pop("custom_ai_match_signature", None)
                            st.rerun()

                custom_signature = json.dumps({
                    "material": custom_draft["material"], "parts": expanded_custom_parts,
                    "area": custom_pickup_area.strip().casefold(), "allowance": int(custom_allowance),
                    "pickup_strategy": custom_pickup_strategy
                }, sort_keys=True)
                if st.button("Find matching scrap for this custom project", type="primary", key="custom_ai_match", use_container_width=True):
                    if not any(part["name"].strip() for part in edited_custom_parts):
                        st.warning("Add a name for at least one part.")
                    elif physical_piece_count > 30:
                        st.warning("Reduce the total quantity to 30 pieces or fewer before matching.")
                    else:
                        custom_inventory = [item for item in inventory if item.get("status", "Available") == "Available"]
                        if custom_pickup_area.strip():
                            q = custom_pickup_area.strip().casefold()
                            custom_inventory = [item for item in custom_inventory if q in (str(item.get("area", "")).casefold() + " " + str(item.get("pickup_location", "")).casefold())]
                        custom_matches = allocate_project_parts(
                            custom_draft["material"], expanded_custom_parts, custom_inventory,
                            allowance_per_side=int(custom_allowance),
                            pickup_strategy=("fewest_locations" if custom_pickup_strategy == "Fewer pickup locations" else "smallest"),
                        )
                        st.session_state["custom_ai_match_results"] = custom_matches
                        st.session_state["custom_ai_match_signature"] = custom_signature

                if st.session_state.get("custom_ai_match_signature") == custom_signature:
                    custom_matches = st.session_state.get("custom_ai_match_results", [])
                    assigned_custom = [(part, pieces[0]) for part, pieces in custom_matches if pieces]
                    missing_custom = [part for part, pieces in custom_matches if not pieces]
                    st.markdown("### Matching results")
                    custom_areas = {str(piece.get("area") or piece.get("pickup_location") or "Location not specified").strip() for _, pieces in custom_matches for piece in pieces}
                    st.info(f"{len(assigned_custom)} of {len(custom_matches)} required pieces are assigned across {len(custom_areas)} pickup location(s). Preference: {custom_pickup_strategy}. Multiple parts may share one physical offcut when the simple layout fits; this is a heuristic plan, not an industrial nesting guarantee.")
                    if assigned_custom:
                        shown_cut_units = {}
                        for required_part, piece in assigned_custom:
                            unit_key = piece.get("cut_plan_unit_key", f"legacy-{piece.get('id', piece.get('name'))}")
                            if unit_key in shown_cut_units:
                                st.write(f"- **{required_part['name']}** → cut from the same offcut as **{shown_cut_units[unit_key]}** (no additional listing cost).")
                            else:
                                shown_cut_units[unit_key] = required_part["name"]
                                extra_names = [name for name in piece.get("cut_plan_part_names", []) if name != required_part["name"]]
                                suffix = f" · shared cut plan also covers: {', '.join(extra_names)}" if extra_names else ""
                                st.markdown(f"**{required_part['name']}** is assigned to this listing{suffix}")
                                render_scrap_listing(piece, key_prefix=f"custom_{unit_key}", show_save=True)
                    if missing_custom:
                        st.warning("No suitable offcut was assigned to: " + ", ".join(part["name"] for part in missing_custom) + ". Try changing dimensions, quantity or pickup area.")
                    elif assigned_custom:
                        st.success("All required pieces have a matching offcut in the current inventory.")

                if custom_draft.get("design_notes"):
                    st.markdown("**Review before building**")
                    for note in custom_draft["design_notes"]:
                        st.write(f"- {note}")
                st.caption("AI drafts are suggestions only. Verify dimensions, material grade, stability, joinery, tooling and safe construction with a qualified person where appropriate.")


    with standard_tab:
        st.subheader("Standard project ideas")
        idea_filter = st.selectbox("What do you have or want to make?", ["Any material", "Wood", "Metal"])
        ideas = [
            {"title": "Wall shelf", "material": "Wood", "difficulty": "Beginner", "parts": "2 side supports, 1 shelf panel", "tools": "Saw, drill, screws, measuring tape", "time": "2–4 hours", "description": "A small shelf for books, tools or everyday items."},
            {"title": "Desk organiser", "material": "Wood", "difficulty": "Beginner", "parts": "Base panel, dividers, back panel", "tools": "Saw, glue or screws, sandpaper", "time": "1–3 hours", "description": "A compact organiser for stationery or small workshop tools."},
            {"title": "Metal tool stand", "material": "Metal", "difficulty": "Intermediate", "parts": "Base plate, supports, brackets", "tools": "Measuring tools, drill; suitable metalworking equipment", "time": "Half a day", "description": "A small stand for organising workshop tools. Use safe, suitable fabrication methods."},
            {"title": "Small planter stand", "material": "Wood", "difficulty": "Beginner", "parts": "Top panel, four legs, braces", "tools": "Saw, drill, screws, sandpaper", "time": "2–4 hours", "description": "A simple stand for a small plant pot."},
            {"title": "Metal parts tray", "material": "Metal", "difficulty": "Intermediate", "parts": "Base sheet, side strips", "tools": "Metal shears or suitable workshop tools; gloves and eye protection", "time": "2–4 hours", "description": "A shallow tray for storing small parts."},
        ]
        idea_parts = {
            "Wall shelf": [{"name":"Shelf panel", "length":600, "width":250, "thickness":18, "quantity":1}, {"name":"Side support 1", "length":250, "width":150, "thickness":18, "quantity":1}, {"name":"Side support 2", "length":250, "width":150, "thickness":18, "quantity":1}],
            "Desk organiser": [{"name":"Base panel", "length":300, "width":180, "thickness":12, "quantity":1}, {"name":"Divider 1", "length":180, "width":100, "thickness":12, "quantity":1}, {"name":"Divider 2", "length":180, "width":100, "thickness":12, "quantity":1}],
            "Metal tool stand": [{"name":"Base plate", "length":300, "width":200, "thickness":3, "quantity":1}, {"name":"Support 1", "length":250, "width":50, "thickness":3, "quantity":1}, {"name":"Support 2", "length":250, "width":50, "thickness":3, "quantity":1}],
            "Small planter stand": [{"name":"Top panel", "length":300, "width":300, "thickness":18, "quantity":1}, {"name":"Leg 1", "length":300, "width":40, "thickness":18, "quantity":1}, {"name":"Leg 2", "length":300, "width":40, "thickness":18, "quantity":1}],
            "Metal parts tray": [{"name":"Base sheet", "length":300, "width":200, "thickness":3, "quantity":1}, {"name":"Side strip 1", "length":300, "width":40, "thickness":3, "quantity":1}, {"name":"Side strip 2", "length":200, "width":40, "thickness":3, "quantity":1}],
        }
        shown = [i for i in ideas if idea_filter == "Any material" or i["material"] == idea_filter]
        for idea in shown:
            with st.container(border=True):
                photo_col, details_col = st.columns([1, 2.2], gap="medium")
                with photo_col:
                    st.image(IDEA_IMAGES.get(idea["title"], material_image(idea["material"])), use_container_width=True)
                    st.caption("Illustrative stock photo")
                with details_col:
                    st.subheader(idea["title"])
                    st.caption(f"{idea['material']} · {idea['difficulty']} · Estimated time: {idea['time']}")
                    st.write(idea["description"])
                    st.write(f"**Suggested parts:** {idea['parts']}")
                    st.write(f"**Tools:** {idea['tools']}")
                    if st.button("Review and customise project", key=f"idea_{idea['title']}"):
                        st.session_state.selected_idea = idea["title"]
                        st.session_state.idea_draft_parts = [x.copy() for x in idea_parts[idea["title"]]]
        if st.session_state.get("selected_idea"):
            selected_title = st.session_state.selected_idea
            selected_idea = next((x for x in ideas if x["title"] == selected_title), None)
            if selected_idea:
                st.divider()
                st.subheader(f"Review project draft: {selected_title}")
                st.caption("Edit each part's dimensions and quantity before searching. Dimensions are starter estimates, not a certified design.")
                draft_material = selected_idea["material"]
                edited_parts = []
                for idx, part in enumerate(st.session_state.idea_draft_parts):
                    st.markdown(f"**{part['name']}**")
                    a,b,c,d,e = st.columns(5)
                    with a: pname = st.text_input("Part name", value=part["name"], key=f"idea_name_{selected_title}_{idx}")
                    with b: plen = st.number_input("Length (mm)", min_value=1, max_value=10000, value=int(part["length"]), step=10, key=f"idea_len_{selected_title}_{idx}")
                    with c: pwid = st.number_input("Width (mm)", min_value=1, max_value=10000, value=int(part["width"]), step=10, key=f"idea_wid_{selected_title}_{idx}")
                    with d: pthick = st.number_input("Thickness (mm)", min_value=1, max_value=500, value=int(part["thickness"]), step=1, key=f"idea_thick_{selected_title}_{idx}")
                    with e: pqty = st.number_input("Quantity", min_value=1, max_value=100, value=int(part.get("quantity", 1)), step=1, key=f"idea_qty_{selected_title}_{idx}")
                    edited_parts.append({"name":pname, "length":int(plen), "width":int(pwid), "thickness":int(pthick), "quantity":int(pqty)})
                idea_area = st.text_input("Pickup area filter (optional)", key=f"idea_area_{selected_title}", placeholder="e.g. Guindy, Chennai")
                idea_allowance = st.number_input("Cutting allowance per side (mm)", min_value=0, max_value=100, value=5, key=f"idea_allowance_{selected_title}")
                idea_pickup_strategy = st.radio(
                    "Matching preference", ["Smallest suitable offcuts", "Fewer pickup locations"],
                    horizontal=True, key=f"idea_pickup_strategy_{selected_title}",
                    help="Fewer pickup locations prefers a location that can supply more of the project parts, then reuses that location where possible.",
                )
                cost_rows, new_est, scrap_est = project_cost_summary(edited_parts, inventory, draft_material, int(idea_allowance))
                st.markdown("### Cost estimate")
                st.caption("Rough prototype estimate from sample listing prices, not a live supplier quote. New-material cost is an area-based proxy; each whole offcut used is counted once. Labour, transport and blade kerf are excluded.")
                st.write(f"**Estimated new-material equivalent:** ₹{new_est:,} · **Whole-offcut scrap estimate:** ₹{scrap_est:,}")
                st.dataframe([{"Part":r["part"], "New material estimate (₹)":r["new_estimate"], "Scrap cost contribution (₹)": (r["scrap_price"] if r["scrap_price"] not in (None, 0) else ("Same offcut — cost counted once" if r["scrap_price"] == 0 else "No match"))} for r in cost_rows], use_container_width=True, hide_index=True)
                if st.button("Find matching scrap for this project", type="primary", key=f"idea_find_{selected_title}"):
                    idea_inventory = [x for x in inventory if x.get("status", "Available") == "Available"]
                    if idea_area.strip():
                        q = idea_area.strip().casefold()
                        idea_inventory = [x for x in idea_inventory if q in (str(x.get("area", "")).casefold()+" "+str(x.get("pickup_location", "")).casefold())]
                    matches = allocate_project_parts(
                        draft_material, expand_part_quantities(edited_parts), idea_inventory,
                        allowance_per_side=int(idea_allowance),
                        pickup_strategy=("fewest_locations" if idea_pickup_strategy == "Fewer pickup locations" else "smallest"),
                    )
                    st.session_state.idea_match_results = matches
                    st.session_state.idea_match_title = selected_title
                if st.session_state.get("idea_match_title") == selected_title:
                    matches = st.session_state.get("idea_match_results", [])
                    found = [(part, pieces[0]) for part, pieces in matches if pieces]
                    missing = [part for part, pieces in matches if not pieces]
                    st.subheader("Matching materials")
                    idea_used_areas = {str(piece.get("area") or piece.get("pickup_location") or "Location not specified").strip() for _, pieces in matches for piece in pieces}
                    st.caption(f"Matching preference: {idea_pickup_strategy} · Pickup stops in this plan: {len(idea_used_areas)}")
                    if found:
                        st.caption("Several parts can share one offcut when the simple cutting layout permits it; the offcut price is counted once in the project estimate.")
                        shown_cut_units = {}
                        for part, piece in found:
                            unit_key = piece.get("cut_plan_unit_key", f"legacy-{piece.get('id', piece.get('name'))}")
                            if unit_key in shown_cut_units:
                                st.write(f"- **{part['name']}** → cut from the same offcut as **{shown_cut_units[unit_key]}** (no additional listing cost).")
                            else:
                                shown_cut_units[unit_key] = part["name"]
                                extra_names = [name for name in piece.get("cut_plan_part_names", []) if name != part["name"]]
                                suffix = f" · shared cut plan also covers: {', '.join(extra_names)}" if extra_names else ""
                                st.markdown(f"**{part['name']}** is assigned to this listing{suffix}")
                                render_scrap_listing(piece, key_prefix=f"idea_{selected_title}_{len(shown_cut_units)}", show_save=True)
                    if missing:
                        st.warning("No matching material was found for: " + ", ".join(x["name"] for x in missing) + ". Try changing dimensions, material, or pickup area.")
                    elif not found:
                        st.warning("No matching materials found. Try changing dimensions or checking the inventory.")
                    else:
                        st.success(f"Matched {len(found)} of {len(matches)} part requirements. Unmatched parts are listed above if any.")
        st.info("These sample project ideas are starter drafts. Check dimensions, stability and safe construction before building.")


# ==============================================================================
# PAGE: List Scrap
# ==============================================================================
# List Scrap page. Guests see a friendly login prompt; the data layer ALSO refuses
# guests, so this is not just a hidden button.

FORM_KEYS = {
    "listing_name_input": "", "listing_material_input": "Wood", "listing_type_input": "",
    "listing_length_input": 600, "listing_thickness_input": 18, "listing_grade_input": "A",
    "listing_width_input": 300, "listing_quantity_input": 1, "listing_price_input": 50,
    "listing_area_input": "", "listing_pickup_location_input": "", "listing_pickup_details_input": "",
    "listing_notes_input": "", "listing_pin_input": False, "listing_lat_input": 13.0827,
    "listing_lon_input": 80.2707,
}


def render_list_scrap_page():
    st.header("➕ List leftover material")
    if not require_login("list your scrap"):
        return
    user = current_user()
    st.write("Enter the dimensions and details of a reusable offcut. Your listing will be linked to your account, and only you can edit or delete it.")

    # Fill / reset the form. Done before the widgets exist, so it is allowed. A failed
    # validation never clears what the user typed.
    reset = st.session_state.pop("reset_listing_form", False)
    for state_key, default_value in FORM_KEYS.items():
        if reset or state_key not in st.session_state:
            st.session_state[state_key] = default_value
    if reset or "listing_seller_input" not in st.session_state:
        st.session_state["listing_seller_input"] = user["name"]
    if reset or "listing_phone_input" not in st.session_state:
        st.session_state["listing_phone_input"] = user.get("phone", "")

    with st.form("list_scrap_form", clear_on_submit=False):
        listing_name = st.text_input("Listing name", placeholder="e.g. Plywood offcut", key="listing_name_input")

        c1, c2 = st.columns(2)
        with c1:
            listing_material = st.selectbox("Material category", MATERIALS, key="listing_material_input")
            listing_type = st.text_input("Material type", placeholder="e.g. Plywood, Pine, Mild steel, Aluminium", key="listing_type_input")
            listing_length = st.number_input("Length (mm)", min_value=1, max_value=MAX_DIMENSION_MM, step=10, key="listing_length_input")
            listing_thickness = st.number_input("Thickness (mm)", min_value=1, max_value=MAX_THICKNESS_MM, step=1, key="listing_thickness_input")
            listing_grade = st.selectbox(
                "Quality grade", QUALITY_GRADES,
                format_func=lambda g: f"Grade {g} – {GRADE_DESCRIPTIONS[g]}", key="listing_grade_input",
            )
        with c2:
            listing_width = st.number_input("Width (mm)", min_value=1, max_value=MAX_DIMENSION_MM, step=10, key="listing_width_input")
            listing_quantity = st.number_input(
                "Number of identical pieces", min_value=1, max_value=MAX_QUANTITY, step=1,
                key="listing_quantity_input",
                help="Use one listing for pieces with the same dimensions, quality and price.",
            )
            listing_price = st.number_input("Price per piece (₹)", min_value=0, max_value=MAX_PRICE, step=10, key="listing_price_input")
            listing_area = st.text_input("Pickup location (area / neighbourhood) *", placeholder="e.g. Guindy, Chennai", key="listing_area_input")

        st.markdown("**Seller and pickup details**")
        seller_name = st.text_input("Seller or shop name", placeholder="e.g. ABC Fabrication Shop", key="listing_seller_input")
        contact_phone = st.text_input("Phone / WhatsApp number *", placeholder="Include country code, e.g. +91...", key="listing_phone_input")
        pickup_location = st.text_input("Pickup address or landmark", placeholder="Shop address, street, or nearby landmark", key="listing_pickup_location_input")
        pickup_details = st.text_area("When can the buyer collect it?", placeholder="e.g. Weekdays 10 AM–5 PM; call before visiting; bring your own transport.", key="listing_pickup_details_input")
        listing_notes = st.text_area("Material description (optional)", placeholder="Describe marks, material grade, storage condition, or other details", key="listing_notes_input")
        listing_photos = st.file_uploader(
            f"Photos of this scrap (optional, up to {MAX_PHOTOS_PER_LISTING})", type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True, key="listing_photos_input",
            help="Photos are saved in the scrapfit_images folder beside the app. The first photo is the main one.",
        )
        st.markdown("**Map pin (optional)**")
        add_pin = st.checkbox("Add an approximate map pin for this listing", key="listing_pin_input",
                              help="Only tick this if the coordinates below are the real pickup location. A pin allows 'Nearest location' sorting.")
        listing_lat = st.number_input("Latitude", min_value=-90.0, max_value=90.0, format="%.6f", key="listing_lat_input")
        listing_lon = st.number_input("Longitude", min_value=-180.0, max_value=180.0, format="%.6f", key="listing_lon_input")

        submitted = st.form_submit_button("Add Listing", type="primary", use_container_width=True)

    if not submitted:
        return

    data = {
        "name": listing_name, "material": listing_material, "material_type": listing_type.strip() or "Other",
        "length": listing_length, "width": listing_width, "thickness": listing_thickness,
        "quantity": listing_quantity, "price": listing_price, "quality_grade": listing_grade,
        "area": listing_area.strip(), "seller_name": seller_name.strip(), "contact_phone": contact_phone.strip(),
        "pickup_location": pickup_location.strip(), "pickup_details": pickup_details.strip(),
        "notes": listing_notes.strip(),
    }
    if add_pin:
        data["coordinates"] = {"lat": float(listing_lat), "lon": float(listing_lon)}

    errors = validate_listing(data, require_contact=True, require_pickup=True)
    if errors:
        st.error("Please fix the following (your entries are still here):")
        for message in errors:
            st.write(f"- {message}")
        return

    saved_photos = []
    try:
        saved_photos = save_uploaded_photos(listing_photos)
        data["photos"] = saved_photos
        created = get_listing_repo().create(data, user)
    except PhotoError as exc:
        st.error(f"{exc} Your other entries are preserved.")
    except ValidationError as exc:
        remove_photo_files(saved_photos)
        st.error(" ".join(exc.errors))
    except (ListingError, OSError) as exc:
        remove_photo_files(saved_photos)  # do not leave orphan photos behind
        st.error(f"ScrapFit couldn't save this listing. Your form entries are preserved. Details: {exc}")
    else:
        flash("success", f"Listing created: '{created['name']}' (ID {created['id']}). It is now visible to buyers.")
        st.session_state["reset_listing_form"] = True
        st.session_state["pending_navigation"] = "Inventory"
        st.session_state["pending_inventory_scope"] = "My listings"
        st.rerun()


# ==============================================================================
# PAGE: Inventory (listing management, QR codes)
# ==============================================================================
# Inventory page: browse every listing; owners manage theirs (edit, quantity, status,
# photos, QR code, delete). All changes go through the repository, which checks ownership.

def _run_action(action, success_message):
    """Run a repository change, show friendly feedback, and refresh the page on success."""
    try:
        action()
    except (ListingError, PhotoError, OSError) as exc:
        st.error(str(exc))
        return False
    flash("success", success_message)
    st.rerun()


def _render_edit_tab(piece, user, repo):
    pid = piece["id"]
    with st.form(f"edit_form_{pid}"):
        name = st.text_input("Listing name", value=piece["name"], key=f"edit_name_{pid}")
        left, right = st.columns(2)
        with left:
            material = st.selectbox("Material category", MATERIALS,
                                    index=MATERIALS.index(piece["material"]) if piece["material"] in MATERIALS else 0,
                                    key=f"edit_material_{pid}")
            material_type = st.text_input("Material type", value=piece.get("material_type", ""), key=f"edit_type_{pid}")
            length = st.number_input("Length (mm)", min_value=1, max_value=MAX_DIMENSION_MM, value=max(1, int(piece["length"])), step=10, key=f"edit_len_{pid}")
            thickness = st.number_input("Thickness (mm)", min_value=1, max_value=MAX_THICKNESS_MM, value=max(1, int(piece["thickness"])), step=1, key=f"edit_thick_{pid}")
            grade = st.selectbox("Quality grade", QUALITY_GRADES, index=QUALITY_GRADES.index(piece["quality_grade"]),
                                 format_func=lambda g: f"Grade {g} – {GRADE_DESCRIPTIONS[g]}", key=f"edit_grade_{pid}")
        with right:
            width = st.number_input("Width (mm)", min_value=1, max_value=MAX_DIMENSION_MM, value=max(1, int(piece["width"])), step=10, key=f"edit_wid_{pid}")
            quantity = st.number_input("Quantity (pieces)", min_value=0, max_value=MAX_QUANTITY, value=int(piece["quantity"]), step=1, key=f"edit_qty_{pid}")
            price = st.number_input("Price per piece (₹)", min_value=0.0, max_value=float(MAX_PRICE), value=float(piece["price"]), step=10.0, key=f"edit_price_{pid}")
            status = st.selectbox("Availability", STATUSES, index=STATUSES.index(piece["status"]), key=f"edit_status_{pid}")
        area = st.text_input("Pickup location (area)", value=piece.get("area", ""), key=f"edit_area_{pid}")
        pickup_location = st.text_input("Pickup address or landmark", value=piece.get("pickup_location", ""), key=f"edit_addr_{pid}")
        pickup_details = st.text_area("Collection details", value=piece.get("pickup_details", ""), key=f"edit_details_{pid}")
        notes = st.text_area("Description / notes", value=piece.get("notes", ""), key=f"edit_notes_{pid}")
        seller_name = st.text_input("Seller or shop name", value=piece.get("seller_name", ""), key=f"edit_seller_{pid}")
        phone = st.text_input("Phone / WhatsApp number", value=piece.get("contact_phone", ""), key=f"edit_phone_{pid}")
        new_photos = st.file_uploader("Replace photos (optional)", type=["png", "jpg", "jpeg", "webp"],
                                      accept_multiple_files=True, key=f"edit_photos_{pid}",
                                      help="Uploading new photos replaces the current ones.")
        remove_photos = st.checkbox("Remove current photos (show the illustrative photo instead)", key=f"edit_rmphoto_{pid}")
        submitted = st.form_submit_button("Save changes", type="primary", use_container_width=True)

    if not submitted:
        return
    changes = {
        "name": name, "material": material, "material_type": material_type.strip() or "Other",
        "length": length, "width": width, "thickness": thickness, "quantity": quantity,
        "price": int(price) if float(price).is_integer() else round(price, 2),
        "quality_grade": grade, "status": status, "area": area.strip(),
        "pickup_location": pickup_location.strip(), "pickup_details": pickup_details.strip(),
        "notes": notes.strip(), "seller_name": seller_name.strip(), "contact_phone": phone.strip(),
    }
    saved_photos = []
    try:
        if new_photos:
            saved_photos = save_uploaded_photos(new_photos)
            changes["photos"] = saved_photos
        elif remove_photos:
            changes["photos"] = []
        repo.update(pid, changes, user)
    except (ListingError, PhotoError, OSError) as exc:
        remove_photo_files(saved_photos)
        message = " ".join(getattr(exc, "errors", [])) or str(exc)
        st.error(message)
    else:
        flash("success", f"Listing {pid} updated. The listing ID and QR code are unchanged.")
        st.rerun()


def _render_quick_tab(piece, user, repo):
    pid = piece["id"]
    st.write(f"**Pieces left:** {piece['quantity']} · **Status:** {piece['status']}")
    sold_col, add_col = st.columns(2)
    if sold_col.button("Sold 1 piece (−1)", key=f"sold1_{pid}", use_container_width=True, disabled=piece["quantity"] < 1):
        _run_action(lambda: repo.adjust_quantity(pid, -1, user),
                    f"One piece of {pid} marked sold. Remaining quantity updated.")
    if add_col.button("Add 1 piece (+1)", key=f"add1_{pid}", use_container_width=True):
        _run_action(lambda: repo.adjust_quantity(pid, 1, user), f"One piece added to {pid}.")
    new_status = st.selectbox("Change availability", STATUSES, index=STATUSES.index(piece["status"]), key=f"quick_status_{pid}")
    if st.button("Update availability", key=f"quick_status_btn_{pid}", disabled=new_status == piece["status"]):
        _run_action(lambda: repo.update(pid, {"status": new_status}, user), f"Availability of {pid} changed to {new_status}.")

    st.divider()
    st.markdown("**Record a completed collection**")
    st.caption("Use this after the buyer has collected the material. The buyer's registered email links the collection to their account, making only that buyer eligible to review this listing.")
    if piece.get("quantity", 0) > 0 and piece.get("status") in ("Available", "Reserved"):
        with st.form(f"record_collection_form_{pid}"):
            buyer_email = st.text_input("Buyer's registered ScrapFit email", key=f"collection_buyer_email_{pid}")
            collected_qty = st.number_input("Quantity collected", min_value=1, max_value=int(piece.get("quantity", 1)), value=1, step=1, key=f"collection_qty_{pid}")
            collection_submitted = st.form_submit_button("Confirm collection & record buyer", type="primary", use_container_width=True)
        if collection_submitted:
            try:
                get_transaction_repo().record_collection(pid, user, buyer_email, collected_qty, repo, get_user_repo())
            except (TrustFeatureError, ListingError, AuthError, OSError) as exc:
                st.error(str(exc))
            else:
                flash("success", f"Collection recorded for listing {pid}. The buyer must confirm the handover in Account / Profile before submitting a review.")
                st.rerun()
    else:
        st.caption("A collection can be recorded only while the listing is Available or Reserved and has stock remaining.")
    if piece["quantity"] == 0:
        st.caption("No pieces left, so this listing is hidden from search and project matching.")


def _render_qr_tab(piece):
    pid = piece["id"]
    url = build_listing_url(pid)
    if is_local_url(url):
        st.warning(
            "The public app URL is not set, so this QR code points to your own computer "
            f"({get_public_base_url()}). Set APP_BASE_URL (see README) to your deployed address "
            "before printing QR codes for other people."
        )
    try:
        png = cached_qr_png(url, pid)
    except RuntimeError as exc:
        st.error(str(exc))
        return
    st.image(png, width=240)
    st.caption("Scanning this code opens this listing:")
    st.code(url, language=None)
    st.download_button("Download QR code (PNG)", data=png, file_name=f"scrapfit_{pid}_qr.png",
                       mime="image/png", key=f"qr_dl_{pid}", use_container_width=True)
    st.caption("The code only contains the listing ID, so it keeps working after you edit the listing.")


def _render_delete_tab(piece, user, repo):
    pid = piece["id"]
    st.warning("Deleting a listing removes it and its photos permanently.")
    confirmed = st.checkbox("I understand this cannot be undone", key=f"del_confirm_{pid}")
    if st.button("Delete listing", key=f"delete_{pid}", disabled=not confirmed):
        _run_action(lambda: repo.delete(pid, user), f"Listing {pid} ('{piece['name']}') was deleted.")


def render_listing_manager(piece, user, repo):
    with st.expander(f"⚙ Manage this listing ({piece['id']})", expanded=False):
        edit_tab, quick_tab, qr_tab, delete_tab = st.tabs(["Edit", "Quantity & availability", "QR code", "Delete"])
        with edit_tab:
            _render_edit_tab(piece, user, repo)
        with quick_tab:
            _render_quick_tab(piece, user, repo)
        with qr_tab:
            _render_qr_tab(piece)
        with delete_tab:
            _render_delete_tab(piece, user, repo)


def render_inventory_page():
    st.header("📦 Scrap inventory")
    repo, user = get_listing_repo(), current_user()
    inventory = get_inventory()

    scopes = ["All listings"] + (["My listings"] if user else [])
    pending_scope = st.session_state.pop("pending_inventory_scope", None)
    if pending_scope in scopes:
        st.session_state["inventory_scope"] = pending_scope
    if st.session_state.get("inventory_scope") not in scopes:
        st.session_state["inventory_scope"] = scopes[0]
    scope = st.radio("Show", scopes, horizontal=True, key="inventory_scope") if user else scopes[0]

    st.caption("Browse listings. Quantity means identical pieces in one listing."
               + (" Use ‘Manage this listing’ on your own listings to edit them." if user
                  else " Log in to manage your own listings."))

    search_text = st.text_input("Search by name, ID or pickup area", key="inventory_search")
    f1, f2, f3 = st.columns(3)
    filter_material = f1.selectbox("Filter material", ["All"] + MATERIALS, key="inventory_material_filter")
    default_status = STATUSES if scope == "My listings" else [STATUS_AVAILABLE]
    status_filter = f2.multiselect("Availability", STATUSES, default=default_status, key=f"inventory_status_{scope}")
    grade_filter = f3.multiselect("Quality grade", QUALITY_GRADES, default=QUALITY_GRADES, key="inventory_grade_filter")

    needle = search_text.strip().lower()
    shown = [
        p for p in inventory
        if (scope == "All listings" or (user and p.get("owner_id") == user["id"]))
        and needle in (str(p.get("name", "")) + str(p.get("id", "")) + str(p.get("area", ""))).lower()
        and (filter_material == "All" or p.get("material") == filter_material)
        and p.get("status") in status_filter and p.get("quality_grade") in grade_filter
    ]

    if not shown:
        if scope == "My listings":
            st.info("You have no listings matching these filters yet. Use List Scrap to add one.")
        else:
            st.info("No listings match these filters.")
    for piece in shown:
        render_scrap_listing(piece, key_prefix="inventory", show_save=True)
        if repo.can_manage(piece, user):
            render_listing_manager(piece, user, repo)
        elif user and repo.can_claim(piece, user, is_admin(user)):
            st.info("This older listing has no owner account yet, but its contact number matches your profile.")
            if st.button("Claim this listing", key=f"claim_{piece['id']}"):
                _run_action(lambda pid=piece["id"]: repo.claim(pid, user, is_admin(user)),
                            f"Listing {piece['id']} is now linked to your account.")
        elif piece.get("owner_review_needed"):
            st.caption("Older listing without an owner account. It can be claimed by an account with the same contact number.")

    st.divider()
    st.caption("Listings are stored in inventory.json beside the app. Keep this file (and the scrapfit_images folder) to retain listings.")


# ==============================================================================
# PAGE: Save for Later
# ==============================================================================
# Save for Later page. Registered users keep favourites permanently (in users.json);
# guests keep them only for the current visit.

def render_saved_page():
    st.header("♡ Save for Later")
    user = current_user()
    if user:
        st.caption("Your saved listings are kept in your account.")
    else:
        st.info("You are browsing as a guest, so saved listings only last for this visit. Log in or create an account to keep them permanently.")
        st.button("Log in / Create account", key="saved_login_prompt", on_click=open_account_page, args=("login",))

    saved_ids = get_saved_ids()
    inventory = {p["id"]: p for p in get_inventory()}
    saved_items = [inventory[i] for i in saved_ids if i in inventory]
    missing = [i for i in saved_ids if i not in inventory]
    if missing:
        st.warning("Some saved listings no longer exist (they were deleted by their sellers): " + ", ".join(missing))
        if st.button("Remove deleted listings from my saved list", key="saved_prune"):
            for listing_id in missing:
                set_saved(listing_id, False)
            st.rerun()

    if not saved_items:
        st.info("You haven't saved any materials yet. Open Find Scrap and select Save for Later on a listing.")
        return

    available_total = sum((p.get("price") or 0) for p in saved_items if p.get("status") == "Available")
    st.metric("Total price of saved available listings (one piece each)", format_price(available_total))
    st.caption("This is the total of saved listing prices, not a complete project estimate. Sold, reserved or unavailable listings are not counted.")
    for piece in saved_items:
        if piece.get("status") != "Available":
            st.warning(f"“{piece['name']}” ({piece['id']}) is currently {piece.get('status')}. Contact the seller before relying on it.")
        render_scrap_listing(piece, key_prefix="saved", show_save=True)


# ==============================================================================
# PAGE: Account / Profile
# ==============================================================================
# Account / Profile page.

def _render_guest():
    st.write("You are using ScrapFit as a **guest**. You can browse, search and plan projects without an account. "
             "An account lets you list scrap, manage your listings, download QR codes and keep saved favourites.")
    hint = st.session_state.pop("account_tab_hint", None)
    if hint in ("login", "register"):
        st.session_state["account_mode"] = "Log in" if hint == "login" else "Create account"
    mode = st.radio("Account", ["Log in", "Create account"], horizontal=True, key="account_mode", label_visibility="collapsed")
    with st.container(border=True):
        if mode == "Log in":
            render_login_form("account")
        else:
            render_register_form("account")


def _render_profile(user):
    repo = get_user_repo()
    st.success(f"Logged in as {user['name']}")
    with st.container(border=True):
        st.markdown("### Your profile")
        st.write(f"**User ID:** `{user['id']}`")
        st.write(f"**Name:** {user['name']}")
        st.write(f"**Email:** {user['email']}")
        st.write(f"**Contact number:** {user.get('phone') or 'Not provided'}")
        st.write(f"**Account created:** {str(user.get('created_at'))[:10]}")
        mine = [p for p in get_inventory() if p.get("owner_id") == user["id"]]
        st.write(f"**Your listings:** {len(mine)} ({sum(p['status'] == 'Available' for p in mine)} available)")

    with st.expander("Edit profile"):
        with st.form("profile_form"):
            name = st.text_input("Full name", value=user["name"])
            phone = st.text_input("Contact number", value=user.get("phone", ""))
            if st.form_submit_button("Save profile", type="primary"):
                try:
                    repo.update_profile(user["id"], name, phone)
                except AuthError as exc:
                    st.error(str(exc))
                else:
                    flash("success", "Profile updated.")
                    st.rerun()

    with st.expander("Change password"):
        with st.form("password_form"):
            current = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password")
            confirm = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Change password"):
                try:
                    repo.change_password(user["id"], current, new, confirm)
                except AuthError as exc:
                    st.error(str(exc))
                else:
                    flash("success", "Password changed.")
                    st.rerun()

    # Buyer confirmation prevents sellers from silently assigning a review to an unrelated account.
    transaction_repo = get_transaction_repo()
    pending_confirmations = transaction_repo.pending_collection_confirmations(user["id"])
    st.subheader("Confirm collected materials")
    if pending_confirmations:
        st.caption("A seller has recorded a collection against your account. The listing is temporarily reserved; its quantity is deducted only after you confirm. Confirm only if you actually collected the material.")
        for pending in pending_confirmations:
            with st.container(border=True):
                st.markdown(f"**{pending.get('listing_name', 'Collected material')}** · {pending.get('listing_id')}")
                st.caption(f"Seller: {pending.get('seller_name') or 'ScrapFit seller'} · Quantity: {pending.get('quantity', 1)} · Recorded: {str(pending.get('recorded_at', ''))[:10]}")
                yes_col, no_col = st.columns(2)
                if yes_col.button("Yes, I collected this", key=f"confirm_collection_{pending['id']}", type="primary"):
                    try:
                        transaction_repo.confirm_collection(pending["id"], user, get_listing_repo(), collected=True)
                    except TrustFeatureError as exc:
                        st.error(str(exc))
                    else:
                        flash("success", "Collection confirmed. You can now review the seller below.")
                        st.rerun()
                if no_col.button("I did not collect this", key=f"dispute_collection_{pending['id']}"):
                    try:
                        transaction_repo.confirm_collection(pending["id"], user, get_listing_repo(), collected=False)
                    except TrustFeatureError as exc:
                        st.error(str(exc))
                    else:
                        flash("info", "The collection was flagged as disputed. It cannot generate a buyer review.")
                        st.rerun()
    else:
        st.caption("No collection confirmations are waiting for you.")

    # Buyer reviews: only collections confirmed by this buyer account appear here.
    pending_reviews = transaction_repo.pending_reviews_for_buyer(user["id"])
    st.subheader("Buyer reviews")
    if pending_reviews:
        st.caption("Review a seller only after you have collected the material. Each recorded collection can be reviewed once.")
        for purchase in pending_reviews:
            snapshot = purchase.get("listing_snapshot") or {}
            with st.container(border=True):
                st.markdown(f"**{purchase.get('listing_name', 'Collected material')}** · {purchase.get('listing_id')}")
                st.caption(f"Seller: {purchase.get('seller_name') or 'ScrapFit seller'} · Quantity: {purchase.get('quantity', 1)} · Collected: {str(purchase.get('recorded_at', ''))[:10]}")
                with st.form(f"buyer_review_form_{purchase['id']}"):
                    material_match = st.radio("Did the material match the listing?", ["Yes", "Mostly", "No"], horizontal=True, key=f"review_material_{purchase['id']}")
                    dimensions_match = st.radio("Were the dimensions and thickness as listed?", ["As listed", "Slightly off", "Way off"], horizontal=True, key=f"review_dimensions_{purchase['id']}")
                    condition_match = st.radio("Was the condition as described?", ["Yes", "No"], horizontal=True, key=f"review_condition_{purchase['id']}")
                    pickup_smooth = st.radio("Was pickup smooth and on time?", ["Yes", "No"], horizontal=True, key=f"review_pickup_{purchase['id']}")
                    overall_rating = st.select_slider("Overall seller rating", options=[1, 2, 3, 4, 5], value=5, key=f"review_stars_{purchase['id']}", format_func=lambda n: f"{'★' * n} ({n}/5)")
                    comment = st.text_area("Comment (optional)", max_chars=500, key=f"review_comment_{purchase['id']}")
                    review_submitted = st.form_submit_button("Submit buyer review", type="primary")
                if review_submitted:
                    try:
                        transaction_repo.submit_review(purchase["id"], user, material_match, dimensions_match, condition_match, pickup_smooth, overall_rating, comment)
                    except TrustFeatureError as exc:
                        st.error(str(exc))
                    else:
                        flash("success", "Your review was submitted and the seller's trust summary was updated.")
                        st.rerun()
    else:
        st.info("No collections are waiting for your review. A seller must record a completed handover against your registered email first.")

    own_trust = transaction_repo.seller_summary(user["id"])
    own_reviews = transaction_repo.reviews_for_seller(user["id"])
    if own_reviews or any(p.get("owner_id") == user["id"] for p in get_inventory()):
        with st.expander("Your shop reputation", expanded=False):
            if own_trust["count"]:
                left, middle, right = st.columns(3)
                left.metric("Average rating", f"{own_trust['average']:.1f}/5")
                middle.metric("Listing accuracy", f"{own_trust['accuracy']:.0f}%")
                right.metric("Buyer-confirmed reviews", own_trust["count"])
                st.write(f"**Buyer trust level:** {own_trust['badge']}")
                for reviewed_tx in own_reviews:
                    review = reviewed_tx["review"]
                    st.markdown(f"**{reviewed_tx.get('listing_name', 'Listing')}** · {'★' * review.get('overall_rating', 0)} · {str(review.get('submitted_at', ''))[:10]}")
                    st.caption(f"Material: {review.get('material_match')} · Dimensions: {review.get('dimensions_match')} · Condition: {review.get('condition_match')} · Pickup: {review.get('pickup_smooth')}")
                    if review.get("comment"):
                        st.write(review["comment"])
            else:
                st.info("You are a new seller. Buyer reviews will appear here after completed collections.")

    # Shop verification request and admin-controlled approval.
    verification = repo.get_shop_verification(user["id"])
    with st.expander("Shop verification", expanded=verification.get("status") == "pending"):
        state = verification.get("status", "not_requested")
        if state == "verified":
            st.success(f"Verified Shop: {verification.get('shop_name', 'Shop')}. This badge is separate from buyer ratings.")
            if verification.get("reviewed_at"):
                st.caption(f"Approved on {str(verification['reviewed_at'])[:10]}.")
        elif state == "pending":
            st.info(f"Your verification request for **{verification.get('shop_name', 'your shop')}** is pending admin review.")
            st.caption("Submitted details: " + str(verification.get("shop_area", "")))
        else:
            if state == "rejected":
                st.warning("Your previous request was not approved. You can update the details and submit again.")
                if verification.get("admin_note"):
                    st.caption("Admin note: " + str(verification.get("admin_note")))
            st.caption("Submit your shop details. A ScrapFit admin must manually check the business evidence before the public Verified Shop badge appears.")
            with st.form("shop_verification_request_form"):
                shop_name = st.text_input("Registered shop / workshop name", value=verification.get("shop_name", ""))
                shop_area = st.text_input("Shop address or area", value=verification.get("shop_area", ""))
                proof_details = st.text_area("Business proof for admin review", value=verification.get("proof_details", ""), max_chars=1000, placeholder="For example: business registration / GST details, business website, or other verifiable proof. Do not enter passwords or sensitive personal information.")
                request_submitted = st.form_submit_button("Submit verification request", type="primary")
            if request_submitted:
                try:
                    repo.request_shop_verification(user["id"], shop_name, shop_area, proof_details)
                except AuthError as exc:
                    st.error(str(exc))
                else:
                    flash("success", "Shop verification request submitted. The Verified Shop badge appears only after admin approval.")
                    st.rerun()

    if is_admin(user):
        with st.expander("Admin: pending shop verification requests"):
            pending_shop_requests = repo.list_pending_shop_verifications()
            if not pending_shop_requests:
                st.info("No shop verification requests are waiting for review.")
            for request in pending_shop_requests:
                st.markdown(f"**{request.get('shop_name')}** · {request.get('name')} ({request.get('email')})")
                st.caption(f"Area: {request.get('shop_area')} · Phone: {request.get('phone') or 'Not provided'} · Submitted: {str(request.get('requested_at', ''))[:10]}")
                st.write(f"Business proof details: {request.get('proof_details', '')}")
                admin_note = st.text_input("Admin note (optional)", key=f"shop_admin_note_{request['user_id']}")
                approve_col, reject_col = st.columns(2)
                if approve_col.button("Approve & verify shop", key=f"shop_approve_{request['user_id']}", type="primary"):
                    try:
                        repo.set_shop_verification_status(request["user_id"], "verified", user, admin_note)
                    except AuthError as exc:
                        st.error(str(exc))
                    else:
                        flash("success", f"{request.get('shop_name')} is now marked as a Verified Shop.")
                        st.rerun()
                if reject_col.button("Reject request", key=f"shop_reject_{request['user_id']}"):
                    try:
                        repo.set_shop_verification_status(request["user_id"], "rejected", user, admin_note)
                    except AuthError as exc:
                        st.error(str(exc))
                    else:
                        flash("info", f"Verification request for {request.get('shop_name')} was rejected.")
                        st.rerun()
                st.divider()

    listing_repo = get_listing_repo()
    claimable = [p for p in get_inventory() if listing_repo.can_claim(p, user, is_admin(user))]
    if claimable:
        with st.expander(f"Older listings you can claim ({len(claimable)})"):
            st.caption("These listings came from the original prototype and have no owner account. "
                       "You can claim a listing whose contact number matches your profile number.")
            for piece in claimable:
                left, right = st.columns([3, 1])
                left.write(f"**{piece['name']}** — {piece['id']} · {piece.get('contact_phone') or 'no contact'}")
                if right.button("Claim", key=f"acct_claim_{piece['id']}"):
                    try:
                        listing_repo.claim(piece["id"], user, is_admin(user))
                    except ListingError as exc:
                        st.error(str(exc))
                    else:
                        flash("success", f"Listing {piece['id']} is now linked to your account.")
                        st.rerun()

    if is_admin(user):
        with st.expander("Admin: backup and export"):
            st.caption("Set SCRAPFIT_ADMIN_EMAILS to a comma-separated list of admin email addresses in the app environment, or ADMIN_EMAILS in Streamlit secrets. Only these accounts can approve shop verification requests. Backups are copied into the backups folder.")
            if st.button("Create a backup of inventory.json now", key="admin_backup"):
                try:
                    path = listing_repo.create_backup("manual")
                    st.success(f"Backup saved: {path.name}")
                except ListingError as exc:
                    st.error(str(exc))
            st.download_button("Download inventory as JSON", data=listing_repo.export_json_bytes(),
                               file_name="scrapfit_inventory_export.json", mime="application/json", key="admin_export")

    st.button("Log out", key="account_logout", on_click=log_out)


def render_account_page():
    st.header("👤 Account / Profile")
    user = current_user()
    if user is None:
        _render_guest()
    else:
        _render_profile(user)


# ==============================================================================
# PAGE: public listing (opened by shared links and QR codes)
# ==============================================================================
# Public listing page opened by a shared link or a scanned QR code: /?listing=<ID>.
# Guests may view it, including photos and seller contact details.

def _leave_listing_view(target_page):
    """Button callback: remove ?listing=... from the address and go to another page."""
    st.query_params.clear()
    navigate_to(target_page)


def render_listing_detail_page(listing_id):
    st.header("Listing details")
    piece = get_listing_repo().get(listing_id)
    if piece is None:
        st.error(f"Listing “{listing_id}” was not found. It may have been deleted, or the link or QR code may be wrong.")
    else:
        status = piece.get("status")
        if status == "Reserved":
            st.warning("This listing is currently reserved. Contact the seller to check whether it is still free.")
        elif status in ("Sold", "Unavailable"):
            st.error(f"This listing is {status.lower()} and cannot be bought right now. It is shown for reference only.")
        render_scrap_listing(piece, key_prefix="public", show_save=True, expanded=True)
    left, right = st.columns(2)
    left.button("Browse all scrap", key="public_to_find", use_container_width=True, on_click=_leave_listing_view, args=("Find Scrap",))
    right.button("Open ScrapFit home", key="public_to_home", use_container_width=True, on_click=_leave_listing_view, args=("Home",))


# ==============================================================================
# MAIN: startup, welcome gate, sidebar and page routing
# ==============================================================================

@st.cache_resource
def upgrade_inventory_once():
    """Runs once per server start. Old-format inventory.json files are backed up, then upgraded."""
    return get_listing_repo().upgrade_if_needed()


def main():
    st.markdown(ORIGINAL_CSS, unsafe_allow_html=True)
    st.markdown(EXTRA_CSS, unsafe_allow_html=True)

    # Make sure inventory.json is readable and up to date (a backup is made before any upgrade).
    try:
        upgrade_report = upgrade_inventory_once()
    except InventoryFileError as exc:
        st.error(str(exc))
        st.stop()  # never continue (and risk overwriting) a file that could not be read

    if upgrade_report and not st.session_state.get("upgrade_notice_shown"):
        st.session_state["upgrade_notice_shown"] = True
        flash(
            "info",
            f"Your inventory file was upgraded to the new format. A backup of the original was saved as "
            f"{upgrade_report['backup_file']}. {upgrade_report['needs_owner_review']} older listing(s) have no owner account yet.",
        )

    user = current_user()
    listing_param = st.query_params.get("listing")  # set by public links and QR codes
    if isinstance(listing_param, list):
        listing_param = listing_param[0] if listing_param else None

    # Landing screen: Continue as Guest / Log in / Create Account. A public listing link skips it.
    if not user and not st.session_state.get("guest_mode") and not listing_param:
        render_welcome_page()
        render_flashes()
        st.stop()

    # Deferred navigation (set by pages before a rerun) must be applied BEFORE the radio is created.
    if "pending_navigation" in st.session_state:
        st.session_state["main_navigation"] = st.session_state.pop("pending_navigation")
    st.session_state.setdefault("main_navigation", "Home")

    with st.sidebar:
        st.markdown("## ♻ ScrapFit")
        st.caption("Find the next use for every offcut")
        if user:
            st.markdown(f'<div class="sf-user-chip">Signed in as<br><strong>{escape(user["name"])}</strong></div>', unsafe_allow_html=True)
            st.button("Log out", key="sidebar_logout", use_container_width=True, on_click=log_out)
        else:
            st.markdown('<div class="sf-user-chip">Browsing as <strong>guest</strong></div>', unsafe_allow_html=True)
            st.button("Log in / Create account", key="sidebar_login", use_container_width=True,
                      on_click=open_account_page, args=("login",))
        st.divider()
        page = st.radio("Navigate", PAGES, label_visibility="collapsed", key="main_navigation")
        st.divider()
        st.caption("Local prototype · JSON storage")

    render_flashes()

    if listing_param:
        render_listing_detail_page(str(listing_param))
    else:
        {
            "Home": render_home_page,
            "Find Scrap": render_find_scrap_page,
            "Project Planner": render_planner_page,
            "Project Ideas": render_project_ideas_page,
            "List Scrap": render_list_scrap_page,
            "Inventory": render_inventory_page,
            "Save for Later": render_saved_page,
            "Account / Profile": render_account_page,
        }[page]()

    st.divider()
    st.caption(
        "ScrapFit MVP · Local JSON prototype · Buyer reviews require a seller-recorded collection "
        "and buyer confirmation; shop verification requires admin approval. The prototype does "
        "not independently verify identity or handover. Sample data is for demonstration only; "
        "verify material grade, dimensions, condition and availability before real-world use."
    )


if __name__ == "__main__":  # Streamlit runs the script as __main__
    main()
