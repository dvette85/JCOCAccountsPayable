#!/usr/bin/env python3
"""
Johnson Church of Christ - Accounts Payable System
Flask + SQLite + Tailwind (CDN) responsive single-page app.
Desktop + Mobile friendly.
"""

import os
import sqlite3
import uuid
from datetime import datetime, date
from flask import Flask, request, jsonify, render_template, redirect, url_for, send_file, g
import io
import csv
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

# Load .env file if present (python-dotenv is in requirements)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- CONFIG ----------
DB_PATH = os.path.join(os.path.dirname(__file__), "ap.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "jcc-ap-dev-secret-change-in-prod")
PORT = int(os.environ.get("PORT", 5000))

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

# Trust proxy headers (important for Render, Heroku, etc. so request.host_url and scheme are correct)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Add password_hash column for existing databases (safe if column already exists)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

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
            FOREIGN KEY(requested_by_id) REFERENCES users(id)
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

    db.commit()

def seed_data():
    db = get_db()
    cur = db.cursor()

    # Seed sample users if none
    cur.execute("SELECT COUNT(*) as c FROM users")
    if cur.fetchone()["c"] == 0:
        default_pw = generate_password_hash("jccpass")
        sample_users = [
            ("jtreasurer", "Jane", "Treasurer", "jane.treasurer@johnsoncoc.org", default_pw),
            ("asmith", "Alex", "Smith", "alex.smith@johnsoncoc.org", default_pw),
            ("bwilson", "Beth", "Wilson", "beth.wilson@johnsoncoc.org", default_pw),
            ("rjohnson", "Robert", "Johnson", "robert.johnson@johnsoncoc.org", default_pw),
            ("mmartinez", "Maria", "Martinez", "maria.martinez@johnsoncoc.org", default_pw),
        ]
        cur.executemany(
            "INSERT INTO users (username, first_name, last_name, email, password_hash) VALUES (?,?,?,?,?)",
            sample_users
        )
        db.commit()
        print("Seeded sample users (default password: jccpass).")

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
    if BASE_URL:
        base_url = BASE_URL.rstrip("/")
    else:
        base_url = request.host_url.rstrip("/")
    token = create_or_get_token(request_row["id"], request_row["current_step"], approver["id"])

    approve_url = f"{base_url}/approve/{token}"
    reject_url = f"{base_url}/reject/{token}"

    subject = f"AP Approval Needed: Request #{request_row['id']} - {request_row['vendor']} (${request_row['amount']:.2f})"

    text = f"""Hello {approver['first_name']},

A new Accounts Payable request requires your approval.

REQUEST DETAILS
---------------
Request ID: {request_row['id']}
Vendor / Payee: {request_row['vendor']}
Invoice #: {request_row['invoice_number'] or 'N/A'}
Invoice Date: {request_row['invoice_date']}
Amount: ${request_row['amount']:.2f}
Description: {request_row['description'] or ''}

General Ledger Coding:
  {gl_row['account_number']} - {gl_row['name']}

Requested By: {requester['first_name']} {requester['last_name']} ({requester['email']})

Approval Step: {step_label}
{"Next approver after you: " + next_approver_name if next_approver_name else "This is the final approver."}

Please click one of the links below:

APPROVE: {approve_url}
REJECT:  {reject_url}

Thank you,
Johnson Church of Christ - Accounts Payable System
"""

    html = f"""<!doctype html>
<html><body style="font-family: system-ui, sans-serif; line-height:1.5; color:#222;">
  <h2 style="color:#1e40af;">Johnson Church of Christ</h2>
  <h3>Accounts Payable Request for Approval</h3>

  <p>Hello {approver['first_name']},</p>

  <table style="border-collapse:collapse; width:100%; max-width:560px; margin:16px 0;" border="1" cellpadding="8">
    <tr><td><strong>Request ID</strong></td><td>#{request_row['id']}</td></tr>
    <tr><td><strong>Vendor / Payee</strong></td><td>{request_row['vendor']}</td></tr>
    <tr><td><strong>Invoice #</strong></td><td>{request_row['invoice_number'] or 'N/A'}</td></tr>
    <tr><td><strong>Invoice Date</strong></td><td>{request_row['invoice_date']}</td></tr>
    <tr><td><strong>Amount</strong></td><td><strong>${request_row['amount']:.2f}</strong></td></tr>
    <tr><td><strong>Description</strong></td><td>{request_row['description'] or ''}</td></tr>
    <tr><td><strong>GL Account</strong></td><td>{gl_row['account_number']} — {gl_row['name']}</td></tr>
    <tr><td><strong>Requested By</strong></td><td>{requester['first_name']} {requester['last_name']} &lt;{requester['email']}&gt;</td></tr>
    <tr><td><strong>Current Step</strong></td><td>{step_label}</td></tr>
  </table>

  <p style="margin:20px 0;">
    <a href="{approve_url}" style="background:#16a34a;color:white;padding:12px 20px;text-decoration:none;border-radius:6px;font-weight:600;margin-right:12px;">✓ APPROVE</a>
    <a href="{reject_url}" style="background:#dc2626;color:white;padding:12px 20px;text-decoration:none;border-radius:6px;font-weight:600;">✕ REJECT</a>
  </p>

  <p style="color:#555;font-size:0.9em;">If approved, this request will be routed to the next approver{(' (' + next_approver_name + ')') if next_approver_name else ''}.</p>
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
        return u
    return None

def get_gl(gl_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM gl_accounts WHERE id=?", (gl_id,))
    row = cur.fetchone()
    return dict_from_row(row) if row else None

def get_request(req_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM requests WHERE id=?", (req_id,))
    row = cur.fetchone()
    return dict_from_row(row) if row else None

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

        # Notify requester of completion (optional)
        if requester:
            subject = f"AP Request #{request_id} FULLY APPROVED - {req['vendor']}"
            body = f"""Hello {requester['first_name']},

Good news — your AP request has received all required approvals and is now APPROVED.

Request #{request_id}
Vendor: {req['vendor']}
Amount: ${req['amount']:.2f}
GL Account: {gl['account_number']} - {gl['name']}

Approved on: {datetime.now().strftime('%Y-%m-%d %H:%M')}

Thank you,
Johnson Church of Christ
"""
            send_email(requester["email"], subject, body)
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

# ---------- ROUTES: PAGES ----------
@app.route("/")
def index():
    # Serve the full SPA
    return render_template("index.html")

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
        cur.execute("SELECT * FROM users ORDER BY last_name, first_name")
        users = []
        for r in cur.fetchall():
            u = dict_from_row(r)
            u.pop("password_hash", None)  # never expose hash
            users.append(u)
        return jsonify(users)

    # POST create
    data = request.get_json() or request.form
    pw = data.get("password")
    pw_hash = generate_password_hash(pw) if pw else None
    try:
        cur.execute("""
            INSERT INTO users (username, first_name, last_name, email, password_hash)
            VALUES (?, ?, ?, ?, ?)
        """, (data["username"], data["first_name"], data["last_name"], data["email"], pw_hash))
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

    if request.method == "DELETE":
        # Prevent delete if referenced? For simplicity allow, or cascade note
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()
        return "", 204

    # PUT
    data = request.get_json() or {}
    fields = ["username", "first_name", "last_name", "email"]
    values = [data.get(f) for f in fields]

    # Handle password update only if provided
    if data.get("password"):
        cur.execute("SELECT password_hash FROM users WHERE id=?", (user_id,))
        # keep existing if blank password sent, only update if non-empty
        pw_hash = generate_password_hash(data["password"])
        cur.execute("""
            UPDATE users SET username=?, first_name=?, last_name=?, email=?, password_hash=?
            WHERE id=?
        """, (values[0], values[1], values[2], values[3], pw_hash, user_id))
    else:
        cur.execute("""
            UPDATE users SET username=?, first_name=?, last_name=?, email=?
            WHERE id=?
        """, (*values, user_id))

    db.commit()
    return jsonify(get_user(user_id))

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
            d = dict_from_row(r)
            d["primary_name"] = r["primary_name"]
            d["secondary_name"] = r["secondary_name"]
            d["tertiary_name"] = r["tertiary_name"]
            rows.append(d)
        return jsonify(rows)

    # POST create
    data = request.get_json()
    cur.execute("""
        INSERT INTO gl_accounts (account_number, name, description, is_expense,
                                 primary_approver_id, secondary_approver_id, tertiary_approver_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data["account_number"], data["name"], data.get("description", ""),
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
        return jsonify(get_gl(gl_id) or ("", 404))

    if request.method == "DELETE":
        cur.execute("DELETE FROM gl_accounts WHERE id=?", (gl_id,))
        db.commit()
        return "", 204

    data = request.get_json()
    cur.execute("""
        UPDATE gl_accounts SET
            account_number=?, name=?, description=?, is_expense=?,
            primary_approver_id=?, secondary_approver_id=?, tertiary_approver_id=?
        WHERE id=?
    """, (
        data["account_number"], data["name"], data.get("description", ""),
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

    if request.method == "POST":
        data = request.get_json()
        # Create
        cur.execute("""
            INSERT INTO requests (
                vendor, invoice_number, invoice_date, amount, description,
                gl_account_id, requested_by_id, status, current_step
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', 1)
        """, (
            data["vendor"], data.get("invoice_number"),
            data.get("invoice_date"), float(data["amount"]), data.get("description", ""),
            int(data["gl_account_id"]), int(data["requested_by_id"])
        ))
        req_id = cur.lastrowid
        db.commit()

        # Snapshot approvers from GL
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

        # Start workflow (send to first approver)
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
        # compute display status / progress
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
        d["gl_display"] = f"{row['account_number']} - {row['gl_name']}"
        results.append(d)
    return jsonify(results)

@app.route("/api/requests/<int:req_id>", methods=["GET", "PUT"])
def api_request_detail(req_id):
    db = get_db()
    cur = db.cursor()
    if request.method == "GET":
        req = get_request(req_id)
        if not req:
            return "", 404
        # enrich
        req["gl"] = get_gl(req["gl_account_id"])
        req["requester"] = get_user(req["requested_by_id"])
        cur.execute("SELECT * FROM approval_history WHERE request_id=? ORDER BY acted_at", (req_id,))
        req["history"] = [dict_from_row(h) for h in cur.fetchall()]
        return jsonify(req)

    # PUT edit (only if pending)
    data = request.get_json()
    req = get_request(req_id)
    if not req or req["status"] != "Pending":
        return jsonify({"error": "Can only edit pending requests"}), 400

    cur.execute("""
        UPDATE requests SET
            vendor=?, invoice_number=?, invoice_date=?, amount=?, description=?,
            gl_account_id=?, requested_by_id=?
        WHERE id=?
    """, (
        data["vendor"], data.get("invoice_number"), data.get("invoice_date"),
        float(data["amount"]), data.get("description", ""),
        int(data["gl_account_id"]), int(data["requested_by_id"]), req_id
    ))
    db.commit()
    return jsonify(get_request(req_id))

@app.route("/api/requests/<int:req_id>/manual_action", methods=["POST"])
def api_manual_action(req_id):
    """Allow UI to manually approve/reject (useful for testing or if email unavailable)."""
    data = request.get_json()
    action = data.get("action")  # "approve" or "reject"
    notes = data.get("notes")
    # For manual we need to know which approver. Use provided or pick current.
    approver_id = data.get("approver_id")
    if not approver_id:
        req = get_request(req_id)
        gl = get_gl(req["gl_account_id"])
        chain = get_approver_chain(gl) if gl else []
        idx = req["current_step"] - 1
        if 0 <= idx < len(chain):
            approver_id = chain[idx]["id"]

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

    # Reuse same filter logic as GET /api/requests but simpler
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
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM email_log ORDER BY sent_at DESC LIMIT 100")
    return jsonify([dict_from_row(r) for r in cur.fetchall()])

@app.route("/api/stats")
def api_stats():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT status, COUNT(*) as cnt FROM requests GROUP BY status")
    by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}

    cur.execute("SELECT COUNT(*) as total FROM requests")
    total = cur.fetchone()["total"]

    return jsonify({
        "total": total,
        "by_status": by_status
    })

# ---------- INIT ----------
if __name__ == "__main__":
    with app.app_context():
        init_db()
        seed_data()
    print(f"\nJohnson Church of Christ Accounts Payable System")
    print(f"Running at http://127.0.0.1:{PORT}")
    print("Open the URL above in your browser (desktop or mobile).\n")
    app.run(host="0.0.0.0", port=PORT, debug=True)
