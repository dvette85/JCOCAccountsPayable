#!/usr/bin/env python3
"""
Johnson Church of Christ - Accounts Payable System
Flask + SQLite + Tailwind (CDN) responsive single-page app.
Desktop + Mobile friendly.
"""

import os
import re
import sqlite3
import uuid
from datetime import datetime, date, timedelta
from functools import wraps
from html import escape as html_escape
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_file, g, session, abort
import io
import csv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

try:
    import openpyxl
except ImportError:
    openpyxl = None

# Load .env file if present (python-dotenv is in requirements)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- CONFIG ----------
# Prefer persistent disk on Render (/data); fall back to local folder for development
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_default_db = os.path.join("/data", "ap.db") if os.path.isdir("/data") else os.path.join(_APP_DIR, "ap.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)
SECRET_KEY = os.environ.get("SECRET_KEY", "jcc-ap-dev-secret-change-in-prod")
PORT = int(os.environ.get("PORT", 5000))

# File uploads (attachments on requests)
_default_upload = os.path.join("/data", "uploads") if os.path.isdir("/data") else os.path.join(_APP_DIR, "uploads")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", _default_upload)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "heic",
    "doc", "docx", "xls", "xlsx", "csv", "txt", "zip",
}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 15))

ROLE_ADMIN = "Administrator"
ROLE_USER = "User"
CONTRIBUTION_METHODS = ("Check", "Cash", "Breeze", "Other")
DEFAULT_CONTRIBUTION_MEMO = "General Contribution"
DEFAULT_LETTER_TEMPLATE = """Johnson Church of Christ
Johnson, Arkansas

{{letter_date}}

{{contributor_name}}
{{address_block}}

Dear {{contributor_name}},

Thank you for your generous contribution to Johnson Church of Christ{{period_phrase}}.

{{gift_detail}}

This letter is your official written acknowledgment for income-tax purposes. No goods or services were provided in exchange for this contribution, other than intangible religious benefits.

With gratitude,

Johnson Church of Christ
"""

APPROVER_STEPS = (
    (1, "primary_approver_id", "Primary"),
    (2, "secondary_approver_id", "Secondary"),
    (3, "tertiary_approver_id", "Tertiary"),
)

# Base URL for generating links in emails (e.g. the public deployment URL)
# Set this as an environment variable on production (e.g. https://jcocaccountspayable.onrender.com)
# If not set, falls back to the incoming request's host (works for local dev)
BASE_URL = os.environ.get("BASE_URL")

# SMTP configuration for Johnson Church of Christ
# These values are set as defaults. They can be overridden by environment variables or .env file.
SMTP_CONFIG = {
    "server": os.environ.get("SMTP_SERVER", "Mail.JohnsonChurchofChrist.Com"),
    "port": int(os.environ.get("SMTP_PORT", 465)),
    "username": os.environ.get("SMTP_USERNAME", "AccountsPayable@JohnsonChurchofChrist.com"),
    "password": os.environ.get("SMTP_PASSWORD", "Hebrews12:15"),
    "from_email": os.environ.get("FROM_EMAIL", "AccountsPayable@JohnsonChurchofChrist.com"),
    "use_tls": True,
}

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure cookies on HTTPS (set SESSION_COOKIE_SECURE=true, or auto when BASE_URL is https)
_secure_cookie_env = os.environ.get("SESSION_COOKIE_SECURE", "").lower()
if _secure_cookie_env in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True
elif _secure_cookie_env in ("0", "false", "no"):
    app.config["SESSION_COOKIE_SECURE"] = False
else:
    app.config["SESSION_COOKIE_SECURE"] = bool(BASE_URL and str(BASE_URL).startswith("https://"))
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 14  # 14 days

# Trust proxy headers (important for Render, Heroku, etc. so request.host_url and scheme are correct)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Paths that do not require login (token approval / view links must stay public)
PUBLIC_ENDPOINTS = {
    "login",
    "logout",
    "approve_link",
    "reject_link",
    "view_request_email",
    "view_request_email_attachment",
    "forgot_password",
    "reset_password",
    "static",
}


def login_next_target():
    """Path (+ query) to resume after sign-in. Avoids open redirects."""
    target = request.full_path or request.path or "/"
    if target.endswith("?"):
        target = target[:-1]
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def safe_next_url(value, fallback=None):
    fallback = fallback or url_for("index")
    if not value:
        return fallback
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    return value


def login_required(f):
    """Decorator for routes that require an authenticated session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login", next=login_next_target()))
        return f(*args, **kwargs)
    return decorated


@app.before_request
def require_login():
    """Protect all app routes except public endpoints and static assets."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.endpoint is None:
        return None
    # Static files are served without endpoint sometimes; Flask marks them as 'static'
    if request.path.startswith("/static/"):
        return None
    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login", next=login_next_target()))
    return None


def current_user():
    """Return the logged-in user dict (without password_hash) or None."""
    uid = session.get("user_id")
    if not uid:
        return None
    return get_user(uid)


def is_admin(user=None):
    u = user if user is not None else current_user()
    return bool(u and u.get("role") == ROLE_ADMIN)


def require_admin_api():
    """Return a 403 JSON response if current user is not Administrator, else None."""
    if not is_admin():
        return jsonify({"error": "Administrator access required"}), 403
    return None


def split_gl_name(name):
    """Split 'CATEGORY:Account Name' into (category, account_name)."""
    if not name:
        return "", ""
    if ":" in name:
        left, right = name.split(":", 1)
        return left.strip(), right.strip()
    return "", name.strip()


def compose_gl_name(category, account_name):
    category = (category or "").strip()
    account_name = (account_name or "").strip()
    if category and account_name:
        return f"{category}:{account_name}"
    return account_name or category


def enrich_gl(d):
    """Add category / account_name display fields from name."""
    if not d:
        return d
    cat, aname = split_gl_name(d.get("name") or "")
    d["category"] = cat
    d["account_name"] = aname or (d.get("name") or "")
    return d


def user_can_view_request(user, req):
    """User may view if admin, requester, on approval chain, or notify recipient."""
    if not user or not req:
        return False
    if user.get("role") == ROLE_ADMIN:
        return True
    uid = user["id"]
    if req.get("requested_by_id") == uid:
        return True
    if req.get("notify_user_id") == uid:
        return True
    for key in ("primary_approver_id", "secondary_approver_id", "tertiary_approver_id"):
        if req.get(key) == uid:
            return True
    return False


def user_can_approve_request(user, req):
    """User may approve/reject if admin or current-step approver."""
    if not user or not req or req.get("status") != "Pending":
        return False
    if user.get("role") == ROLE_ADMIN:
        return True
    step = req.get("current_step") or 1
    keys = {1: "primary_approver_id", 2: "secondary_approver_id", 3: "tertiary_approver_id"}
    return req.get(keys.get(step)) == user["id"]


def user_can_edit_request(user, req):
    if not user or not req or req.get("status") != "Pending":
        return False
    if user.get("role") == ROLE_ADMIN:
        return True
    return req.get("requested_by_id") == user["id"]


def user_can_delete_request(user, req):
    if not user or not req:
        return False
    if user.get("role") == ROLE_ADMIN:
        return True
    return req.get("requested_by_id") == user["id"] and req.get("status") == "Pending"


def allowed_file(filename):
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def ensure_user_passwords():
    """Give existing users without a password the default so they can log in."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE password_hash IS NULL OR password_hash = ''")
    rows = cur.fetchall()
    if not rows:
        return
    default_pw = generate_password_hash("jccpass")
    for row in rows:
        cur.execute("UPDATE users SET password_hash=? WHERE id=?", (default_pw, row["id"]))
    db.commit()
    print(f"Set default password (jccpass) on {len(rows)} user(s) missing a password.")


def ensure_user_roles():
    """Ensure role column values are set; bootstrap first Administrator."""
    db = get_db()
    cur = db.cursor()
    cur.execute("UPDATE users SET role=? WHERE role IS NULL OR role=''", (ROLE_USER,))
    db.commit()

    # Ensure Darron.Mitchell exists as Administrator
    cur.execute(
        "SELECT id, role, password_hash FROM users WHERE username = ? COLLATE NOCASE",
        ("Darron.Mitchell",),
    )
    row = cur.fetchone()
    default_pw = generate_password_hash("jccpass")
    if row:
        # Promote / refresh identity; keep existing password if already set
        cur.execute(
            "UPDATE users SET role=?, first_name=?, last_name=?, email=? WHERE id=?",
            (ROLE_ADMIN, "Darron", "Mitchell", "Darron.Mitchell@hotmail.com", row["id"]),
        )
        if not row["password_hash"]:
            cur.execute("UPDATE users SET password_hash=? WHERE id=?", (default_pw, row["id"]))
        db.commit()
    else:
        try:
            cur.execute("""
                INSERT INTO users (username, first_name, last_name, email, password_hash, role)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ("Darron.Mitchell", "Darron", "Mitchell", "Darron.Mitchell@hotmail.com", default_pw, ROLE_ADMIN))
            db.commit()
            print("Seeded administrator Darron.Mitchell (default password: jccpass).")
        except sqlite3.IntegrityError:
            # Email collision — promote by email if present
            cur.execute("SELECT id FROM users WHERE email = ? COLLATE NOCASE", ("Darron.Mitchell@hotmail.com",))
            r2 = cur.fetchone()
            if r2:
                cur.execute(
                    "UPDATE users SET username=?, first_name=?, last_name=?, role=? WHERE id=?",
                    ("Darron.Mitchell", "Darron", "Mitchell", ROLE_ADMIN, r2["id"]),
                )
                db.commit()

    # If no administrator exists at all, promote first user
    cur.execute("SELECT COUNT(*) as c FROM users WHERE role=?", (ROLE_ADMIN,))
    if cur.fetchone()["c"] == 0:
        cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
        first = cur.fetchone()
        if first:
            cur.execute("UPDATE users SET role=? WHERE id=?", (ROLE_ADMIN, first["id"]))
            db.commit()
            print("Promoted first user to Administrator (no admin was present).")

# ---------- DB HELPERS ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    cur = db.cursor()

    # Users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            role TEXT DEFAULT 'User',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Safe migrations for existing databases
    for col_sql in (
        "ALTER TABLE users ADD COLUMN password_hash TEXT",
        "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'User'",
        "ALTER TABLE requests ADD COLUMN notify_user_id INTEGER",
    ):
        try:
            cur.execute(col_sql)
        except sqlite3.OperationalError:
            pass

    # GL Accounts
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gl_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_number TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            is_expense INTEGER DEFAULT 1,
            primary_approver_id INTEGER,
            secondary_approver_id INTEGER,
            tertiary_approver_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(primary_approver_id) REFERENCES users(id),
            FOREIGN KEY(secondary_approver_id) REFERENCES users(id),
            FOREIGN KEY(tertiary_approver_id) REFERENCES users(id)
        )
    """)

    # Requests
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date TEXT,
            amount REAL NOT NULL,
            description TEXT,
            gl_account_id INTEGER NOT NULL,
            requested_by_id INTEGER NOT NULL,
            notify_user_id INTEGER,
            status TEXT DEFAULT 'Pending',  -- Pending, Approved, Rejected
            current_step INTEGER DEFAULT 1,
            primary_approver_id INTEGER,
            secondary_approver_id INTEGER,
            tertiary_approver_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            approved_at TEXT,
            rejected_at TEXT,
            reject_reason TEXT,
            FOREIGN KEY(gl_account_id) REFERENCES gl_accounts(id),
            FOREIGN KEY(requested_by_id) REFERENCES users(id),
            FOREIGN KEY(notify_user_id) REFERENCES users(id)
        )
    """)

    # Request file attachments
    cur.execute("""
        CREATE TABLE IF NOT EXISTS request_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER,
            uploaded_by_id INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(request_id) REFERENCES requests(id),
            FOREIGN KEY(uploaded_by_id) REFERENCES users(id)
        )
    """)

    # Password reset tokens (forgot password)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Approval history
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approval_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            step INTEGER,
            approver_id INTEGER,
            action TEXT,  -- approved / rejected
            acted_at TEXT DEFAULT (datetime('now')),
            notes TEXT,
            FOREIGN KEY(request_id) REFERENCES requests(id),
            FOREIGN KEY(approver_id) REFERENCES users(id)
        )
    """)

    # Pending approval tokens (for email links)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            step INTEGER NOT NULL,
            approver_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(request_id) REFERENCES requests(id),
            FOREIGN KEY(approver_id) REFERENCES users(id)
        )
    """)

    # Email log (simulated + real attempts)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_email TEXT NOT NULL,
            subject TEXT,
            body TEXT,
            is_html INTEGER DEFAULT 1,
            sent_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'simulated'  -- simulated, sent, failed
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS contributors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            address_line1 TEXT DEFAULT '',
            address_line2 TEXT DEFAULT '',
            city TEXT DEFAULT '',
            state TEXT DEFAULT '',
            zip TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS contribution_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contributor_id INTEGER NOT NULL,
            contribution_date TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            check_number TEXT DEFAULT '',
            memo TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            created_by_id INTEGER,
            FOREIGN KEY(contributor_id) REFERENCES contributors(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS contribution_letter_template (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            body TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            updated_by_id INTEGER
        )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_contrib_entries_date ON contribution_entries(contribution_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_contrib_entries_contributor ON contribution_entries(contributor_id)"
    )

    db.commit()
    ensure_letter_template()

def seed_data():
    db = get_db()
    cur = db.cursor()

    # Seed sample users if none (roles applied by ensure_user_roles; Darron is admin)
    cur.execute("SELECT COUNT(*) as c FROM users")
    if cur.fetchone()["c"] == 0:
        default_pw = generate_password_hash("jccpass")
        sample_users = [
            ("Darron.Mitchell", "Darron", "Mitchell", "Darron.Mitchell@hotmail.com", default_pw, ROLE_ADMIN),
            ("jtreasurer", "Jane", "Treasurer", "jane.treasurer@johnsoncoc.org", default_pw, ROLE_USER),
            ("asmith", "Alex", "Smith", "alex.smith@johnsoncoc.org", default_pw, ROLE_USER),
            ("bwilson", "Beth", "Wilson", "beth.wilson@johnsoncoc.org", default_pw, ROLE_USER),
            ("rjohnson", "Robert", "Johnson", "robert.johnson@johnsoncoc.org", default_pw, ROLE_USER),
            ("mmartinez", "Maria", "Martinez", "maria.martinez@johnsoncoc.org", default_pw, ROLE_USER),
        ]
        cur.executemany(
            "INSERT INTO users (username, first_name, last_name, email, password_hash, role) VALUES (?,?,?,?,?,?)",
            sample_users
        )
        db.commit()
        print("Seeded sample users (default password: jccpass). Admin: Darron.Mitchell")

    # Get user ids for approver assignment
    cur.execute("SELECT id, username FROM users ORDER BY id")
    users = {row["username"]: row["id"] for row in cur.fetchall()}

    # Seed GL accounts from church list (if none)
    cur.execute("SELECT COUNT(*) as c FROM gl_accounts")
    if cur.fetchone()["c"] == 0:
        # From the provided Account List - expense accounts primarily
        EXPENSE_ACCOUNTS = [
            ("4000", "Uncategorized Expenses", "Expenses"),
            ("5000", "YOUTH EXPENSE", "Expenses"),
            ("5005", "YOUTH EXPENSE:Gifts", "Expenses"),
            ("5010", "YOUTH EXPENSE:Activities", "Expenses"),
            ("5011", "YOUTH EXPENSE:Youth Outreach University", "Expenses"),
            ("5012", "YOUTH EXPENSE:ARK Retreat", "Expenses"),
            ("5013", "YOUTH EXPENSE:Senior Sunday", "Expenses"),
            ("5014", "YOUTH EXPENSE:Summer Kickoff", "Expenses"),
            ("5015", "YOUTH EXPENSE:Orientation Meeting", "Expenses"),
            ("5016", "YOUTH EXPENSE:Service Day and Project", "Expenses"),
            ("5017", "YOUTH EXPENSE:Back to School Event", "Expenses"),
            ("5018", "YOUTH EXPENSE:Fall Retreat", "Expenses"),
            ("5019", "YOUTH EXPENSE:Lock-in Event", "Expenses"),
            ("5020", "YOUTH EXPENSE:Deeper Youth Conference", "Expenses"),
            ("5021", "YOUTH EXPENSE:Teen Devotionals", "Expenses"),
            ("5022", "YOUTH EXPENSE:Youth Camp Out Events", "Expenses"),
            ("5023", "YOUTH EXPENSE:Parent Ministry", "Expenses"),
            ("5024", "YOUTH EXPENSE:Area Wide Teen Workshop", "Expenses"),
            ("5025", "YOUTH EXPENSE:Youth Supplies", "Expenses"),
            ("5026", "YOUTH EXPENSE:ReFuel Events", "Expenses"),
            ("5027", "YOUTH EXPENSE:Miscellaneous Expenses", "Expenses"),
            ("5028", "YOUTH EXPENSE:Mentor Training", "Expenses"),
            ("5029", "YOUTH EXPENSE:Uplift", "Expenses"),
            ("5100", "EDUCATION EXPENSE", "Expenses"),
            ("5110", "EDUCATION EXPENSE:Elementary", "Expenses"),
            ("5115", "EDUCATION EXPENSE:Secondary", "Expenses"),
            ("5120", "EDUCATION EXPENSE:Adult Ed", "Expenses"),
            ("5125", "EDUCATION EXPENSE:VBS", "Expenses"),
            ("5130", "EDUCATION EXPENSE:Library", "Expenses"),
            ("5200", "Lads to Leaders", "Expenses"),
            ("5202", "Lads to Leaders:Lad to Leaders Registration", "Expenses"),
            ("5205", "Lads to Leaders:Lads to Leaders - Supplies", "Expenses"),
            ("5210", "Lads to Leaders:Lads to Leaders - Food", "Expenses"),
            ("5300", "CHRISTIAN FELLOWSHIP", "Expenses"),
            ("5305", "CHRISTIAN FELLOWSHIP:Congregation Food", "Expenses"),
            ("5310", "CHRISTIAN FELLOWSHIP:Kitchen Supplies", "Expenses"),
            ("5315", "CHRISTIAN FELLOWSHIP:Golden Years", "Expenses"),
            ("5320", "CHRISTIAN FELLOWSHIP:Ladies Ministry", "Expenses"),
            ("5325", "CHRISTIAN FELLOWSHIP:CREW Food", "Expenses"),
            ("5330", "CHRISTIAN FELLOWSHIP:Mens Ministry", "Expenses"),
            ("5400", "WORSHIP", "Expenses"),
            ("5405", "WORSHIP:Supplies (Worship)", "Expenses"),
            ("5410", "WORSHIP:New Member", "Expenses"),
            ("5415", "WORSHIP:Members Directory", "Expenses"),
            ("5500", "BENEVOLENCE", "Expenses"),
            ("5505", "BENEVOLENCE:Member Expense", "Expenses"),
            ("5510", "BENEVOLENCE:Transient Expense", "Expenses"),
            ("5515", "BENEVOLENCE:Flowers", "Expenses"),
            ("5520", "BENEVOLENCE:Funeral Expense", "Expenses"),
            ("5522", "BENEVOLENCE:Disaster Relief Effort, Inc.", "Expenses"),
            ("5523", "BENEVOLENCE:Churches Of Christ Disaster Response Team", "Expenses"),
            ("5524", "BENEVOLENCE:DISASTER ASSISTANCE MISSION", "Expenses"),
            ("5525", "BENEVOLENCE:Southern Christian Home", "Expenses"),
            ("5530", "BENEVOLENCE:Paragould Christian Home", "Expenses"),
            ("5535", "BENEVOLENCE:Manuelito Christian Home", "Expenses"),
            ("5536", "BENEVOLENCE:Village of Hope", "Expenses"),
            ("5540", "BENEVOLENCE:Local Aid", "Expenses"),
            ("5545", "BENEVOLENCE:Domestic Aid", "Expenses"),
            ("5546", "BENEVOLENCE:Foreign Aid", "Expenses"),
            ("5550", "BENEVOLENCE:Threads of Love", "Expenses"),
            ("5600", "LOCAL MISSIONS", "Expenses"),
            ("5605", "LOCAL MISSIONS:Green Valley Bible Camp", "Expenses"),
            ("5610", "LOCAL MISSIONS:Razorbacks for Christ", "Expenses"),
            ("5615", "LOCAL MISSIONS:Baldwin Tracts", "Expenses"),
            ("5620", "LOCAL MISSIONS:Area-Wide Services", "Expenses"),
            ("5625", "LOCAL MISSIONS:Summer Series", "Expenses"),
            ("5700", "DOMESTIC MISSIONS", "Expenses"),
            ("5701", "DOMESTIC MISSIONS:Preaching School", "Expenses"),
            ("5702", "DOMESTIC MISSIONS:Mitchell Church of Christ", "Expenses"),
            ("5703", "DOMESTIC MISSIONS:Chalmet Church of Christ", "Expenses"),
            ("5704", "DOMESTIC MISSIONS:New Mexico Bldg Projects", "Expenses"),
            ("5705", "DOMESTIC MISSIONS:New Mexico Mission Trip", "Expenses"),
            ("5707", "DOMESTIC MISSIONS:Gallup Church of Christ", "Expenses"),
            ("5708", "DOMESTIC MISSIONS:Truth for Today", "Expenses"),
            ("5709", "DOMESTIC MISSIONS:Estes Church of Christ (Mosher)", "Expenses"),
            ("5710", "DOMESTIC MISSIONS:In Search of the Lords Way", "Expenses"),
            ("5720", "DOMESTIC MISSIONS:Other Opportunities", "Expenses"),
            ("5800", "INTERNATIONAL MISSIONS", "Expenses"),
            ("5801", "INTERNATIONAL MISSIONS:Honduras - Marco Antonio", "Expenses"),
            ("5802", "INTERNATIONAL MISSIONS:Honduras - Marco Antonio Supplies", "Expenses"),
            ("5803", "INTERNATIONAL MISSIONS:Wire Fee", "Expenses"),
            ("5810", "INTERNATIONAL MISSIONS:Honduras Medical Mission", "Expenses"),
            ("5811", "INTERNATIONAL MISSIONS:Honduras Preaching", "Expenses"),
            ("5812", "INTERNATIONAL MISSIONS:Honduras Special Trips", "Expenses"),
            ("5813", "INTERNATIONAL MISSIONS:Gustavo Support", "Expenses"),
            ("5814", "INTERNATIONAL MISSIONS:Honduras Supplies", "Expenses"),
            ("5815", "INTERNATIONAL MISSIONS:Other Opportunities", "Expenses"),
            ("5816", "INTERNATIONAL MISSIONS:Gospel Chariots", "Expenses"),
            ("5817", "INTERNATIONAL MISSIONS:Yoni Gonzales - Honduras", "Expenses"),
            ("5818", "INTERNATIONAL MISSIONS:Juanito Nacario", "Expenses"),
            ("5819", "INTERNATIONAL MISSIONS:Tuttle - Billy Smith - Philippines", "Expenses"),
            ("5820", "INTERNATIONAL MISSIONS:Torch Missions", "Expenses"),
            ("5821", "INTERNATIONAL MISSIONS:Jay Justus (India Missions - Bibles Only)", "Expenses"),
            ("5822", "INTERNATIONAL MISSIONS:Tuttle - Rick McCorter - Ghana West Africa", "Expenses"),
            ("5823", "INTERNATIONAL MISSIONS:Tuttle - India Minister (Samual Raj)", "Expenses"),
            ("5832", "INTERNATIONAL MISSIONS:Philemon - India Blind Ministry", "Expenses"),
            ("5835", "INTERNATIONAL MISSIONS:Jerry Bates World Evangelism", "Expenses"),
            ("5837", "INTERNATIONAL MISSIONS:Rui Giogo - Brazil", "Expenses"),
            ("5840", "INTERNATIONAL MISSIONS:Student Summer Mission Requests", "Expenses"),
            ("5842", "INTERNATIONAL MISSIONS:World Bible School", "Expenses"),
            ("5850", "INTERNATIONAL MISSIONS:Nigeria Mission - Robert Okolo Support", "Expenses"),
            ("5851", "INTERNATIONAL MISSIONS:Nigeria Missions - Herb Chikwu", "Expenses"),
            ("5852", "INTERNATIONAL MISSIONS:Nigeria Mission - Preaching Students", "Expenses"),
            ("5860", "INTERNATIONAL MISSIONS:Nigeria Mission - Chad Wagner Support", "Expenses"),
            ("5861", "INTERNATIONAL MISSIONS:Nigeria Missions - Chad Wagner - Bibles", "Expenses"),
            ("5900", "BUILDING AND GROUNDS", "Expenses"),
            ("5901", "BUILDING AND GROUNDS:Gas (Utility)", "Expenses"),
            ("5902", "BUILDING AND GROUNDS:Electric (Utility)", "Expenses"),
            ("5903", "BUILDING AND GROUNDS:Water (Utility)", "Expenses"),
            ("5904", "BUILDING AND GROUNDS:Garbage Service", "Expenses"),
            ("5905", "BUILDING AND GROUNDS:Mowing Expense", "Expenses"),
            ("5906", "BUILDING AND GROUNDS:Upkeep of Grounds", "Expenses"),
            ("5907", "BUILDING AND GROUNDS:Janitorial Supplies", "Expenses"),
            ("5908", "BUILDING AND GROUNDS:Security Services", "Expenses"),
            ("5909", "BUILDING AND GROUNDS:Elevator Expenses", "Expenses"),
            ("5910", "BUILDING AND GROUNDS:Equipment", "Expenses"),
            ("5920", "BUILDING AND GROUNDS:Maintenance", "Expenses"),
            ("5930", "BUILDING AND GROUNDS:Preachers House", "Expenses"),
            ("5940", "BUILDING AND GROUNDS:Construction Expense", "Expenses"),
            ("5950", "BUILDING AND GROUNDS:I.T. Expenses (Randall)", "Expenses"),
            ("5960", "BUILDING AND GROUNDS:Copyright Insurance", "Expenses"),
            ("5970", "BUILDING AND GROUNDS:Building Insurance", "Expenses"),
            ("6000", "TRANSPORTATION", "Expenses"),
            ("6010", "TRANSPORTATION:Vehicle Maintenance", "Expenses"),
            ("6020", "TRANSPORTATION:Fuel", "Expenses"),
            ("6030", "TRANSPORTATION:Auto Insurance", "Expenses"),
            ("6040", "TRANSPORTATION:New Vehicle Expense", "Expenses"),
            ("6050", "TRANSPORTATION:Van Rental", "Expenses"),
            ("6100", "ADMINISTRATIVE EXPENSE", "Expenses"),
            ("6110", "ADMINISTRATIVE EXPENSE:Copier Expense", "Expenses"),
            ("6120", "ADMINISTRATIVE EXPENSE:Office Supplies & Expense", "Expenses"),
            ("6130", "ADMINISTRATIVE EXPENSE:Professional Fees", "Expenses"),
            ("6140", "ADMINISTRATIVE EXPENSE:Bank Service Charge", "Expenses"),
            ("6150", "ADMINISTRATIVE EXPENSE:Communications", "Expenses"),
            ("6160", "ADMINISTRATIVE EXPENSE:Dues & Subscriptions", "Expenses"),
            ("6165", "ADMINISTRATIVE EXPENSE:Workman's Comp Insurance", "Expenses"),
            ("6170", "ADMINISTRATIVE EXPENSE:Secretary Training", "Expenses"),
            ("6180", "ADMINISTRATIVE EXPENSE:Returned checks", "Expenses"),
            ("6185", "ADMINISTRATIVE EXPENSE:Postage Expense", "Expenses"),
            ("6190", "ADMINISTRATIVE EXPENSE:Unbudgeted Office Expense", "Expenses"),
            ("6200", "SALARIES & COMPENSATIONS", "Expenses"),
            ("6205", "SALARIES & COMPENSATIONS:Wages", "Expenses"),
            ("6210", "SALARIES & COMPENSATIONS:Preachers Salary", "Expenses"),
            ("6211", "SALARIES & COMPENSATIONS:Youth Minister Salary", "Expenses"),
            ("6212", "SALARIES & COMPENSATIONS:Secretary Salary", "Expenses"),
            ("6213", "SALARIES & COMPENSATIONS:Janitor Salary", "Expenses"),
            ("6214", "SALARIES & COMPENSATIONS:Bonus", "Expenses"),
            ("6225", "SALARIES & COMPENSATIONS:Medical Insurance", "Expenses"),
            ("6230", "SALARIES & COMPENSATIONS:Ministerial supplies", "Expenses"),
            ("6235", "SALARIES & COMPENSATIONS:Housing Expense", "Expenses"),
            ("6240", "SALARIES & COMPENSATIONS:Travel Expense", "Expenses"),
            ("6250", "SALARIES & COMPENSATIONS:Self-Employment Tax", "Expenses"),
            ("6260", "SALARIES & COMPENSATIONS:Interim Preaching Expense", "Expenses"),
            ("6270", "SALARIES & COMPENSATIONS:FICA", "Expenses"),
            ("6280", "SALARIES & COMPENSATIONS:State With-holding", "Expenses"),
            ("6290", "SALARIES & COMPENSATIONS:AR Unemployment Tax", "Expenses"),
            ("6295", "SALARIES & COMPENSATIONS:Federal Taxes (941/944)", "Expenses"),
            ("66900", "Reconciliation Discrepancies", "Expenses"),
        ]

        # Assign some default approvers for demo (cycle through users)
        user_ids = list(users.values())
        if not user_ids:
            user_ids = [None]

        for i, (num, name, typ) in enumerate(EXPENSE_ACCOUNTS):
            is_exp = 1 if typ == "Expenses" or num[0] in "56" else 0
            # Assign cycling approvers for demo - user can change in UI
            p = user_ids[i % len(user_ids)] if user_ids else None
            s = user_ids[(i + 1) % len(user_ids)] if len(user_ids) > 1 else None
            t = user_ids[(i + 2) % len(user_ids)] if len(user_ids) > 2 else None

            cur.execute("""
                INSERT INTO gl_accounts (account_number, name, description, is_expense,
                                         primary_approver_id, secondary_approver_id, tertiary_approver_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (num, name, "", is_exp, p, s, t))

        db.commit()
        print(f"Seeded {len(EXPENSE_ACCOUNTS)} GL accounts from church list.")

    # Optional: seed one demo request if empty
    cur.execute("SELECT COUNT(*) as c FROM requests")
    if cur.fetchone()["c"] == 0:
        cur.execute("SELECT id FROM gl_accounts WHERE is_expense=1 LIMIT 1")
        gl = cur.fetchone()
        cur.execute("SELECT id FROM users LIMIT 1")
        req_by = cur.fetchone()
        if gl and req_by:
            cur.execute("""
                INSERT INTO requests (vendor, invoice_number, invoice_date, amount, description,
                                      gl_account_id, requested_by_id, status, current_step,
                                      primary_approver_id, secondary_approver_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', 1, ?, ?)
            """, ("ABC Office Supply", "INV-78432", "2026-06-28", 245.67,
                  "Office supplies for admin - paper, toner, pens", gl["id"], req_by["id"],
                  gl["id"] if "primary" else None, None))
            db.commit()
            print("Seeded demo request.")

def dict_from_row(row):
    return {k: row[k] for k in row.keys()}

# ---------- EMAIL ----------
def log_email(to_email, subject, body, status="simulated"):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO email_log (to_email, subject, body, is_html, status)
        VALUES (?, ?, ?, 1, ?)
    """, (to_email, subject, body, status))
    db.commit()
    return cur.lastrowid

def public_base_url():
    """Public origin used in emails and token links."""
    if BASE_URL:
        return BASE_URL.rstrip("/")
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:
        return ""


def send_email(to_email, subject, text_body, html_body=None):
    """Send or simulate email. Always logs. Attempts real send if SMTP_CONFIG populated."""
    body_to_log = html_body or text_body
    status = "simulated"

    if SMTP_CONFIG.get("server") and SMTP_CONFIG.get("username"):
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = SMTP_CONFIG["from_email"]
            msg["To"] = to_email

            msg.attach(MIMEText(text_body, "plain"))
            if html_body:
                msg.attach(MIMEText(html_body, "html"))

            server_addr = SMTP_CONFIG["server"]
            port = SMTP_CONFIG["port"]
            use_tls = SMTP_CONFIG.get("use_tls", True)

            # Use SMTP_SSL for port 465 (implicit SSL), SMTP + STARTTLS otherwise
            if port == 465:
                with smtplib.SMTP_SSL(server_addr, port) as server:
                    server.login(SMTP_CONFIG["username"], SMTP_CONFIG["password"])
                    server.sendmail(SMTP_CONFIG["from_email"], [to_email], msg.as_string())
            else:
                with smtplib.SMTP(server_addr, port) as server:
                    if use_tls:
                        server.starttls()
                    server.login(SMTP_CONFIG["username"], SMTP_CONFIG["password"])
                    server.sendmail(SMTP_CONFIG["from_email"], [to_email], msg.as_string())
            status = "sent"
            print(f"[EMAIL SENT] to {to_email}")
        except Exception as e:
            print(f"[EMAIL FAILED] {e}")
            status = "failed"
    else:
        print(f"\n[EMAIL SIMULATED] To: {to_email}\nSubject: {subject}\n---\n{text_body[:300]}...\n")

    log_email(to_email, subject, body_to_log, status)
    return status

def build_approval_email(request_row, gl_row, requester, approver, step_label, next_approver_name=None):
    base_url = public_base_url()
    token = create_or_get_token(request_row["id"], request_row["current_step"], approver["id"])

    view_url = f"{base_url}/view/{token}"
    app_url = f"{base_url}/requests/{request_row['id']}"
    approve_url = f"{base_url}/approve/{token}"
    reject_url = f"{base_url}/reject/{token}"

    description = (request_row.get("description") or "").strip()
    attachments = list_attachments(request_row["id"])
    att_names = [a.get("original_filename") or "file" for a in attachments]
    att_text = ", ".join(att_names) if att_names else "None"

    subject = f"AP Approval Needed: Request #{request_row['id']} - {request_row['vendor']} (${request_row['amount']:.2f})"

    requester_name = f"{requester['first_name']} {requester['last_name']}" if requester else "Unknown"
    requester_email = (requester or {}).get("email") or ""
    gl_label = f"{gl_row['account_number']} - {gl_row['name']}" if gl_row else ""
    next_line = (
        "Next approver after you: " + next_approver_name
        if next_approver_name
        else "This is the final approver."
    )

    text = f"""Hello {approver['first_name']},

A new Accounts Payable request requires your approval.

REQUEST DETAILS
---------------
Request ID: {request_row['id']}
Vendor / Payee: {request_row['vendor']}
Invoice #: {request_row['invoice_number'] or 'N/A'}
Invoice Date: {request_row['invoice_date']}
Amount: ${request_row['amount']:.2f}

Description / Purpose:
{description or '(none provided)'}

General Ledger Coding:
  {gl_label}

Requested By: {requester_name} ({requester_email})
Attachments: {att_text}

Approval Step: {step_label}
{next_line}

Review this request (opens the request, including any attachments):
{view_url}

Or sign in to the AP system:
{app_url}

Please click one of the links below:

APPROVE: {approve_url}
REJECT:  {reject_url}

Thank you,
Johnson Church of Christ - Accounts Payable System
"""

    desc_html = html_escape(description).replace("\n", "<br>") if description else "<em>(none provided)</em>"
    att_html = html_escape(att_text)
    next_note = html_escape(next_approver_name) if next_approver_name else ""

    html = f"""<!doctype html>
<html><body style="font-family: system-ui, sans-serif; line-height:1.5; color:#222;">
  <h2 style="color:#1e40af;">Johnson Church of Christ</h2>
  <h3>Accounts Payable Request for Approval</h3>

  <p>Hello {html_escape(approver['first_name'])},</p>

  <table style="border-collapse:collapse; width:100%; max-width:560px; margin:16px 0;" border="1" cellpadding="8">
    <tr><td><strong>Request ID</strong></td><td>#{request_row['id']}</td></tr>
    <tr><td><strong>Vendor / Payee</strong></td><td>{html_escape(request_row['vendor'] or '')}</td></tr>
    <tr><td><strong>Invoice #</strong></td><td>{html_escape(request_row['invoice_number'] or 'N/A')}</td></tr>
    <tr><td><strong>Invoice Date</strong></td><td>{html_escape(str(request_row['invoice_date'] or ''))}</td></tr>
    <tr><td><strong>Amount</strong></td><td><strong>${request_row['amount']:.2f}</strong></td></tr>
    <tr><td style="vertical-align:top;"><strong>Description / Purpose</strong></td><td>{desc_html}</td></tr>
    <tr><td><strong>GL Account</strong></td><td>{html_escape(gl_label)}</td></tr>
    <tr><td><strong>Requested By</strong></td><td>{html_escape(requester_name)} &lt;{html_escape(requester_email)}&gt;</td></tr>
    <tr><td><strong>Attachments</strong></td><td>{att_html}</td></tr>
    <tr><td><strong>Current Step</strong></td><td>{html_escape(step_label)}</td></tr>
  </table>

  <p style="margin:20px 0;">
    <a href="{html_escape(view_url)}" style="background:#1e40af;color:white;padding:12px 20px;text-decoration:none;border-radius:6px;font-weight:600;margin-right:12px;display:inline-block;">View request</a>
  </p>
  <p style="color:#555;font-size:0.9em;margin-top:-8px;">Opens this request so you can review the description/purpose and any uploaded attachments before approving.</p>

  <p style="margin:20px 0;">
    <a href="{html_escape(approve_url)}" style="background:#16a34a;color:white;padding:12px 20px;text-decoration:none;border-radius:6px;font-weight:600;margin-right:12px;display:inline-block;">✓ APPROVE</a>
    <a href="{html_escape(reject_url)}" style="background:#dc2626;color:white;padding:12px 20px;text-decoration:none;border-radius:6px;font-weight:600;display:inline-block;">✕ REJECT</a>
  </p>

  <p style="color:#555;font-size:0.9em;">If approved, this request will be routed to the next approver{(' (' + next_note + ')') if next_note else ''}.</p>
  <p style="color:#555;font-size:0.85em;"><a href="{html_escape(app_url)}">Open in the AP system</a> (sign-in required)</p>
  <p style="color:#555;font-size:0.85em;">Johnson Church of Christ • Accounts Payable System • {datetime.now().strftime('%Y-%m-%d')}</p>
</body></html>"""

    return subject, text, html

def create_or_get_token(request_id, step, approver_id):
    db = get_db()
    cur = db.cursor()
    # Check if existing pending token for this exact step
    cur.execute("""
        SELECT token FROM pending_approvals 
        WHERE request_id=? AND step=? AND approver_id=?
    """, (request_id, step, approver_id))
    row = cur.fetchone()
    if row:
        return row["token"]

    token = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO pending_approvals (request_id, step, approver_id, token)
        VALUES (?, ?, ?, ?)
    """, (request_id, step, approver_id, token))
    db.commit()
    return token

def lookup_pending_token(token):
    """Return pending approval row for a token without consuming it."""
    if not token:
        return None
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT request_id, step, approver_id, token FROM pending_approvals WHERE token=?",
        (token,),
    )
    row = cur.fetchone()
    return dict_from_row(row) if row else None


def consume_token(token):
    """Return (request_id, step, approver_id) or None. Deletes the token."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT request_id, step, approver_id FROM pending_approvals WHERE token=?", (token,))
    row = cur.fetchone()
    if not row:
        return None
    cur.execute("DELETE FROM pending_approvals WHERE token=?", (token,))
    db.commit()
    return dict_from_row(row)

def get_user(user_id):
    if not user_id:
        return None
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if row:
        u = dict_from_row(row)
        u.pop("password_hash", None)
        if not u.get("role"):
            u["role"] = ROLE_USER
        return u
    return None

def get_gl(gl_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM gl_accounts WHERE id=?", (gl_id,))
    row = cur.fetchone()
    return enrich_gl(dict_from_row(row)) if row else None


def enrich_attachment(att):
    d = dict(att) if isinstance(att, dict) else dict_from_row(att)
    d["kind"] = attachment_kind(d)
    if d.get("size_bytes"):
        kb = d["size_bytes"] / 1024
        d["size_label"] = f"{kb:.0f} KB" if kb >= 1 else f"{d['size_bytes']} B"
    else:
        d["size_label"] = ""
    return d


def list_attachments(request_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, request_id, original_filename, content_type, size_bytes, uploaded_by_id, uploaded_at "
        "FROM request_attachments WHERE request_id=? ORDER BY uploaded_at",
        (request_id,),
    )
    return [enrich_attachment(r) for r in cur.fetchall()]


def send_attachment_response(att):
    """Send a stored attachment; images and PDFs display inline unless download=1."""
    if att is None:
        return jsonify({"error": "Attachment not found"}), 404
    if not isinstance(att, dict):
        att = dict_from_row(att)
    path = os.path.join(UPLOAD_FOLDER, att.get("stored_filename") or "")
    if not os.path.isfile(path):
        return jsonify({"error": "File missing on server"}), 404
    kind = attachment_kind(att)
    force_download = request.args.get("download", "").lower() in ("1", "true", "yes")
    force_inline = request.args.get("inline", "").lower() in ("1", "true", "yes")
    if force_download:
        as_attachment = True
    elif force_inline:
        as_attachment = False
    else:
        as_attachment = kind == "file"
    return send_file(
        path,
        mimetype=att.get("content_type") or "application/octet-stream",
        as_attachment=as_attachment,
        download_name=att.get("original_filename") or "attachment",
        max_age=0,
    )


def send_approval_complete_notice(req, gl, recipient):
    if not recipient:
        return
    view_url = f"{public_base_url()}/requests/{req['id']}"
    description = (req.get("description") or "").strip() or "(none provided)"
    subject = f"AP Request #{req['id']} FULLY APPROVED - {req['vendor']}"
    body = f"""Hello {recipient['first_name']},

Good news — AP request #{req['id']} has received all required approvals and is now APPROVED.

Request #{req['id']}
Vendor: {req['vendor']}
Amount: ${req['amount']:.2f}
Description / Purpose: {description}
GL Account: {gl['account_number'] if gl else ''} - {gl['name'] if gl else ''}

Approved on: {datetime.now().strftime('%Y-%m-%d %H:%M')}

View this request:
{view_url}

Thank you,
Johnson Church of Christ
"""
    send_email(recipient["email"], subject, body)

def get_request(req_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM requests WHERE id=?", (req_id,))
    row = cur.fetchone()
    return dict_from_row(row) if row else None


def user_full_name(user):
    if not user:
        return ""
    name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    return name or user.get("username") or ""


def normalize_contribution_date(value):
    """Store and compare contribution dates as YYYY-MM-DD."""
    if value is None or value == "":
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        n = float(value)
        if 20000 <= n <= 80000:
            return (datetime(1899, 12, 30) + timedelta(days=n)).strftime("%Y-%m-%d")
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            n = float(text)
            if 20000 <= n <= 80000:
                return (datetime(1899, 12, 30) + timedelta(days=n)).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            pass
    text = text.replace("T", " ")
    candidates = [text]
    if " " in text:
        candidates.append(text.split(" ", 1)[0])
    for candidate in candidates:
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%m/%d/%Y",
            "%m/%d/%y",
            "%m-%d-%Y",
            "%m-%d-%y",
        ):
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return ""


def format_display_date(value):
    """Readable date for contribution lists and letters, e.g. Jan 4, 2026."""
    iso = normalize_contribution_date(value)
    if not iso:
        return format_display_dt(value) if value else "—"
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d")
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    except ValueError:
        return format_display_dt(value)


def format_display_dt(value):
    """Turn SQLite timestamps into a readable date/time for print and UI."""
    if not value:
        return "—"
    raw = str(value).strip()
    text = raw.replace("T", " ")
    dt = None
    matched_date_only = False
    for fmt, width in (
        ("%Y-%m-%d %H:%M:%S.%f", 26),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        chunk = text[: min(len(text), width)]
        try:
            dt = datetime.strptime(chunk, fmt)
            matched_date_only = fmt == "%Y-%m-%d"
            break
        except ValueError:
            continue
    if dt is None:
        return raw
    date_part = dt.strftime("%b %d, %Y")
    if matched_date_only or (" " not in raw and "T" not in raw):
        return date_part
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{date_part}, {hour}:{dt.strftime('%M %p')}"


def format_money(amount):
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return "—"


def attachment_kind(att):
    name = (att.get("original_filename") or "").lower()
    ct = (att.get("content_type") or "").lower()
    if name.endswith((".heic", ".heif")) or "heic" in ct or "heif" in ct:
        return "file"
    if ct.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return "image"
    if "pdf" in ct or name.endswith(".pdf"):
        return "pdf"
    return "file"


def fetch_approval_history(request_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT h.*, u.first_name, u.last_name, u.email, u.username
        FROM approval_history h
        LEFT JOIN users u ON u.id = h.approver_id
        WHERE h.request_id=?
        ORDER BY h.acted_at, h.id
        """,
        (request_id,),
    )
    rows = []
    for row in cur.fetchall():
        d = dict_from_row(row)
        d["approver_name"] = (
            f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip()
            or d.get("username")
            or (f"User #{d['approver_id']}" if d.get("approver_id") else "Unknown")
        )
        d["acted_at_display"] = format_display_dt(d.get("acted_at"))
        rows.append(d)
    return rows


def build_print_context(req, printer=None):
    """Assemble a one-sheet request summary plus printable attachments."""
    gl = get_gl(req.get("gl_account_id"))
    requester = get_user(req.get("requested_by_id"))
    notify_user = get_user(req.get("notify_user_id")) if req.get("notify_user_id") else None
    history = fetch_approval_history(req["id"])
    history_by_step = {}
    for h in history:
        history_by_step.setdefault(h.get("step"), []).append(h)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT step, approver_id, created_at FROM pending_approvals WHERE request_id=?",
        (req["id"],),
    )
    pending_by_step = {row["step"]: dict_from_row(row) for row in cur.fetchall()}

    status = (req.get("status") or "Pending").strip()
    current_step = req.get("current_step") or 1
    assigned_steps = [step for step, key, _role in APPROVER_STEPS if req.get(key)]
    last_assigned = assigned_steps[-1] if assigned_steps else None

    approvers = []
    for step, key, role in APPROVER_STEPS:
        uid = req.get(key)
        if not uid:
            continue
        user = get_user(uid)
        events = history_by_step.get(step) or []
        latest = events[-1] if events else None
        action = "Waiting"
        action_key = "waiting"
        acted_at = "—"
        notes = ""
        actor_name = user_full_name(user) or f"User #{uid}"
        if latest:
            act = (latest.get("action") or "").lower()
            if act == "approved":
                action, action_key = "Approved", "approved"
            elif act == "rejected":
                action, action_key = "Rejected", "rejected"
            else:
                action, action_key = (act.title() or "Recorded"), "other"
            acted_at = latest.get("acted_at_display") or "—"
            notes = latest.get("notes") or ""
            actor_name = latest.get("approver_name") or actor_name
        elif status == "Approved" or (status == "Pending" and current_step > step):
            action, action_key = "Approved", "approved"
            if status == "Approved" and last_assigned == step and req.get("approved_at"):
                acted_at = format_display_dt(req.get("approved_at"))
        elif status == "Rejected" and current_step == step:
            action, action_key = "Rejected", "rejected"
            acted_at = format_display_dt(req.get("rejected_at"))
            notes = req.get("reject_reason") or ""
        elif status == "Rejected" and current_step < step:
            action, action_key = "Not reached", "waiting"
        elif status == "Pending" and current_step == step:
            action, action_key = "Awaiting review", "pending"
            pending = pending_by_step.get(step)
            if pending and pending.get("created_at"):
                notes = f"Request sent {format_display_dt(pending['created_at'])}"
        elif status == "Pending" and current_step < step:
            action, action_key = "Waiting on prior approval", "waiting"

        approvers.append({
            "step": step,
            "role": f"{role} Approver",
            "name": actor_name,
            "email": (user or {}).get("email") or "",
            "action": action,
            "action_key": action_key,
            "acted_at": acted_at,
            "notes": notes,
        })

    attachments = []
    for att in list_attachments(req["id"]):
        att = dict(att)
        att["inline_url"] = url_for("api_attachment", att_id=att["id"], inline=1)
        att["download_url"] = url_for("api_attachment", att_id=att["id"], download=1)
        attachments.append(att)

    gl_number = (gl or {}).get("account_number") or ""
    gl_name = (gl or {}).get("name") or ""
    gl_category = (gl or {}).get("category") or ""
    gl_account_name = (gl or {}).get("account_name") or gl_name

    return {
        "req": req,
        "status": status,
        "amount_display": format_money(req.get("amount")),
        "invoice_number": req.get("invoice_number") or "—",
        "invoice_date": format_display_dt(req.get("invoice_date")),
        "submitted_at": format_display_dt(req.get("created_at")),
        "approved_at": format_display_dt(req.get("approved_at")) if req.get("approved_at") else None,
        "rejected_at": format_display_dt(req.get("rejected_at")) if req.get("rejected_at") else None,
        "reject_reason": (req.get("reject_reason") or "").strip(),
        "description": (req.get("description") or "").strip() or "—",
        "vendor": req.get("vendor") or "—",
        "requester_name": user_full_name(requester) or "—",
        "requester_email": (requester or {}).get("email") or "",
        "notify_name": user_full_name(notify_user) if notify_user else "",
        "gl": gl,
        "gl_number": gl_number,
        "gl_name": gl_name,
        "gl_category": gl_category,
        "gl_account_name": gl_account_name,
        "gl_display": f"{gl_number} — {gl_name}".strip(" —") if gl else "—",
        "approvers": approvers,
        "history": history,
        "attachments": attachments,
        "image_attachments": [a for a in attachments if a["kind"] == "image"],
        "pdf_attachments": [a for a in attachments if a["kind"] == "pdf"],
        "other_attachments": [a for a in attachments if a["kind"] == "file"],
        "printable_attachment_count": len([a for a in attachments if a["kind"] in ("image", "pdf")]),
        "printer_name": user_full_name(printer) or (printer or {}).get("username") or "",
        "printed_at": format_display_dt(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "autoprint": request.args.get("preview") != "1",
    }

# ---------- WORKFLOW ----------
def get_approver_chain(gl):
    chain = []
    for key in ["primary_approver_id", "secondary_approver_id", "tertiary_approver_id"]:
        uid = gl.get(key)
        if uid:
            u = get_user(uid)
            if u:
                chain.append(u)
    return chain

def start_workflow(request_id):
    """Send first email for a newly created request."""
    req = get_request(request_id)
    if not req or req["status"] != "Pending":
        return

    gl = get_gl(req["gl_account_id"])
    if not gl:
        return

    chain = get_approver_chain(gl)
    if not chain:
        # No approvers configured — leave as pending, admin must assign
        print(f"Warning: No approvers on GL {gl['account_number']}")
        return

    # Snapshot approvers into request if not already
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE requests SET 
            primary_approver_id = COALESCE(primary_approver_id, ?),
            secondary_approver_id = COALESCE(secondary_approver_id, ?),
            tertiary_approver_id = COALESCE(tertiary_approver_id, ?)
        WHERE id=?
    """, (gl.get("primary_approver_id"), gl.get("secondary_approver_id"), gl.get("tertiary_approver_id"), request_id))
    db.commit()

    # Send to first
    first = chain[0]
    requester = get_user(req["requested_by_id"])
    step_label = "Primary Approver"

    next_name = chain[1]["first_name"] + " " + chain[1]["last_name"] if len(chain) > 1 else None

    subject, text, html = build_approval_email(req, gl, requester, first, step_label, next_name)
    send_email(first["email"], subject, text, html)

    # Update current_step to 1
    cur.execute("UPDATE requests SET current_step=1 WHERE id=?", (request_id,))
    db.commit()

def advance_or_complete(request_id, approver_id, action="approved", notes=None):
    """Record action, advance workflow or finalize."""
    db = get_db()
    cur = db.cursor()

    req = get_request(request_id)
    if not req:
        return False, "Request not found"

    if req["status"] != "Pending":
        return False, "Request is no longer pending"

    gl = get_gl(req["gl_account_id"])
    requester = get_user(req["requested_by_id"])
    approver = get_user(approver_id)

    # Record history
    cur.execute("""
        INSERT INTO approval_history (request_id, step, approver_id, action, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (request_id, req["current_step"], approver_id, action, notes))
    db.commit()

    if action == "rejected":
        cur.execute("""
            UPDATE requests SET status='Rejected', rejected_at=datetime('now'), reject_reason=?
            WHERE id=?
        """, (notes or "Rejected by approver", request_id))
        db.commit()

        # Notify requester
        if requester:
            subject = f"AP Request #{request_id} REJECTED - {req['vendor']}"
            body = f"""Hello {requester['first_name']},

Your accounts payable request has been rejected.

Request #{request_id}
Vendor: {req['vendor']}
Amount: ${req['amount']:.2f}
GL: {gl['account_number']} - {gl['name'] if gl else ''}

Reason: {notes or 'No reason provided'}
Description / Purpose: {(req.get('description') or '').strip() or '(none provided)'}

View this request:
{public_base_url()}/requests/{request_id}

Please review and resubmit if needed.

Thank you,
Johnson Church of Christ AP System
"""
            send_email(requester["email"], subject, body)
        return True, "Request rejected. Requester notified."

    # APPROVED - advance
    chain = []
    for k in ["primary_approver_id", "secondary_approver_id", "tertiary_approver_id"]:
        if req.get(k):
            u = get_user(req[k])
            if u:
                chain.append(u)

    current_step = req["current_step"]
    next_step = current_step + 1

    if next_step > len(chain):
        # Final approval
        cur.execute("""
            UPDATE requests SET status='Approved', approved_at=datetime('now'), current_step=?
            WHERE id=?
        """, (next_step, request_id))
        db.commit()

        # Notify requester and optional additional notice recipient
        send_approval_complete_notice(req, gl, requester)
        notify_extra = get_user(req.get("notify_user_id")) if req.get("notify_user_id") else None
        if notify_extra and (not requester or notify_extra["id"] != requester["id"]):
            send_approval_complete_notice(req, gl, notify_extra)
        return True, "Request fully approved!"

    # Route to next
    next_approver = chain[next_step - 1]
    cur.execute("UPDATE requests SET current_step=? WHERE id=?", (next_step, request_id))
    db.commit()

    # Send email to next
    step_labels = {1: "Primary", 2: "Secondary", 3: "Tertiary"}
    step_label = f"{step_labels.get(next_step, 'Step ' + str(next_step))} Approver"

    next_next = chain[next_step] if next_step < len(chain) else None
    next_next_name = f"{next_next['first_name']} {next_next['last_name']}" if next_next else None

    subject, text, html = build_approval_email(req, gl, requester, next_approver, step_label, next_next_name)
    send_email(next_approver["email"], subject, text, html)

    return True, f"Approved. Routed to {next_approver['first_name']} {next_approver['last_name']}."

# ---------- ROUTES: PAGES & AUTH ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(safe_next_url(request.args.get("next")))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"

        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
        row = cur.fetchone()

        if row and row["password_hash"] and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["id"]
            session["username"] = row["username"]
            session["display_name"] = f"{row['first_name']} {row['last_name']}"
            session["role"] = row["role"] if "role" in row.keys() and row["role"] else ROLE_USER
            session.permanent = bool(remember)
            next_url = safe_next_url(request.args.get("next") or request.form.get("next"))
            return redirect(next_url)

        error = "Invalid username or password."

    return render_template("login.html", error=error, next=request.args.get("next", ""), message=None)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = None
    if request.method == "POST":
        identity = (request.form.get("username") or request.form.get("email") or "").strip()
        # Always show the same success message (do not reveal whether the user exists)
        message = "If an account matches that username or email, a reset link has been sent."
        if identity:
            db = get_db()
            cur = db.cursor()
            cur.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
                (identity, identity),
            )
            row = cur.fetchone()
            if row and row["email"]:
                token = str(uuid.uuid4())
                expires = (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
                    (row["id"], token, expires),
                )
                db.commit()
                if BASE_URL:
                    base = BASE_URL.rstrip("/")
                else:
                    base = request.host_url.rstrip("/")
                reset_url = f"{base}/reset-password/{token}"
                body = f"""Hello {row['first_name']},

A password reset was requested for your Accounts Payable account ({row['username']}).

Open this link within 2 hours to set a new password:
{reset_url}

If you did not request this, you can ignore this email.

Johnson Church of Christ AP System
"""
                send_email(row["email"], "AP System password reset", body)
    return render_template("login.html", mode="forgot", error=error, message=message, next="")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0",
        (token,),
    )
    row = cur.fetchone()
    error = None
    message = None
    valid = False
    if row:
        try:
            exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
            valid = exp >= datetime.utcnow()
        except ValueError:
            valid = False
    if not row or not valid:
        return render_template(
            "login.html",
            mode="reset",
            error="This reset link is invalid or has expired. Please request a new one.",
            message=None,
            token=token,
            next="",
        )

    if request.method == "POST":
        pw = request.form.get("password") or ""
        pw2 = request.form.get("password_confirm") or ""
        if len(pw) < 6:
            error = "Password must be at least 6 characters."
        elif pw != pw2:
            error = "Passwords do not match."
        else:
            cur.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(pw), row["user_id"]),
            )
            cur.execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (row["id"],))
            db.commit()
            return render_template(
                "login.html",
                mode="login",
                error=None,
                message="Password updated. You can sign in with your new password.",
                next="",
            )

    return render_template(
        "login.html", mode="reset", error=error, message=message, token=token, next=""
    )


@app.route("/")
def index():
    u = current_user() or {}
    return render_template(
        "index.html",
        current_user={
            "id": u.get("id") or session.get("user_id"),
            "username": u.get("username") or session.get("username"),
            "display_name": (
                f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                or session.get("display_name")
            ),
            "role": u.get("role") or session.get("role") or ROLE_USER,
            "email": u.get("email") or "",
        },
    )


@app.route("/requests/<int:req_id>")
def open_request(req_id):
    """Deep link into the app on a specific request (sign-in required)."""
    me = current_user()
    req = get_request(req_id)
    if not req:
        abort(404)
    if not user_can_view_request(me, req):
        abort(403)
    return redirect(url_for("index", req=req_id))


@app.route("/view/<token>")
def view_request_email(token):
    """Public request view from an approval email. Does not consume the token."""
    data = lookup_pending_token(token)
    if not data:
        return (
            "<html><body style='font-family:sans-serif;padding:2rem;max-width:520px;margin:auto;'>"
            "<h2>This view link is invalid or has already been used.</h2>"
            "<p>If the request was already approved or rejected, sign in to the AP system to open it.</p>"
            "<p><a href='/'>Return to AP System</a></p>"
            "</body></html>"
        ), 410

    req = get_request(data["request_id"])
    if not req:
        abort(404)

    ctx = build_print_context(req)
    ctx["autoprint"] = False
    ctx["token"] = token
    ctx["approve_url"] = url_for("approve_link", token=token)
    ctx["reject_url"] = url_for("reject_link", token=token)
    ctx["app_url"] = url_for("open_request", req_id=req["id"])
    for att in ctx["attachments"]:
        att["inline_url"] = url_for(
            "view_request_email_attachment", token=token, att_id=att["id"], inline=1
        )
        att["download_url"] = url_for(
            "view_request_email_attachment", token=token, att_id=att["id"], download=1
        )
    return render_template("view_request.html", **ctx)


@app.route("/view/<token>/attachments/<int:att_id>")
def view_request_email_attachment(token, att_id):
    """Serve an attachment to someone holding a valid approval-view token."""
    data = lookup_pending_token(token)
    if not data:
        abort(403)
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM request_attachments WHERE id=?", (att_id,))
    att = cur.fetchone()
    if not att or att["request_id"] != data["request_id"]:
        abort(404)
    return send_attachment_response(att)


@app.route("/requests/<int:req_id>/print")
def print_request(req_id):
    """One-sheet request summary (plus printable attachments on following pages)."""
    me = current_user()
    req = get_request(req_id)
    if not req:
        abort(404)
    if not user_can_view_request(me, req):
        abort(403)
    return render_template("print_request.html", **build_print_context(req, printer=me))


@app.route("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify(u)


@app.route("/api/change_password", methods=["POST"])
def api_change_password():
    u = current_user()
    if not u:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""
    if len(new_pw) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT password_hash FROM users WHERE id=?", (u["id"],))
    row = cur.fetchone()
    if not row or not check_password_hash(row["password_hash"] or "", current_pw):
        return jsonify({"error": "Current password is incorrect"}), 400
    cur.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new_pw), u["id"]),
    )
    db.commit()
    return jsonify({"success": True, "message": "Password changed"})


@app.route("/approve/<token>")
def approve_link(token):
    data = consume_token(token)
    if not data:
        return "<h3>Invalid or already used approval link.</h3><p><a href='/'>Return to AP System</a></p>"

    ok, msg = advance_or_complete(data["request_id"], data["approver_id"], "approved")
    return f"""
    <html><body style="font-family:sans-serif;padding:2rem;max-width:520px;margin:auto;">
      <h2 style="color:#166534;">Approval Recorded</h2>
      <p>{msg}</p>
      <p><a href="/" style="color:#1e40af;">← Back to Accounts Payable System</a></p>
      <p style="color:#666;font-size:0.85em;">Request #{data['request_id']} • Step {data['step']}</p>
    </body></html>
    """

@app.route("/reject/<token>")
def reject_link(token):
    data = consume_token(token)
    if not data:
        return "<h3>Invalid or already used reject link.</h3><p><a href='/'>Return to AP System</a></p>"

    # Ask for reason via simple form? For email button simplicity, just reject with default.
    # For better UX we could redirect to form, but for one-click: direct reject.
    ok, msg = advance_or_complete(data["request_id"], data["approver_id"], "rejected", "Rejected via email link")
    return f"""
    <html><body style="font-family:sans-serif;padding:2rem;max-width:520px;margin:auto;">
      <h2 style="color:#991b1b;">Request Rejected</h2>
      <p>{msg}</p>
      <p><a href="/" style="color:#1e40af;">← Back to Accounts Payable System</a></p>
      <p style="color:#666;font-size:0.85em;">Request #{data['request_id']} • Step {data['step']}</p>
    </body></html>
    """

# ---------- API ROUTES ----------
@app.route("/api/users", methods=["GET", "POST"])
def api_users():
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        # All authenticated users can list users (needed for dropdowns)
        cur.execute("SELECT * FROM users ORDER BY last_name, first_name")
        users = []
        for r in cur.fetchall():
            u = dict_from_row(r)
            u.pop("password_hash", None)
            if not u.get("role"):
                u["role"] = ROLE_USER
            users.append(u)
        return jsonify(users)

    denied = require_admin_api()
    if denied:
        return denied

    # POST create
    data = request.get_json() or request.form
    pw = (data.get("password") or "").strip()
    if not pw:
        return jsonify({"error": "Password is required for new users"}), 400
    role = data.get("role") or ROLE_USER
    if role not in (ROLE_ADMIN, ROLE_USER):
        role = ROLE_USER
    pw_hash = generate_password_hash(pw)
    try:
        cur.execute("""
            INSERT INTO users (username, first_name, last_name, email, password_hash, role)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (data["username"], data["first_name"], data["last_name"], data["email"], pw_hash, role))
        db.commit()
        uid = cur.lastrowid
        return jsonify(get_user(uid)), 201
    except sqlite3.IntegrityError as e:
        return jsonify({"error": "Username or email already exists"}), 400

@app.route("/api/users/<int:user_id>", methods=["GET", "PUT", "DELETE"])
def api_user(user_id):
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        u = get_user(user_id)
        return jsonify(u) if u else ("", 404)

    denied = require_admin_api()
    if denied:
        return denied

    if request.method == "DELETE":
        me = current_user()
        if me and me["id"] == user_id:
            return jsonify({"error": "You cannot delete your own account"}), 400
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()
        return "", 204

    # PUT
    data = request.get_json() or {}
    fields = ["username", "first_name", "last_name", "email"]
    values = [data.get(f) for f in fields]
    role = data.get("role") or ROLE_USER
    if role not in (ROLE_ADMIN, ROLE_USER):
        role = ROLE_USER

    # Prevent demoting the last administrator
    if role != ROLE_ADMIN:
        cur.execute("SELECT role FROM users WHERE id=?", (user_id,))
        existing = cur.fetchone()
        if existing and existing["role"] == ROLE_ADMIN:
            cur.execute("SELECT COUNT(*) as c FROM users WHERE role=?", (ROLE_ADMIN,))
            if cur.fetchone()["c"] <= 1:
                return jsonify({"error": "Cannot remove the last Administrator"}), 400

    if data.get("password"):
        pw_hash = generate_password_hash(data["password"])
        cur.execute("""
            UPDATE users SET username=?, first_name=?, last_name=?, email=?, password_hash=?, role=?
            WHERE id=?
        """, (values[0], values[1], values[2], values[3], pw_hash, role, user_id))
    else:
        cur.execute("""
            UPDATE users SET username=?, first_name=?, last_name=?, email=?, role=?
            WHERE id=?
        """, (*values, role, user_id))

    db.commit()
    return jsonify(get_user(user_id))

def _gl_name_from_payload(data):
    if data.get("category") is not None or data.get("account_name") is not None:
        return compose_gl_name(data.get("category"), data.get("account_name") or data.get("name"))
    return data.get("name") or ""


def _normalize_header(h):
    if h is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(h).strip().lower())


def _map_gl_header(headers):
    """Map flexible column headers to field keys."""
    aliases = {
        "account_number": {
            "accountnumber", "account", "accountno", "accountnum", "acct",
            "acctnumber", "acctno", "acctnum", "account#", "acct#", "number", "no",
        },
        "name": {
            "name", "fullname", "accountname", "accountfullname", "glname", "title",
        },
        "category": {"category", "acctcategory", "accountcategory", "class"},
        "account_name": {"accountnameonly", "subaccount", "detailname"},
        "description": {"description", "desc", "notes", "memo"},
        "type": {"type", "accounttype", "accttype"},
        "is_expense": {"isexpense", "expense", "expenseaccount"},
    }
    # Prefer exact "account#" style after normalize strips #
    normalized = [_normalize_header(h) for h in headers]
    mapping = {}
    for field, keys in aliases.items():
        for i, nh in enumerate(normalized):
            if nh in keys and field not in mapping:
                mapping[field] = i
                break
    return mapping


def _parse_is_expense(val, type_val=None):
    if val is not None and str(val).strip() != "":
        s = str(val).strip().lower()
        if s in ("1", "true", "yes", "y", "expense", "expenses"):
            return 1
        if s in ("0", "false", "no", "n"):
            return 0
    if type_val is not None:
        t = str(type_val).strip().lower()
        if "expense" in t:
            return 1
        if t in ("bank", "income", "asset", "liability", "equity", "other current asset",
                 "other current assets", "fixed asset", "other asset", "credit card",
                 "other current liability", "long term liability"):
            return 0
    return 1  # default expense for AP coding


def _rows_from_csv(file_storage):
    raw = file_storage.read()
    if isinstance(raw, bytes):
        # Try utf-8-sig then latin-1
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    else:
        text = raw
    # Sniff delimiter
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return [list(row) for row in reader]


def _rows_from_xlsx(file_storage):
    if openpyxl is None:
        raise RuntimeError("openpyxl is not installed on the server")
    data = file_storage.read()
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([("" if c is None else c) for c in row])
    wb.close()
    return rows


def _find_header_row(rows, max_scan=25):
    """Find the first row that looks like column headers for a GL import."""
    for i, row in enumerate(rows[:max_scan]):
        mapping = _map_gl_header(row)
        if "account_number" in mapping and ("name" in mapping or "account_name" in mapping):
            return i, mapping
    return None, {}


def parse_gl_import_rows(rows):
    """Parse spreadsheet rows into list of dicts: account_number, name, description, is_expense."""
    if not rows:
        return [], "File is empty"

    header_idx, mapping = _find_header_row(rows)
    if header_idx is None:
        # Assume first row is data with columns: account_number, name [, description]
        # or account_number, category, account_name
        parsed = []
        for i, row in enumerate(rows):
            if not row or all(str(c).strip() == "" for c in row):
                continue
            acct = str(row[0]).strip() if len(row) > 0 else ""
            if not acct or not re.search(r"\d", acct):
                continue
            if len(row) >= 3 and row[1] and ":" not in str(row[1]) and row[2]:
                name = compose_gl_name(row[1], row[2])
                desc = str(row[3]).strip() if len(row) > 3 else ""
            else:
                name = str(row[1]).strip() if len(row) > 1 else ""
                desc = str(row[2]).strip() if len(row) > 2 else ""
            if not name:
                continue
            parsed.append({
                "account_number": acct,
                "name": name,
                "description": desc,
                "is_expense": 1,
            })
        if not parsed:
            return [], (
                "Could not detect headers. Include columns like "
                "'Account #' and 'Full name' (or account_number, name)."
            )
        return parsed, None

    parsed = []
    for row in rows[header_idx + 1:]:
        if not row or all(str(c).strip() == "" for c in row):
            continue

        def cell(field):
            idx = mapping.get(field)
            if idx is None or idx >= len(row):
                return None
            v = row[idx]
            if v is None:
                return None
            return str(v).strip()

        acct = cell("account_number") or ""
        # Skip title rows / non-numeric account codes without digits
        if not acct or not re.search(r"\d", acct):
            continue
        # Skip repeated header-like rows
        if _normalize_header(acct) in ("account", "accountnumber", "acct"):
            continue

        category = cell("category")
        account_name = cell("account_name")
        full_name = cell("name") or ""
        if category or account_name:
            name = compose_gl_name(category, account_name or full_name)
        else:
            name = full_name
        if not name:
            continue

        desc = cell("description") or ""
        is_exp = _parse_is_expense(cell("is_expense"), cell("type"))
        parsed.append({
            "account_number": acct,
            "name": name,
            "description": desc,
            "is_expense": is_exp,
        })

    if not parsed:
        return [], "No account rows found after the header row"
    return parsed, None


@app.route("/api/gl_accounts/import", methods=["POST"])
def api_gl_import():
    """Admin-only: import GL accounts from CSV or Excel (.xlsx).

    Upserts by account_number (updates name/description/is_expense; keeps approvers).
    Query/form option: mode=insert_only to skip existing account numbers.
    """
    denied = require_admin_api()
    if denied:
        return denied

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded (use form field name 'file')"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "No file selected"}), 400

    filename = secure_filename(f.filename)
    lower = filename.lower()
    mode = (request.form.get("mode") or request.args.get("mode") or "upsert").strip().lower()
    insert_only = mode in ("insert_only", "insert", "add")

    try:
        if lower.endswith(".csv") or lower.endswith(".txt"):
            rows = _rows_from_csv(f)
        elif lower.endswith(".xlsx"):
            rows = _rows_from_xlsx(f)
        elif lower.endswith(".xls"):
            return jsonify({
                "error": "Legacy .xls is not supported. Save as .xlsx or CSV and try again."
            }), 400
        else:
            return jsonify({"error": "Unsupported file type. Upload .csv or .xlsx"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400

    accounts, err = parse_gl_import_rows(rows)
    if err:
        return jsonify({"error": err}), 400

    db = get_db()
    cur = db.cursor()
    created = 0
    updated = 0
    skipped = 0
    errors = []

    for acct in accounts:
        num = acct["account_number"]
        cur.execute("SELECT id FROM gl_accounts WHERE account_number = ?", (num,))
        existing = cur.fetchone()
        try:
            if existing:
                if insert_only:
                    skipped += 1
                    continue
                cur.execute("""
                    UPDATE gl_accounts
                    SET name = ?, description = ?, is_expense = ?
                    WHERE id = ?
                """, (acct["name"], acct.get("description") or "", acct.get("is_expense", 1), existing["id"]))
                updated += 1
            else:
                cur.execute("""
                    INSERT INTO gl_accounts (account_number, name, description, is_expense)
                    VALUES (?, ?, ?, ?)
                """, (num, acct["name"], acct.get("description") or "", acct.get("is_expense", 1)))
                created += 1
        except Exception as e:
            errors.append(f"{num}: {e}")

    db.commit()
    return jsonify({
        "success": True,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:20],
        "total_rows": len(accounts),
        "message": (
            f"Import complete: {created} created, {updated} updated"
            + (f", {skipped} skipped" if skipped else "")
            + (f", {len(errors)} errors" if errors else "")
            + "."
        ),
    })


@app.route("/api/gl_accounts", methods=["GET", "POST"])
def api_gl():
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        cur.execute("""
            SELECT g.*, 
                   u1.first_name || ' ' || u1.last_name as primary_name,
                   u2.first_name || ' ' || u2.last_name as secondary_name,
                   u3.first_name || ' ' || u3.last_name as tertiary_name
            FROM gl_accounts g
            LEFT JOIN users u1 ON g.primary_approver_id = u1.id
            LEFT JOIN users u2 ON g.secondary_approver_id = u2.id
            LEFT JOIN users u3 ON g.tertiary_approver_id = u3.id
            ORDER BY CAST(g.account_number AS TEXT)
        """)
        rows = []
        for r in cur.fetchall():
            d = enrich_gl(dict_from_row(r))
            d["primary_name"] = r["primary_name"]
            d["secondary_name"] = r["secondary_name"]
            d["tertiary_name"] = r["tertiary_name"]
            rows.append(d)
        return jsonify(rows)

    denied = require_admin_api()
    if denied:
        return denied

    # POST create
    data = request.get_json()
    name = _gl_name_from_payload(data)
    cur.execute("""
        INSERT INTO gl_accounts (account_number, name, description, is_expense,
                                 primary_approver_id, secondary_approver_id, tertiary_approver_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data["account_number"], name, data.get("description", ""),
        1 if data.get("is_expense", True) else 0,
        data.get("primary_approver_id"), data.get("secondary_approver_id"), data.get("tertiary_approver_id")
    ))
    db.commit()
    return jsonify(get_gl(cur.lastrowid)), 201

@app.route("/api/gl_accounts/<int:gl_id>", methods=["GET", "PUT", "DELETE"])
def api_gl_one(gl_id):
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        gl = get_gl(gl_id)
        return jsonify(gl) if gl else ("", 404)

    denied = require_admin_api()
    if denied:
        return denied

    if request.method == "DELETE":
        cur.execute("DELETE FROM gl_accounts WHERE id=?", (gl_id,))
        db.commit()
        return "", 204

    data = request.get_json()
    name = _gl_name_from_payload(data)
    cur.execute("""
        UPDATE gl_accounts SET
            account_number=?, name=?, description=?, is_expense=?,
            primary_approver_id=?, secondary_approver_id=?, tertiary_approver_id=?
        WHERE id=?
    """, (
        data["account_number"], name, data.get("description", ""),
        1 if data.get("is_expense", True) else 0,
        data.get("primary_approver_id"), data.get("secondary_approver_id"), data.get("tertiary_approver_id"),
        gl_id
    ))
    db.commit()
    return jsonify(get_gl(gl_id))

@app.route("/api/requests", methods=["GET", "POST"])
def api_requests():
    db = get_db()
    cur = db.cursor()
    me = current_user()

    if request.method == "POST":
        data = request.get_json() or {}
        # Non-admins may only create requests as themselves
        requested_by = int(data.get("requested_by_id") or (me["id"] if me else 0))
        if me and not is_admin(me):
            requested_by = me["id"]
        notify_user_id = data.get("notify_user_id")
        notify_user_id = int(notify_user_id) if notify_user_id else None

        cur.execute("""
            INSERT INTO requests (
                vendor, invoice_number, invoice_date, amount, description,
                gl_account_id, requested_by_id, notify_user_id, status, current_step
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 1)
        """, (
            data["vendor"], data.get("invoice_number"),
            data.get("invoice_date"), float(data["amount"]), data.get("description", ""),
            int(data["gl_account_id"]), requested_by, notify_user_id
        ))
        req_id = cur.lastrowid
        db.commit()

        gl = get_gl(int(data["gl_account_id"]))
        if gl:
            cur.execute("""
                UPDATE requests SET
                    primary_approver_id = ?,
                    secondary_approver_id = ?,
                    tertiary_approver_id = ?
                WHERE id = ?
            """, (gl.get("primary_approver_id"), gl.get("secondary_approver_id"), gl.get("tertiary_approver_id"), req_id))
            db.commit()

        start_workflow(req_id)
        return jsonify(get_request(req_id)), 201

    # GET with optional filters
    where = []
    params = []

    status = request.args.get("status")
    if status and status != "All":
        where.append("r.status = ?")
        params.append(status)

    date_from = request.args.get("date_from")
    if date_from:
        where.append("r.invoice_date >= ?")
        params.append(date_from)

    date_to = request.args.get("date_to")
    if date_to:
        where.append("r.invoice_date <= ?")
        params.append(date_to)

    search = request.args.get("search", "").strip()
    if search:
        where.append("(r.vendor LIKE ? OR r.description LIKE ? OR CAST(r.id AS TEXT) LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    acct = request.args.get("account_number")
    if acct:
        where.append("g.account_number LIKE ?")
        params.append(f"%{acct}%")

    # Non-admins only see related requests
    if me and not is_admin(me):
        where.append("""(
            r.requested_by_id = ? OR r.notify_user_id = ?
            OR r.primary_approver_id = ? OR r.secondary_approver_id = ? OR r.tertiary_approver_id = ?
        )""")
        params.extend([me["id"], me["id"], me["id"], me["id"], me["id"]])

    sql = """
        SELECT r.*, 
               g.account_number, g.name as gl_name,
               u.first_name || ' ' || u.last_name as requester_name,
               u.email as requester_email
        FROM requests r
        JOIN gl_accounts g ON r.gl_account_id = g.id
        JOIN users u ON r.requested_by_id = u.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.created_at DESC"

    cur.execute(sql, params)
    results = []
    for row in cur.fetchall():
        d = dict_from_row(row)
        chain_info = []
        for step, key in enumerate(["primary_approver_id", "secondary_approver_id", "tertiary_approver_id"], 1):
            aid = row[key]
            if aid:
                au = get_user(aid)
                if au:
                    chain_info.append({
                        "step": step,
                        "name": f"{au['first_name']} {au['last_name']}",
                        "approved": step < d["current_step"] or (d["status"] == "Approved")
                    })
        d["approver_chain"] = chain_info
        cat, aname = split_gl_name(row["gl_name"] or "")
        d["gl_category"] = cat
        d["gl_account_name"] = aname or row["gl_name"]
        d["gl_display"] = f"{row['account_number']} - {row['gl_name']}"
        d["attachments"] = list_attachments(d["id"])
        d["can_edit"] = user_can_edit_request(me, d)
        d["can_approve"] = user_can_approve_request(me, d)
        d["can_delete"] = user_can_delete_request(me, d)
        results.append(d)
    return jsonify(results)

@app.route("/api/requests/<int:req_id>", methods=["GET", "PUT", "DELETE"])
def api_request_detail(req_id):
    db = get_db()
    cur = db.cursor()
    me = current_user()
    if request.method == "GET":
        req = get_request(req_id)
        if not req:
            return "", 404
        if not user_can_view_request(me, req):
            return jsonify({"error": "Not authorized to view this request"}), 403
        req["gl"] = get_gl(req["gl_account_id"])
        req["requester"] = get_user(req["requested_by_id"])
        req["notify_user"] = get_user(req.get("notify_user_id")) if req.get("notify_user_id") else None
        req["history"] = fetch_approval_history(req_id)
        req["attachments"] = list_attachments(req_id)
        req["can_edit"] = user_can_edit_request(me, req)
        req["can_approve"] = user_can_approve_request(me, req)
        req["can_delete"] = user_can_delete_request(me, req)
        return jsonify(req)

    if request.method == "DELETE":
        req = get_request(req_id)
        if not req:
            return jsonify({"error": "Request not found"}), 404
        if not user_can_delete_request(me, req):
            return jsonify({"error": "Not authorized to delete this request"}), 403
        for att in list_attachments(req_id):
            cur.execute("SELECT stored_filename FROM request_attachments WHERE id=?", (att["id"],))
            arow = cur.fetchone()
            if arow:
                path = os.path.join(UPLOAD_FOLDER, arow["stored_filename"])
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        cur.execute("DELETE FROM request_attachments WHERE request_id=?", (req_id,))
        cur.execute("DELETE FROM approval_history WHERE request_id=?", (req_id,))
        cur.execute("DELETE FROM pending_approvals WHERE request_id=?", (req_id,))
        cur.execute("DELETE FROM requests WHERE id=?", (req_id,))
        db.commit()
        return "", 204

    # PUT edit (only if pending + authorized)
    data = request.get_json() or {}
    req = get_request(req_id)
    if not req or req["status"] != "Pending":
        return jsonify({"error": "Can only edit pending requests"}), 400
    if not user_can_edit_request(me, req):
        return jsonify({"error": "Not authorized to edit this request"}), 403

    requested_by = int(data.get("requested_by_id") or req["requested_by_id"])
    if me and not is_admin(me):
        requested_by = me["id"]
    notify_user_id = data.get("notify_user_id")
    notify_user_id = int(notify_user_id) if notify_user_id else None

    cur.execute("""
        UPDATE requests SET
            vendor=?, invoice_number=?, invoice_date=?, amount=?, description=?,
            gl_account_id=?, requested_by_id=?, notify_user_id=?
        WHERE id=?
    """, (
        data["vendor"], data.get("invoice_number"), data.get("invoice_date"),
        float(data["amount"]), data.get("description", ""),
        int(data["gl_account_id"]), requested_by, notify_user_id, req_id
    ))
    db.commit()
    return jsonify(get_request(req_id))

@app.route("/api/requests/<int:req_id>/attachments", methods=["GET", "POST"])
def api_request_attachments(req_id):
    me = current_user()
    req = get_request(req_id)
    if not req:
        return jsonify({"error": "Request not found"}), 404
    if not user_can_view_request(me, req):
        return jsonify({"error": "Not authorized"}), 403

    if request.method == "GET":
        return jsonify(list_attachments(req_id))

    # POST upload — requester, approvers on chain, or admin
    if not (is_admin(me) or user_can_edit_request(me, req) or user_can_approve_request(me, req) or user_can_view_request(me, req)):
        return jsonify({"error": "Not authorized to attach files"}), 403
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": "File type not allowed"}), 400

    original = secure_filename(f.filename)
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else "bin"
    stored = f"{req_id}_{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_FOLDER, stored)
    f.save(path)
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({"error": f"File exceeds {MAX_UPLOAD_MB} MB limit"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO request_attachments
            (request_id, original_filename, stored_filename, content_type, size_bytes, uploaded_by_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (req_id, original, stored, f.mimetype, size, me["id"] if me else None))
    db.commit()
    return jsonify(list_attachments(req_id)[-1]), 201


@app.route("/api/attachments/<int:att_id>", methods=["GET", "DELETE"])
def api_attachment(att_id):
    me = current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM request_attachments WHERE id=?", (att_id,))
    att = cur.fetchone()
    if not att:
        return jsonify({"error": "Attachment not found"}), 404
    req = get_request(att["request_id"])
    if not user_can_view_request(me, req):
        return jsonify({"error": "Not authorized"}), 403

    if request.method == "DELETE":
        if not (is_admin(me) or user_can_edit_request(me, req)):
            return jsonify({"error": "Not authorized to delete attachment"}), 403
        path = os.path.join(UPLOAD_FOLDER, att["stored_filename"])
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        cur.execute("DELETE FROM request_attachments WHERE id=?", (att_id,))
        db.commit()
        return "", 204

    return send_attachment_response(att)


@app.route("/api/requests/<int:req_id>/manual_action", methods=["POST"])
def api_manual_action(req_id):
    """Allow UI to manually approve/reject when the current user is authorized."""
    me = current_user()
    data = request.get_json() or {}
    action = data.get("action")  # "approve" or "reject"
    notes = data.get("notes")
    req = get_request(req_id)
    if not req:
        return jsonify({"error": "Request not found"}), 404
    if not user_can_approve_request(me, req):
        return jsonify({"error": "Not authorized to approve/reject this request"}), 403

    approver_id = data.get("approver_id") or (me["id"] if me else None)
    if is_admin(me) and not data.get("approver_id"):
        # Admin acting as current-step approver when not on chain
        keys = {1: "primary_approver_id", 2: "secondary_approver_id", 3: "tertiary_approver_id"}
        step_key = keys.get(req.get("current_step") or 1)
        approver_id = req.get(step_key) or me["id"]

    if not approver_id:
        return jsonify({"error": "No approver specified and none found in chain"}), 400

    if action == "approve":
        ok, msg = advance_or_complete(req_id, approver_id, "approved", notes)
    else:
        ok, msg = advance_or_complete(req_id, approver_id, "rejected", notes or "Rejected via UI")
    return jsonify({"success": ok, "message": msg})

@app.route("/api/export")
def api_export():
    """CSV export of requests (respecting simple filters via query params)."""
    db = get_db()
    cur = db.cursor()
    me = current_user()

    where = []
    params = []
    if request.args.get("status") and request.args.get("status") != "All":
        where.append("r.status = ?")
        params.append(request.args.get("status"))
    if request.args.get("date_from"):
        where.append("r.invoice_date >= ?")
        params.append(request.args.get("date_from"))
    if request.args.get("date_to"):
        where.append("r.invoice_date <= ?")
        params.append(request.args.get("date_to"))
    if request.args.get("search"):
        s = f"%{request.args.get('search')}%"
        where.append("(r.vendor LIKE ? OR r.description LIKE ?)")
        params.extend([s, s])
    if request.args.get("account_number"):
        where.append("g.account_number LIKE ?")
        params.append(f"%{request.args.get('account_number')}%")

    if me and not is_admin(me):
        where.append("""(
            r.requested_by_id = ? OR r.notify_user_id = ?
            OR r.primary_approver_id = ? OR r.secondary_approver_id = ? OR r.tertiary_approver_id = ?
        )""")
        params.extend([me["id"], me["id"], me["id"], me["id"], me["id"]])

    sql = """
        SELECT r.id, r.created_at, r.invoice_date, r.vendor, r.invoice_number,
               r.amount, r.description, g.account_number, g.name as gl_name,
               u.first_name || ' ' || u.last_name as requester,
               r.status, r.current_step
        FROM requests r
        JOIN gl_accounts g ON r.gl_account_id = g.id
        JOIN users u ON r.requested_by_id = u.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.created_at DESC"

    cur.execute(sql, params)
    rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Created", "Invoice Date", "Vendor", "Invoice #", "Amount", "Description",
                     "GL Account", "GL Name", "Requested By", "Status", "Current Step"])
    for r in rows:
        writer.writerow(list(r))

    output.seek(0)
    filename = f"ap_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename
    )

@app.route("/api/email_log")
def api_email_log():
    denied = require_admin_api()
    if denied:
        return denied
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM email_log ORDER BY sent_at DESC LIMIT 100")
    return jsonify([dict_from_row(r) for r in cur.fetchall()])

@app.route("/api/stats")
def api_stats():
    """Return request counts for the header.

    IMPORTANT: Use the same JOINs as GET /api/requests so the badges match
    what appears in Status & Lookup. A bare COUNT on requests alone can
    include orphaned rows (missing GL/user) that never show in the table —
    which is why the UI could show "1 pending" with only an Approved row listed.
    """
    db = get_db()
    cur = db.cursor()
    me = current_user()

    # Same base join as the Status & Lookup list
    base_from = """
        FROM requests r
        JOIN gl_accounts g ON r.gl_account_id = g.id
        JOIN users u ON r.requested_by_id = u.id
    """
    where = []
    params = []

    if me and not is_admin(me):
        where.append("""(
            r.requested_by_id = ? OR r.notify_user_id = ?
            OR r.primary_approver_id = ? OR r.secondary_approver_id = ? OR r.tertiary_approver_id = ?
        )""")
        params.extend([me["id"], me["id"], me["id"], me["id"], me["id"]])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    cur.execute(
        f"SELECT TRIM(r.status) as status, COUNT(*) as cnt {base_from}{where_sql} GROUP BY TRIM(r.status)",
        params,
    )
    by_norm = {}
    for r in cur.fetchall():
        key = (r["status"] or "").strip()
        by_norm[key] = int(r["cnt"] or 0)

    cur.execute(f"SELECT COUNT(*) as total {base_from}{where_sql}", params)
    total = int(cur.fetchone()["total"] or 0)

    # Explicit pending count (status exactly Pending after trim)
    pending_where = where + ["TRIM(r.status) = 'Pending'"]
    pending_sql = " WHERE " + " AND ".join(pending_where)
    cur.execute(f"SELECT COUNT(*) as cnt {base_from}{pending_sql}", params)
    pending = int(cur.fetchone()["cnt"] or 0)

    return jsonify({
        "total": total,
        "pending": pending,
        "approved": int(by_norm.get("Approved") or 0),
        "rejected": int(by_norm.get("Rejected") or 0),
        "by_status": by_norm,
    })

# ---------- CONTRIBUTIONS (administrator only) ----------
def ensure_letter_template():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM contribution_letter_template WHERE id=1")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO contribution_letter_template (id, body) VALUES (1, ?)",
            (DEFAULT_LETTER_TEMPLATE,),
        )
        db.commit()


def contributor_full_name(row):
    return f"{(row.get('first_name') or '').strip()} {(row.get('last_name') or '').strip()}".strip()


def contributor_address_block(row):
    lines = []
    if (row.get("address_line1") or "").strip():
        lines.append(row["address_line1"].strip())
    if (row.get("address_line2") or "").strip():
        lines.append(row["address_line2"].strip())
    city = (row.get("city") or "").strip()
    state = (row.get("state") or "").strip()
    zipc = (row.get("zip") or "").strip()
    parts = []
    if city and state:
        parts.append(f"{city}, {state}")
    elif city or state:
        parts.append(city or state)
    if zipc:
        parts.append(zipc)
    city_line = " ".join(parts)
    if city_line:
        lines.append(city_line)
    return "\n".join(lines) if lines else ""


def get_contributor(cid):
    if not cid:
        return None
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM contributors WHERE id=?", (cid,))
    row = cur.fetchone()
    return dict_from_row(row) if row else None


def serialize_contributor(row):
    d = dict_from_row(row) if not isinstance(row, dict) else dict(row)
    d["name"] = contributor_full_name(d)
    d["address_block"] = contributor_address_block(d)
    return d


def find_contributor_by_name(name):
    raw = (name or "").strip()
    if not raw:
        return None
    db = get_db()
    cur = db.cursor()
    if "," in raw:
        last, first = [p.strip() for p in raw.split(",", 1)]
    else:
        bits = raw.split()
        if len(bits) >= 2:
            first, last = bits[0], " ".join(bits[1:])
        else:
            first, last = raw, ""
    cur.execute(
        """
        SELECT * FROM contributors
        WHERE lower(trim(first_name)) = lower(?) AND lower(trim(last_name)) = lower(?)
        """,
        (first, last),
    )
    row = cur.fetchone()
    if row:
        return dict_from_row(row)
    cur.execute(
        """
        SELECT * FROM contributors
        WHERE lower(trim(first_name) || ' ' || trim(last_name)) = lower(?)
        """,
        (raw,),
    )
    row = cur.fetchone()
    return dict_from_row(row) if row else None


def get_letter_template_body():
    ensure_letter_template()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT body, updated_at, updated_by_id FROM contribution_letter_template WHERE id=1")
    row = cur.fetchone()
    if not row:
        return DEFAULT_LETTER_TEMPLATE, None, None
    return row["body"], row["updated_at"], row["updated_by_id"]


def format_money_amount(amount):
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def parse_contributor_id(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in ("", "all", "any", "0", "none"):
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def contribution_entry_filters():
    where = []
    params = []
    date_from = (request.args.get("date_from") or request.form.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or request.form.get("date_to") or "").strip()
    memo = (request.args.get("memo") or request.form.get("memo") or "").strip()
    contributor_id = parse_contributor_id(
        request.args.get("contributor_id") or request.form.get("contributor_id")
    )
    method = (request.args.get("method") or "").strip()
    if memo:
        where.append("IFNULL(e.memo,'') LIKE ?")
        params.append(f"%{memo}%")
    if contributor_id:
        where.append("e.contributor_id = ?")
        params.append(contributor_id)
    if method and method in CONTRIBUTION_METHODS:
        where.append("e.method = ?")
        params.append(method)
    return where, params, date_from, date_to


def entry_in_date_range(entry, date_from, date_to):
    iso = normalize_contribution_date(entry.get("contribution_date")) or ""
    start = normalize_contribution_date(date_from) if date_from else ""
    end = normalize_contribution_date(date_to) if date_to else ""
    if start and (not iso or iso < start):
        return False
    if end and (not iso or iso > end):
        return False
    return True


def list_contribution_entries_filtered(where, params, date_from=None, date_to=None):
    db = get_db()
    cur = db.cursor()
    sql = """
        SELECT e.*, c.first_name, c.last_name, c.address_line1, c.address_line2,
               c.city, c.state, c.zip
        FROM contribution_entries e
        LEFT JOIN contributors c ON c.id = e.contributor_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.contribution_date, c.last_name, c.first_name, e.id"
    cur.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = dict_from_row(r)
        d["contributor_name"] = contributor_full_name(d)
        d["amount"] = float(d.get("amount") or 0)
        iso = normalize_contribution_date(d.get("contribution_date"))
        if iso:
            d["contribution_date"] = iso
        d["contribution_date_display"] = format_display_date(d.get("contribution_date"))
        if not entry_in_date_range(d, date_from, date_to):
            continue
        rows.append(d)
    rows.sort(key=lambda e: (e.get("contribution_date") or "", e.get("last_name") or "", e.get("id") or 0))
    return rows


def build_gift_detail(entries, detail_mode):
    total = sum(e["amount"] for e in entries)
    if detail_mode == "transactions":
        lines = ["Date            Method     Check #      Memo                         Amount"]
        lines.append("-" * 76)
        for e in entries:
            memo = (e.get("memo") or "")[:24]
            chk = (e.get("check_number") or "")[:10]
            lines.append(
                f"{e.get('contribution_date_display') or format_display_date(e.get('contribution_date')) or '':<16}{(e.get('method') or ''):<11}{chk:<13}{memo:<28}{format_money_amount(e['amount']):>8}"
            )
        lines.append("-" * 76)
        lines.append(f"{'Total':<68}{format_money_amount(total):>8}")
        return "\n".join(lines), total
    return f"The total of your contributions for this period is {format_money_amount(total)}.", total


def build_gift_detail_html(entries, detail_mode):
    total = sum(e["amount"] for e in entries)
    if detail_mode != "transactions":
        return (
            f"<p>The total of your contributions for this period is "
            f"<strong>{html_escape(format_money_amount(total))}</strong>.</p>"
        ), total
    rows = []
    for e in entries:
        rows.append(
            "<tr>"
            f"<td>{html_escape(e.get('contribution_date_display') or format_display_date(e.get('contribution_date')) or '')}</td>"
            f"<td>{html_escape(e.get('method') or '')}</td>"
            f"<td>{html_escape(e.get('check_number') or '—')}</td>"
            f"<td>{html_escape(e.get('memo') or '—')}</td>"
            f"<td class='amt'>{html_escape(format_money_amount(e['amount']))}</td>"
            "</tr>"
        )
    table = (
        "<table class='gifts'><thead><tr>"
        "<th>Date</th><th>Method</th><th>Check #</th><th>Memo</th><th>Amount</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody><tfoot><tr><td colspan='4'>Total</td>"
        f"<td class='amt'>{html_escape(format_money_amount(total))}</td></tr></tfoot></table>"
    )
    return table, total


def apply_letter_template(body, context, gift_html=None):
    token = "___GIFT_DETAIL_HTML___"
    text = body or ""
    replacements = {}
    for key, val in context.items():
        if key == "gift_detail":
            continue
        replacements["{{" + key + "}}"] = str(val or "")
    if gift_html is not None:
        text = text.replace("{{gift_detail}}", token)
    else:
        replacements["{{gift_detail}}"] = context.get("gift_detail") or ""
    for needle, val in replacements.items():
        text = text.replace(needle, val)
    if gift_html is None:
        return text
    escaped = html_escape(text).replace("\n", "<br>\n")
    return escaped.replace(token, gift_html)


def parse_money(value):
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def amount_matches(value, mode, min_amt, max_amt):
    mode = (mode or "any").strip().lower()
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if mode in ("", "any"):
        return True
    if mode == "gt":
        return min_amt is not None and amount > min_amt
    if mode == "lt":
        return min_amt is not None and amount < min_amt
    if mode == "between":
        if min_amt is None and max_amt is None:
            return True
        if min_amt is not None and amount < min_amt:
            return False
        if max_amt is not None and amount > max_amt:
            return False
        return True
    return True


def letter_request_filters():
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    memo = (request.args.get("memo") or "").strip()
    contributor_id = parse_contributor_id(request.args.get("contributor_id"))
    detail = (request.args.get("detail") or "total").strip()
    if detail not in ("total", "transactions"):
        detail = "total"
    amount_mode = (request.args.get("amount_mode") or "any").strip().lower()
    if amount_mode not in ("any", "gt", "lt", "between"):
        amount_mode = "any"
    amount_min = parse_money(request.args.get("amount_min") or request.args.get("amount"))
    amount_max = parse_money(request.args.get("amount_max"))
    amount_apply = (request.args.get("amount_apply") or "total").strip().lower()
    if amount_apply not in ("total", "gift"):
        amount_apply = "total"
    return {
        "date_from": date_from,
        "date_to": date_to,
        "memo": memo,
        "contributor_id": contributor_id,
        "detail": detail,
        "amount_mode": amount_mode,
        "amount_min": amount_min,
        "amount_max": amount_max,
        "amount_apply": amount_apply,
    }


def letters_for_filters(
    date_from,
    date_to,
    memo,
    contributor_id,
    detail_mode,
    amount_mode="any",
    amount_min=None,
    amount_max=None,
    amount_apply="total",
):
    where, params = [], []
    if memo:
        where.append("IFNULL(e.memo,'') LIKE ?")
        params.append(f"%{memo}%")
    cid = parse_contributor_id(contributor_id)
    if cid:
        where.append("e.contributor_id = ?")
        params.append(cid)
    entries = list_contribution_entries_filtered(where, params, date_from, date_to)
    if amount_apply == "gift":
        entries = [e for e in entries if amount_matches(e.get("amount"), amount_mode, amount_min, amount_max)]
    grouped = {}
    for e in entries:
        grouped.setdefault(e["contributor_id"], []).append(e)
    body, _, _ = get_letter_template_body()
    letter_date = format_display_dt(datetime.now().strftime("%Y-%m-%d"))
    period_phrase = ""
    if date_from and date_to:
        period_phrase = f" from {format_display_dt(date_from)} through {format_display_dt(date_to)}"
    elif date_from:
        period_phrase = f" beginning {format_display_dt(date_from)}"
    elif date_to:
        period_phrase = f" through {format_display_dt(date_to)}"
    letters = []
    for cid, gifts in grouped.items():
        person = get_contributor(cid) or gifts[0]
        gift_text, total = build_gift_detail(gifts, detail_mode)
        if amount_apply != "gift" and not amount_matches(total, amount_mode, amount_min, amount_max):
            continue
        gift_html, _ = build_gift_detail_html(gifts, detail_mode)
        ctx = {
            "contributor_name": contributor_full_name(person),
            "address_block": contributor_address_block(person),
            "letter_date": letter_date,
            "date_from": format_display_dt(date_from) if date_from else "",
            "date_to": format_display_dt(date_to) if date_to else "",
            "period_phrase": period_phrase,
            "total_amount": format_money_amount(total),
            "church_name": "Johnson Church of Christ",
            "gift_detail": gift_text,
        }
        letters.append({
            "contributor_id": cid,
            "contributor_name": ctx["contributor_name"],
            "total": total,
            "entry_count": len(gifts),
            "body_text": apply_letter_template(body, ctx),
            "body_html": apply_letter_template(body, ctx, gift_html=gift_html),
        })
    letters.sort(key=lambda x: x["contributor_name"].lower())
    return letters


def summary_report_data(date_from, date_to, memo):
    where, params = [], []
    if memo:
        where.append("IFNULL(e.memo,'') LIKE ?")
        params.append(f"%{memo}%")
    entries = list_contribution_entries_filtered(where, params, date_from, date_to)
    by_date = {}
    for e in entries:
        d = e.get("contribution_date") or ""
        bucket = by_date.setdefault(d, {m: 0.0 for m in CONTRIBUTION_METHODS})
        method = e.get("method") if e.get("method") in CONTRIBUTION_METHODS else "Other"
        bucket[method] = bucket.get(method, 0.0) + e["amount"]
    rows = []
    totals = {m: 0.0 for m in CONTRIBUTION_METHODS}
    for d in sorted(by_date.keys()):
        rec = {"date": d, "date_display": format_display_date(d)}
        day_total = 0.0
        for m in CONTRIBUTION_METHODS:
            rec[m] = by_date[d].get(m, 0.0)
            totals[m] += rec[m]
            day_total += rec[m]
        rec["Total"] = day_total
        rows.append(rec)
    n = len(rows) or 1
    averages = {m: (totals[m] / n if rows else 0.0) for m in CONTRIBUTION_METHODS}
    averages["Total"] = sum(totals.values()) / n if rows else 0.0
    grand = dict(totals)
    grand["Total"] = sum(totals.values())
    return {
        "methods": list(CONTRIBUTION_METHODS),
        "rows": rows,
        "averages": averages,
        "grand_totals": grand,
        "date_count": len(rows),
        "entry_count": len(entries),
        "date_from": date_from,
        "date_to": date_to,
        "memo": memo,
    }


@app.route("/api/contributors", methods=["GET", "POST"])
def api_contributors():
    denied = require_admin_api()
    if denied:
        return denied
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        q = (request.args.get("search") or "").strip()
        sql = "SELECT * FROM contributors"
        params = []
        if q:
            sql += """ WHERE first_name LIKE ? OR last_name LIKE ? OR address_line1 LIKE ?
                       OR city LIKE ? OR zip LIKE ?"""
            like = f"%{q}%"
            params = [like, like, like, like, like]
        sql += " ORDER BY last_name, first_name"
        cur.execute(sql, params)
        return jsonify([serialize_contributor(r) for r in cur.fetchall()])

    data = request.get_json() or {}
    first = (data.get("first_name") or "").strip()
    last = (data.get("last_name") or "").strip()
    if not first or not last:
        return jsonify({"error": "First name and last name are required"}), 400
    cur.execute(
        """
        INSERT INTO contributors (first_name, last_name, address_line1, address_line2, city, state, zip)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            first, last,
            (data.get("address_line1") or "").strip(),
            (data.get("address_line2") or "").strip(),
            (data.get("city") or "").strip(),
            (data.get("state") or "").strip(),
            (data.get("zip") or "").strip(),
        ),
    )
    db.commit()
    return jsonify(serialize_contributor(get_contributor(cur.lastrowid))), 201


@app.route("/api/contributors/bulk_delete", methods=["POST"])
def api_contributors_bulk_delete():
    denied = require_admin_api()
    if denied:
        return denied
    data = request.get_json() or {}
    ids = []
    for value in data.get("ids") or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    ids = list(dict.fromkeys(i for i in ids if i > 0))
    if not ids:
        return jsonify({"error": "No contributors selected"}), 400
    db = get_db()
    cur = db.cursor()
    deleted = 0
    skipped = []
    for cid in ids:
        person = get_contributor(cid)
        if not person:
            continue
        cur.execute("SELECT COUNT(*) AS c FROM contribution_entries WHERE contributor_id=?", (cid,))
        if cur.fetchone()["c"]:
            skipped.append(contributor_full_name(person) or f"#{cid}")
            continue
        cur.execute("DELETE FROM contributors WHERE id=?", (cid,))
        deleted += 1
    db.commit()
    msg = f"Deleted {deleted} contributor(s)."
    if skipped:
        msg += " Could not delete (has contribution entries): " + ", ".join(skipped[:12])
        if len(skipped) > 12:
            msg += ", …"
    return jsonify({"deleted": deleted, "skipped": skipped, "message": msg})


@app.route("/api/contributors/<int:cid>", methods=["GET", "PUT", "DELETE"])
def api_contributor(cid):
    denied = require_admin_api()
    if denied:
        return denied
    db = get_db()
    cur = db.cursor()
    person = get_contributor(cid)
    if not person:
        return jsonify({"error": "Contributor not found"}), 404
    if request.method == "GET":
        return jsonify(serialize_contributor(person))
    if request.method == "DELETE":
        cur.execute("SELECT COUNT(*) AS c FROM contribution_entries WHERE contributor_id=?", (cid,))
        if cur.fetchone()["c"]:
            return jsonify({"error": "Cannot delete a contributor who has contribution entries"}), 400
        cur.execute("DELETE FROM contributors WHERE id=?", (cid,))
        db.commit()
        return "", 204
    data = request.get_json() or {}
    first = (data.get("first_name") or person["first_name"]).strip()
    last = (data.get("last_name") or person["last_name"]).strip()
    if not first or not last:
        return jsonify({"error": "First name and last name are required"}), 400
    cur.execute(
        """
        UPDATE contributors SET first_name=?, last_name=?, address_line1=?, address_line2=?,
            city=?, state=?, zip=?
        WHERE id=?
        """,
        (
            first, last,
            (data.get("address_line1") if "address_line1" in data else person.get("address_line1") or "").strip(),
            (data.get("address_line2") if "address_line2" in data else person.get("address_line2") or "").strip(),
            (data.get("city") if "city" in data else person.get("city") or "").strip(),
            (data.get("state") if "state" in data else person.get("state") or "").strip(),
            (data.get("zip") if "zip" in data else person.get("zip") or "").strip(),
            cid,
        ),
    )
    db.commit()
    return jsonify(serialize_contributor(get_contributor(cid)))


@app.route("/api/contribution_memos")
def api_contribution_memos():
    denied = require_admin_api()
    if denied:
        return denied
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT DISTINCT TRIM(memo) AS memo
        FROM contribution_entries
        WHERE TRIM(IFNULL(memo, '')) != ''
        ORDER BY memo COLLATE NOCASE
        """
    )
    names = []
    seen = set()
    for row in cur.fetchall():
        memo = (row["memo"] or "").strip()
        key = memo.lower()
        if memo and key not in seen:
            seen.add(key)
            names.append(memo)
    default = DEFAULT_CONTRIBUTION_MEMO
    names = [default] + [n for n in names if n.lower() != default.lower()]
    return jsonify(names)


@app.route("/api/contribution_entries", methods=["GET", "POST"])
def api_contribution_entries():
    denied = require_admin_api()
    if denied:
        return denied
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        where, params, date_from, date_to = contribution_entry_filters()
        return jsonify(list_contribution_entries_filtered(where, params, date_from, date_to))

    data = request.get_json() or {}
    try:
        cid = int(data.get("contributor_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Contributor is required"}), 400
    if not get_contributor(cid):
        return jsonify({"error": "Contributor not found"}), 404
    cdate = normalize_contribution_date(data.get("contribution_date"))
    method = (data.get("method") or "").strip()
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid amount is required"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero"}), 400
    if not cdate:
        return jsonify({"error": "Date is required"}), 400
    if method not in CONTRIBUTION_METHODS:
        return jsonify({"error": "Method must be Check, Cash, Breeze, or Other"}), 400
    me = current_user()
    cur.execute(
        """
        INSERT INTO contribution_entries
            (contributor_id, contribution_date, amount, method, check_number, memo, created_by_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid, cdate, amount, method,
            (data.get("check_number") or "").strip(),
            (data.get("memo") or "").strip() or DEFAULT_CONTRIBUTION_MEMO,
            me["id"] if me else None,
        ),
    )
    db.commit()
    where, params = ["e.id = ?"], [cur.lastrowid]
    return jsonify(list_contribution_entries_filtered(where, params)[0]), 201


@app.route("/api/contribution_entries/bulk_delete", methods=["POST"])
def api_contribution_entries_bulk_delete():
    denied = require_admin_api()
    if denied:
        return denied
    data = request.get_json() or {}
    raw_ids = data.get("ids") or []
    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    ids = list(dict.fromkeys(i for i in ids if i > 0))
    if not ids:
        return jsonify({"error": "No contribution entries selected"}), 400
    db = get_db()
    cur = db.cursor()
    placeholders = ",".join("?" * len(ids))
    cur.execute(f"DELETE FROM contribution_entries WHERE id IN ({placeholders})", ids)
    deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(ids)
    db.commit()
    return jsonify({"deleted": deleted, "message": f"Deleted {deleted} contribution(s)."})


@app.route("/api/contribution_entries/<int:eid>", methods=["GET", "PUT", "DELETE"])
def api_contribution_entry(eid):
    denied = require_admin_api()
    if denied:
        return denied
    db = get_db()
    cur = db.cursor()
    rows = list_contribution_entries_filtered(["e.id = ?"], [eid])
    if not rows:
        return jsonify({"error": "Entry not found"}), 404
    if request.method == "GET":
        return jsonify(rows[0])
    if request.method == "DELETE":
        cur.execute("DELETE FROM contribution_entries WHERE id=?", (eid,))
        db.commit()
        return "", 204
    data = request.get_json() or {}
    existing = rows[0]
    try:
        cid = int(data.get("contributor_id") or existing["contributor_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "Contributor is required"}), 400
    if not get_contributor(cid):
        return jsonify({"error": "Contributor not found"}), 404
    cdate = normalize_contribution_date(
        data.get("contribution_date") if "contribution_date" in data else existing["contribution_date"]
    )
    method = (data.get("method") or existing["method"]).strip()
    try:
        amount = float(data["amount"]) if "amount" in data else float(existing["amount"])
    except (TypeError, ValueError):
        return jsonify({"error": "A valid amount is required"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero"}), 400
    if method not in CONTRIBUTION_METHODS:
        return jsonify({"error": "Method must be Check, Cash, Breeze, or Other"}), 400
    cur.execute(
        """
        UPDATE contribution_entries SET contributor_id=?, contribution_date=?, amount=?,
            method=?, check_number=?, memo=?
        WHERE id=?
        """,
        (
            cid, cdate, amount, method,
            (data.get("check_number") if "check_number" in data else existing.get("check_number") or "").strip(),
            ((data.get("memo") if "memo" in data else existing.get("memo") or "").strip() or DEFAULT_CONTRIBUTION_MEMO),
            eid,
        ),
    )
    db.commit()
    return jsonify(list_contribution_entries_filtered(["e.id = ?"], [eid])[0])


@app.route("/api/contribution_entries/export")
def api_contribution_entries_export():
    denied = require_admin_api()
    if denied:
        return denied
    where, params, date_from, date_to = contribution_entry_filters()
    rows = list_contribution_entries_filtered(where, params, date_from, date_to)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Contributor", "Check Number", "Method", "Memo", "Amount"])
    for r in rows:
        writer.writerow([
            r.get("contribution_date") or "",
            r.get("contributor_name") or "",
            r.get("check_number") or "",
            r.get("method") or "",
            r.get("memo") or "",
            f"{r.get('amount') or 0:.2f}",
        ])
    output.seek(0)
    filename = f"contribution_entries_{datetime.now().strftime('%Y%m%d')}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/contribution_entries/import", methods=["POST"])
def api_contribution_entries_import():
    denied = require_admin_api()
    if denied:
        return denied
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "No file selected"}), 400
    name = f.filename.lower()
    rows = []
    try:
        if name.endswith(".xlsx"):
            if not openpyxl:
                return jsonify({"error": "Excel import requires openpyxl"}), 400
            wb = openpyxl.load_workbook(f, data_only=True)
            ws = wb.active
            rows = [[c if c is not None else "" for c in row] for row in ws.iter_rows(values_only=True)]
        else:
            text = f.read().decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400
    if not rows:
        return jsonify({"error": "File is empty"}), 400

    def norm(h):
        return re.sub(r"[^a-z0-9]+", "", (h or "").strip().lower())

    header_idx = 0
    headers = [norm(x) for x in rows[0]]
    colmap = {}
    aliases = {
        "date": ("date", "contributiondate"),
        "contributor": ("contributor", "name", "contributorname"),
        "check": ("checknumber", "check", "checkno"),
        "method": ("method", "methodofpayment", "paymentmethod"),
        "memo": ("memo", "memodescription", "description"),
        "amount": ("amount", "amt"),
    }
    for key, names in aliases.items():
        for i, h in enumerate(headers):
            if h in names:
                colmap[key] = i
                break
    if "date" not in colmap or "contributor" not in colmap or "amount" not in colmap:
        return jsonify({"error": "File must include Date, Contributor, and Amount columns"}), 400

    db = get_db()
    cur = db.cursor()
    me = current_user()
    added = 0
    skipped = []
    created_people = 0
    for n, row in enumerate(rows[header_idx + 1 :], start=2):
        if not row or not any(str(c).strip() for c in row if c is not None):
            continue
        def cell(key):
            i = colmap.get(key)
            if i is None or i >= len(row):
                return ""
            v = row[i]
            if hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d")
            return str(v).strip() if v is not None else ""

        cname = cell("contributor")
        raw_date = row[colmap["date"]] if colmap.get("date") is not None and colmap["date"] < len(row) else cell("date")
        cdate = normalize_contribution_date(raw_date) or normalize_contribution_date(cell("date"))
        method = cell("method") or "Check"
        if method not in CONTRIBUTION_METHODS:
            method_l = method.lower()
            match = next((m for m in CONTRIBUTION_METHODS if m.lower() == method_l), None)
            method = match or "Other"
        try:
            amount = float(str(cell("amount")).replace("$", "").replace(",", ""))
        except ValueError:
            skipped.append(f"Row {n}: invalid amount")
            continue
        if not cname or not cdate or amount <= 0:
            skipped.append(f"Row {n}: missing name, date, or amount")
            continue
        person = find_contributor_by_name(cname)
        if not person:
            if "," in cname:
                last, first = [p.strip() for p in cname.split(",", 1)]
            else:
                bits = cname.split()
                first, last = (bits[0], " ".join(bits[1:])) if len(bits) >= 2 else (cname, "")
            if not last:
                skipped.append(f"Row {n}: could not parse contributor '{cname}'")
                continue
            cur.execute(
                "INSERT INTO contributors (first_name, last_name) VALUES (?, ?)",
                (first, last),
            )
            person = {"id": cur.lastrowid}
            created_people += 1
        cur.execute(
            """
            INSERT INTO contribution_entries
                (contributor_id, contribution_date, amount, method, check_number, memo, created_by_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person["id"], cdate, amount, method,
                cell("check"), cell("memo"),
                me["id"] if me else None,
            ),
        )
        added += 1
    db.commit()
    msg = f"Imported {added} contribution(s)."
    if created_people:
        msg += f" Created {created_people} new contributor(s)."
    return jsonify({"message": msg, "added": added, "created_contributors": created_people, "errors": skipped[:20]})


@app.route("/api/contribution_template", methods=["GET", "PUT"])
def api_contribution_template():
    denied = require_admin_api()
    if denied:
        return denied
    if request.method == "GET":
        body, updated_at, updated_by = get_letter_template_body()
        return jsonify({
            "body": body,
            "updated_at": updated_at,
            "placeholders": [
                "{{contributor_name}}", "{{address_block}}", "{{letter_date}}",
                "{{date_from}}", "{{date_to}}", "{{period_phrase}}",
                "{{gift_detail}}", "{{total_amount}}", "{{church_name}}",
            ],
        })
    data = request.get_json() or {}
    body = data.get("body")
    if body is None or not str(body).strip():
        return jsonify({"error": "Template body is required"}), 400
    me = current_user()
    db = get_db()
    cur = db.cursor()
    ensure_letter_template()
    cur.execute(
        "UPDATE contribution_letter_template SET body=?, updated_at=datetime('now'), updated_by_id=? WHERE id=1",
        (body, me["id"] if me else None),
    )
    db.commit()
    return jsonify({"success": True, "body": body})


@app.route("/api/contributions/reports/summary")
def api_contribution_summary():
    denied = require_admin_api()
    if denied:
        return denied
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    memo = (request.args.get("memo") or "").strip()
    return jsonify(summary_report_data(date_from, date_to, memo))


@app.route("/api/contributions/letters")
def api_contribution_letters():
    denied = require_admin_api()
    if denied:
        return denied
    f = letter_request_filters()
    letters = letters_for_filters(
        f["date_from"], f["date_to"], f["memo"], f["contributor_id"], f["detail"],
        f["amount_mode"], f["amount_min"], f["amount_max"], f["amount_apply"],
    )
    return jsonify({
        "count": len(letters),
        "letters": letters,
        "date_from": f["date_from"],
        "date_to": f["date_to"],
        "contributor_id": f["contributor_id"],
    })


@app.route("/contributions/summary/print")
def print_contribution_summary():
    if not is_admin():
        abort(403)
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    memo = (request.args.get("memo") or "").strip()
    data = summary_report_data(date_from, date_to, memo)
    return render_template(
        "print_contribution_summary.html",
        data=data,
        money=format_money_amount,
        printed_at=format_display_dt(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        printer_name=user_full_name(current_user()) if current_user() else "",
    )


@app.route("/contributions/letters/print")
def print_contribution_letters():
    if not is_admin():
        abort(403)
    f = letter_request_filters()
    date_from, date_to = f["date_from"], f["date_to"]
    letters = letters_for_filters(
        f["date_from"], f["date_to"], f["memo"], f["contributor_id"], f["detail"],
        f["amount_mode"], f["amount_min"], f["amount_max"], f["amount_apply"],
    )
    download = request.args.get("download") in ("1", "true", "yes")
    html = render_template(
        "print_contribution_letters.html",
        letters=letters,
        autoprint=not download,
        date_from=format_display_dt(date_from) if date_from else "",
        date_to=format_display_dt(date_to) if date_to else "",
    )
    if download:
        filename = f"contribution_letters_{datetime.now().strftime('%Y%m%d')}.html"
        buf = io.BytesIO(html.encode("utf-8"))
        return send_file(buf, mimetype="text/html", as_attachment=True, download_name=filename)
    return html


# ---------- INIT ----------
def migrate_contribution_dates():
    """Rewrite stored contribution dates to YYYY-MM-DD so range filters match."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT id, contribution_date FROM contribution_entries")
    except sqlite3.OperationalError:
        return
    changed = 0
    for row in cur.fetchall():
        raw = row["contribution_date"]
        iso = normalize_contribution_date(raw)
        if iso and iso != (raw or ""):
            cur.execute(
                "UPDATE contribution_entries SET contribution_date=? WHERE id=?",
                (iso, row["id"]),
            )
            changed += 1
    if changed:
        db.commit()


def bootstrap_db():
    """Create schema, seed demo data, ensure passwords and roles exist."""
    init_db()
    seed_data()
    ensure_user_passwords()
    ensure_user_roles()
    ensure_letter_template()
    migrate_contribution_dates()
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)


app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Run on import so gunicorn / waitress also initialize the database
with app.app_context():
    bootstrap_db()


if __name__ == "__main__":
    print(f"\nJohnson Church of Christ Accounts Payable System")
    print(f"Running at http://127.0.0.1:{PORT}")
    print("Login required — default password: jccpass")
    print("Administrator: Darron.Mitchell")
    print("Open the URL above in your browser (desktop or mobile / iOS Safari).\n")
    app.run(host="0.0.0.0", port=PORT, debug=True)
