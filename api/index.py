import os
import re
import io
import json
import imaplib
import email
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
import qrcode

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    render_template_string,
    send_file
)


# ============================================================
# CONFIG
# ============================================================

GMAIL_EMAIL = os.getenv(
    "GMAIL_EMAIL",
    "hamza.ali.khan6200@gmail.com"
)

GMAIL_APP_PASSWORD = os.getenv(
    "GMAIL_APP_PASSWORD",
    "kdnc botx dmpa qeep"
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:pathan@786khan@db.pckwcojlzslvolnxrluh.supabase.co:5432/postgres"
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    "786"
)

FLASK_SECRET = os.getenv(
    "FLASK_SECRET",
    "change-this-secret-in-vercel"
)

CRON_SECRET = os.getenv(
    "CRON_SECRET",
    ""
)

UPI_ID = os.getenv(
    "UPI_ID",
    "khannhamzaa@fam"
)

UPI_NAME = os.getenv(
    "UPI_NAME",
    "Hamza"
)

DEFAULT_SCAN_DAYS = int(
    os.getenv(
        "DEFAULT_SCAN_DAYS",
        "100"
    )
)

DEFAULT_SCAN_INTERVAL = int(
    os.getenv(
        "DEFAULT_SCAN_INTERVAL",
        "10"
    )
)


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = FLASK_SECRET


# ============================================================
# TIME
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_string():
    return now_utc().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


# ============================================================
# DATABASE
# PostgreSQL / Neon / Supabase PostgreSQL
# ============================================================

def get_db():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing"
        )

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10
    )


def init_db():

    with get_db() as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS payments (

                id BIGSERIAL PRIMARY KEY,

                amount NUMERIC(14,2),

                sender TEXT,

                transaction_id TEXT,

                utr TEXT,

                payment_date TEXT,

                email_uid TEXT UNIQUE,

                email_subject TEXT,

                status TEXT DEFAULT 'DETECTED',

                verification_response TEXT,

                detected_at TIMESTAMPTZ DEFAULT NOW(),

                verified_at TIMESTAMPTZ
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_payments_utr
            ON payments(utr)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_payments_transaction
            ON payments(transaction_id)
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (

                key TEXT PRIMARY KEY,

                value TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS logs (

                id BIGSERIAL PRIMARY KEY,

                log_time TIMESTAMPTZ DEFAULT NOW(),

                level TEXT,

                message TEXT
            )
        """)

        con.execute("""
            INSERT INTO settings(key, value)
            VALUES ('scan_days', %s)
            ON CONFLICT(key) DO NOTHING
        """, (
            str(DEFAULT_SCAN_DAYS),
        ))

        con.execute("""
            INSERT INTO settings(key, value)
            VALUES ('scan_interval', %s)
            ON CONFLICT(key) DO NOTHING
        """, (
            str(DEFAULT_SCAN_INTERVAL),
        ))

        con.execute("""
            INSERT INTO settings(key, value)
            VALUES ('last_scan_uid', '0')
            ON CONFLICT(key) DO NOTHING
        """)


# ============================================================
# LOGGING
# ============================================================

def write_log(
    message,
    level="INFO"
):

    timestamp = now_string()

    print(
        f"[{timestamp}] [{level}] {message}",
        flush=True
    )

    try:

        with get_db() as con:

            con.execute("""
                INSERT INTO logs(
                    log_time,
                    level,
                    message
                )
                VALUES (
                    NOW(),
                    %s,
                    %s
                )
            """, (
                level,
                message
            ))

    except Exception as exc:

        print(
            f"[LOG DATABASE ERROR] {exc}",
            flush=True
        )


# ============================================================
# SETTINGS
# ============================================================

def get_setting(
    key,
    default=None
):

    try:

        with get_db() as con:

            row = con.execute("""
                SELECT value
                FROM settings
                WHERE key=%s
            """, (
                key,
            )).fetchone()

            if row:
                return row["value"]

    except Exception as exc:

        write_log(
            f"SETTING READ ERROR: {exc}",
            "ERROR"
        )

    return default


def set_setting(
    key,
    value
):

    with get_db() as con:

        con.execute("""
            INSERT INTO settings(
                key,
                value
            )
            VALUES(%s, %s)

            ON CONFLICT(key)

            DO UPDATE SET
                value=EXCLUDED.value
        """, (
            key,
            str(value)
        ))


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

try:

    init_db()

except Exception as exc:

    print(
        f"DATABASE INIT ERROR: {exc}",
        flush=True
    )


# ============================================================
# GMAIL LOGIN
# ============================================================

def gmail_login():

    if not GMAIL_EMAIL:

        raise RuntimeError(
            "GMAIL_EMAIL is missing"
        )

    if not GMAIL_APP_PASSWORD:

        raise RuntimeError(
            "GMAIL_APP_PASSWORD is missing"
        )

    mail = imaplib.IMAP4_SSL(
        "imap.gmail.com",
        993
    )

    mail.login(
        GMAIL_EMAIL,
        GMAIL_APP_PASSWORD
    )

    status, _ = mail.select(
        "INBOX",
        readonly=True
    )

    if status != "OK":

        mail.logout()

        raise RuntimeError(
            "Could not select Gmail INBOX"
        )

    return mail


# ============================================================
# EMAIL BODY
# ============================================================

def get_email_text(msg):

    pieces = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = (
                part.get_content_type()
            )

            if content_type not in (
                "text/plain",
                "text/html"
            ):
                continue

            try:

                payload = part.get_payload(
                    decode=True
                )

                if not payload:
                    continue

                charset = (
                    part.get_content_charset()
                    or "utf-8"
                )

                pieces.append(
                    payload.decode(
                        charset,
                        errors="ignore"
                    )
                )

            except Exception:
                continue

    else:

        try:

            payload = msg.get_payload(
                decode=True
            )

            if payload:

                pieces.append(
                    payload.decode(
                        "utf-8",
                        errors="ignore"
                    )
                )

        except Exception:
            pass

    text = " ".join(pieces)

    text = re.sub(
        r"<[^>]*>",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# FAMAPP PARSER
# ============================================================

def parse_famapp_email(
    raw_email,
    uid
):

    msg = email.message_from_bytes(
        raw_email
    )

    subject = str(
        msg.get(
            "Subject",
            ""
        )
    )

    body = get_email_text(
        msg
    )

    combined = (
        subject
        + " "
        + body
    )

    lower = combined.lower()

    if "famapp" not in lower:

        return None

    if (
        "successfully received"
        not in lower
    ):

        return None

    # --------------------------------------------------------
    # Amount
    # --------------------------------------------------------

    amount_match = re.search(
        r"₹\s*([\d,]+(?:\.\d+)?)",
        combined,
        re.I
    )

    if not amount_match:

        return None

    amount = float(
        amount_match
        .group(1)
        .replace(",", "")
    )

    # --------------------------------------------------------
    # Sender
    # --------------------------------------------------------

    sender = ""

    sender_match = re.search(
        r"successfully\s+received"
        r"\s*₹\s*[\d,.]+"
        r"\s*from\s*(.*?)"
        r"\s*Transaction\s*ID",
        combined,
        re.I
    )

    if sender_match:

        sender = (
            sender_match
            .group(1)
            .strip()
        )

    # --------------------------------------------------------
    # Transaction ID
    # --------------------------------------------------------

    txn = ""

    txn_match = re.search(
        r"Transaction\s*ID\s*:\s*"
        r"([A-Za-z0-9_-]+)",
        combined,
        re.I
    )

    if txn_match:

        txn = (
            txn_match
            .group(1)
            .strip()
        )

    # --------------------------------------------------------
    # UTR
    # --------------------------------------------------------

    utr = ""

    utr_match = re.search(
        r"UTR\s*:\s*"
        r"([A-Za-z0-9_-]+)",
        combined,
        re.I
    )

    if utr_match:

        utr = (
            utr_match
            .group(1)
            .strip()
        )

    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    payment_date = ""

    date_match = re.search(
        r"Date\s*:\s*(.*?)"
        r"\s*(?:Updated\s*Balance|UTR\s*:)",
        combined,
        re.I
    )

    if date_match:

        payment_date = (
            date_match
            .group(1)
            .strip()
        )

    return {

        "amount": amount,

        "sender": sender,

        "transaction_id": txn,

        "utr": utr,

        "payment_date": payment_date,

        "email_uid": str(uid),

        "email_subject": subject
    }


# ============================================================
# PAYMENT SAVE
# ============================================================

def save_payment(
    payment
):

    with get_db() as con:

        row = con.execute("""
            INSERT INTO payments(

                amount,
                sender,
                transaction_id,
                utr,
                payment_date,
                email_uid,
                email_subject,
                status,
                detected_at
            )

            VALUES(
                %s,%s,%s,%s,%s,%s,%s,%s,NOW()
            )

            ON CONFLICT(email_uid)
            DO NOTHING

            RETURNING id
        """, (

            payment["amount"],

            payment["sender"],

            payment["transaction_id"],

            payment["utr"],

            payment["payment_date"],

            payment["email_uid"],

            payment["email_subject"],

            "DETECTED"
        )).fetchone()

    if row:

        write_log(
            "PAYMENT FOUND | "
            f"₹{payment['amount']:.2f} | "
            f"TXN={payment['transaction_id']} | "
            f"UTR={payment['utr']}"
        )

        return True

    return False


# ============================================================
# GMAIL SCAN
# ============================================================

def scan_gmail(
    days=1
):

    mail = None

    try:

        write_log(
            f"GMAIL SCAN START | LAST {days} DAYS"
        )

        mail = gmail_login()

        write_log(
            "GMAIL CONNECTED"
        )

        since_date = (
            datetime.now()
            - timedelta(days=days)
        ).strftime(
            "%d-%b-%Y"
        )

        status, data = mail.uid(
            "search",
            None,
            f"SINCE {since_date}"
        )

        if status != "OK":

            raise RuntimeError(
                "Gmail search failed"
            )

        uids = data[0].split()

        write_log(
            f"GMAIL UID COUNT={len(uids)}"
        )

        found = 0
        saved = 0

        # newest first

        for uid in reversed(uids):

            uid_string = (
                uid.decode()
                if isinstance(
                    uid,
                    bytes
                )
                else str(uid)
            )

            # Existing email UID check
            with get_db() as con:

                exists = con.execute("""
                    SELECT id
                    FROM payments
                    WHERE email_uid=%s
                    LIMIT 1
                """, (
                    uid_string,
                )).fetchone()

            if exists:

                continue

            status, result = mail.uid(
                "fetch",
                uid,
                "(RFC822)"
            )

            if status != "OK":

                continue

            raw = None

            for item in result:

                if isinstance(
                    item,
                    tuple
                ):

                    raw = item[1]
                    break

            if not raw:

                continue

            payment = parse_famapp_email(
                raw,
                uid_string
            )

            if not payment:

                continue

            found += 1

            if save_payment(
                payment
            ):

                saved += 1

        write_log(
            f"GMAIL SCAN COMPLETE | "
            f"FOUND={found} | "
            f"SAVED={saved}"
        )

        return {

            "success": True,

            "found": found,

            "saved": saved,

            "checked": len(uids),

            "days": days
        }

    except Exception as exc:

        write_log(
            f"GMAIL SCAN ERROR: {exc}",
            "ERROR"
        )

        return {

            "success": False,

            "error": str(exc)
        }

    finally:

        if mail:

            try:
                mail.logout()
            except Exception:
                pass


# ============================================================
# ADMIN AUTH
# ============================================================

def is_admin():

    return (
        session.get(
            "admin"
        ) is True
    )


def require_admin():

    if not is_admin():

        return jsonify({
            "success": False,
            "error": "Admin login required"
        }), 401

    return None


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/admin/login",
    methods=[
        "GET",
        "POST"
    ]
)
def admin_login():

    if request.method == "POST":

        password = (
            request.form.get(
                "password",
                ""
            )
        )

        if secrets.compare_digest(
            password,
            ADMIN_PASSWORD
        ):

            session["admin"] = True

            write_log(
                "ADMIN LOGIN"
            )

            return redirect(
                "/admin"
            )

        write_log(
            "ADMIN LOGIN FAILED",
            "WARNING"
        )

        error = "Invalid password"

    else:

        error = ""

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>Admin Login</title>

<style>
body{
background:#090909;
color:white;
font-family:Arial;
display:flex;
align-items:center;
justify-content:center;
min-height:100vh;
}

.box{
background:#151515;
padding:25px;
border-radius:15px;
width:300px;
}

input,button{
width:100%;
box-sizing:border-box;
padding:12px;
margin-top:10px;
border-radius:8px;
border:1px solid #444;
background:#101010;
color:white;
}

button{
cursor:pointer;
}

.error{
color:#ff5555;
}
</style>
</head>

<body>

<div class="box">

<h2>🔐 Admin</h2>

<form method="POST">

<input
type="password"
name="password"
placeholder="Password"
required
>

<button>
Login
</button>

</form>

<div class="error">
{{ error }}
</div>

</div>

</body>
</html>
""", error=error)


# ============================================================
# LOGOUT
# ============================================================

@app.route(
    "/admin/logout"
)
def admin_logout():

    session.clear()

    return redirect(
        "/admin/login"
    )


# ============================================================
# QR CODE
# ============================================================

@app.route(
    "/api/qr"
)
def generate_qr():

    amount = request.args.get(
        "amount"
    )

    txn = request.args.get(
        "txn",
        "PAYMENT"
    )

    if not amount:

        return jsonify({
            "success": False,
            "error": "amount required"
        }), 400

    try:

        amount_value = float(
            amount
        )

        if amount_value <= 0:
            raise ValueError

    except Exception:

        return jsonify({
            "success": False,
            "error": "invalid amount"
        }), 400

    upi_uri = (
        "upi://pay"
        "?pa=" + quote(UPI_ID)
        + "&pn=" + quote(UPI_NAME)
        + "&am=" + quote(
            f"{amount_value:.2f}"
        )
        + "&cu=INR"
        + "&tn=" + quote(txn)
    )

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4
    )

    qr.add_data(
        upi_uri
    )

    qr.make(
        fit=True
    )

    image = qr.make_image()

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="PNG"
    )

    buffer.seek(0)

    return send_file(
        buffer,
        mimetype="image/png",
        download_name="payment_qr.png"
    )


# ============================================================
# CREATE PAYMENT
# ============================================================

@app.route(
    "/api/payment",
    methods=[
        "GET",
        "POST"
    ]
)
def create_payment():

    data = (
        request.get_json(
            silent=True
        )
        or request.args
        or request.form
    )

    amount = data.get(
        "amount"
    )

    if not amount:

        return jsonify({
            "success": False,
            "error": "amount required"
        }), 400

    try:

        amount_value = float(
            amount
        )

        if amount_value <= 0:
            raise ValueError

    except Exception:

        return jsonify({
            "success": False,
            "error": "invalid amount"
        }), 400

    # Random order ID
    order_id = (
        "PAY-"
        + secrets.token_hex(6).upper()
    )

    return jsonify({

        "success": True,

        "order_id": order_id,

        "amount": amount_value,

        "currency": "INR",

        "upi_id": UPI_ID,

        "upi_name": UPI_NAME,

        "qr_endpoint":
            "/api/qr"
            f"?amount={quote(str(amount_value))}"
            f"&txn={quote(order_id)}"
    })


# ============================================================
# VERIFY UTR OR TXN
# ============================================================

@app.route(
    "/api/verify/<identifier>"
)
def verify_payment(
    identifier
):

    identifier = identifier.strip()

    if not identifier:

        return jsonify({
            "success": False,
            "verified": False,
            "error": "Identifier required"
        }), 400

    with get_db() as con:

        row = con.execute("""
            SELECT
                id,
                amount,
                sender,
                transaction_id,
                utr,
                payment_date,
                email_uid,
                email_subject,
                status,
                verification_response,
                detected_at,
                verified_at

            FROM payments

            WHERE
                utr=%s
                OR transaction_id=%s

            ORDER BY id DESC

            LIMIT 1
        """, (
            identifier,
            identifier
        )).fetchone()

    if not row:

        write_log(
            f"VERIFY NOT FOUND | {identifier}",
            "WARNING"
        )

        return jsonify({

            "success": True,

            "verified": False,

            "matched_by": None,

            "message": "Payment not found",

            "identifier": identifier

        })

    if row["utr"] == identifier:

        matched_by = "UTR"

    else:

        matched_by = "TRANSACTION_ID"

    # Update verification status
    verification_info = json.dumps({

        "verified": True,

        "matched_by": matched_by,

        "identifier": identifier,

        "verified_at": now_string()

    })

    with get_db() as con:

        con.execute("""
            UPDATE payments

            SET
                status='VERIFIED',
                verification_response=%s,
                verified_at=NOW()

            WHERE id=%s
        """, (
            verification_info,
            row["id"]
        ))

    write_log(
        f"PAYMENT VERIFIED | "
        f"BY={matched_by} | "
        f"ID={identifier}"
    )

    payment = dict(row)

    payment["status"] = "VERIFIED"

    payment["verification_response"] = (
        verification_info
    )

    return jsonify({

        "success": True,

        "verified": True,

        "matched_by": matched_by,

        "message": "Payment verified",

        "payment": payment

    })


# ============================================================
# PAYMENTS
# ============================================================

@app.route(
    "/api/payments"
)
def payments():

    auth = require_admin()

    if auth:
        return auth

    with get_db() as con:

        rows = con.execute("""
            SELECT *
            FROM payments
            ORDER BY id DESC
            LIMIT 500
        """).fetchall()

    return jsonify({

        "success": True,

        "count": len(rows),

        "payments": [
            dict(row)
            for row in rows
        ]
    })


# ============================================================
# LOGS
# ============================================================

@app.route(
    "/api/logs"
)
def api_logs():

    auth = require_admin()

    if auth:
        return auth

    with get_db() as con:

        rows = con.execute("""
            SELECT *
            FROM logs
            ORDER BY id DESC
            LIMIT 500
        """).fetchall()

    return jsonify({

        "success": True,

        "count": len(rows),

        "logs": [
            dict(row)
            for row in rows
        ]
    })


# ============================================================
# MANUAL NEW SCAN
# ============================================================

@app.route(
    "/api/scan"
)
def scan_new():

    auth = require_admin()

    if auth:
        return auth

    days = 1

    write_log(
        "MANUAL NEW EMAIL SCAN"
    )

    return jsonify(
        scan_gmail(days)
    )


# ============================================================
# MANUAL OLD SCAN
# ============================================================

@app.route(
    "/api/scan-old"
)
def scan_old():

    auth = require_admin()

    if auth:
        return auth

    days = int(
        get_setting(
            "scan_days",
            DEFAULT_SCAN_DAYS
        )
    )

    days = max(
        1,
        min(
            days,
            3650
        )
    )

    write_log(
        f"MANUAL OLD SCAN | {days} DAYS"
    )

    return jsonify(
        scan_gmail(days)
    )


# ============================================================
# SETTINGS
# ============================================================

@app.route(
    "/api/settings",
    methods=[
        "GET",
        "POST"
    ]
)
def settings():

    auth = require_admin()

    if auth:
        return auth

    if request.method == "POST":

        data = (
            request.get_json(
                silent=True
            )
            or request.form
        )

        if "scan_days" in data:

            try:

                days = int(
                    data[
                        "scan_days"
                    ]
                )

                days = max(
                    1,
                    min(days, 3650)
                )

                set_setting(
                    "scan_days",
                    days
                )

            except Exception:

                return jsonify({
                    "success": False,
                    "error":
                        "Invalid scan_days"
                }), 400

        if "scan_interval" in data:

            try:

                interval = int(
                    data[
                        "scan_interval"
                    ]
                )

                interval = max(
                    60,
                    interval
                )

                set_setting(
                    "scan_interval",
                    interval
                )

            except Exception:

                return jsonify({
                    "success": False,
                    "error":
                        "Invalid scan_interval"
                }), 400

        write_log(
            "SETTINGS UPDATED"
        )

    return jsonify({

        "success": True,

        "scan_days": int(
            get_setting(
                "scan_days",
                DEFAULT_SCAN_DAYS
            )
        ),

        "scan_interval": int(
            get_setting(
                "scan_interval",
                DEFAULT_SCAN_INTERVAL
            )
        )
    })


# ============================================================
# CRON SCAN
# ============================================================

@app.route(
    "/api/cron/scan"
)
def cron_scan():

    if CRON_SECRET:

        supplied = (
            request.headers.get(
                "Authorization",
                ""
            )
        )

        expected = (
            "Bearer "
            + CRON_SECRET
        )

        if not secrets.compare_digest(
            supplied,
            expected
        ):

            return jsonify({
                "success": False,
                "error": "Unauthorized"
            }), 401

    days = 1

    write_log(
        "CRON EMAIL SCAN START"
    )

    result = scan_gmail(
        days
    )

    write_log(
        "CRON EMAIL SCAN FINISHED"
    )

    return jsonify(
        result
    )


# ============================================================
# API INFORMATION
# ============================================================

@app.route(
    "/api"
)
def api_info():

    return jsonify({

        "name":
            "FamApp Payment Gateway",

        "version":
            "Vercel-1.0",

        "upi_id":
            UPI_ID,

        "endpoints": {

            "health":
                "/health",

            "api":
                "/api",

            "create_payment":
                "/api/payment?amount=60",

            "qr":
                "/api/qr?amount=60&txn=ORDER123",

            "verify":
                "/api/verify/<UTR_OR_TXN>",

            "payments":
                "/api/payments",

            "logs":
                "/api/logs",

            "new_scan":
                "/api/scan",

            "old_scan":
                "/api/scan-old",

            "settings":
                "/api/settings",

            "cron":
                "/api/cron/scan",

            "admin":
                "/admin",

            "login":
                "/admin/login",

            "logout":
                "/admin/logout"

        }

    })


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health"
)
def health():

    database_ok = False

    try:

        with get_db() as con:

            con.execute(
                "SELECT 1"
            )

        database_ok = True

    except Exception:
        database_ok = False

    return jsonify({

        "status":
            "online",

        "gmail_configured":
            bool(
                GMAIL_EMAIL
                and GMAIL_APP_PASSWORD
            ),

        "database_configured":
            bool(
                DATABASE_URL
            ),

        "database_connected":
            database_ok,

        "upi_id":
            UPI_ID,

        "scan_days":
            int(
                get_setting(
                    "scan_days",
                    DEFAULT_SCAN_DAYS
                )
            ),

        "scan_interval":
            int(
                get_setting(
                    "scan_interval",
                    DEFAULT_SCAN_INTERVAL
                )
            )

    })


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route(
    "/admin"
)
def admin():

    if not is_admin():

        return redirect(
            "/admin/login"
        )

    with get_db() as con:

        payment_rows = con.execute("""
            SELECT *
            FROM payments
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()

        log_rows = con.execute("""
            SELECT *
            FROM logs
            ORDER BY id DESC
            LIMIT 150
        """).fetchall()

    days = int(
        get_setting(
            "scan_days",
            DEFAULT_SCAN_DAYS
        )
    )

    interval = int(
        get_setting(
            "scan_interval",
            DEFAULT_SCAN_INTERVAL
        )
    )

    return render_template_string(
        ADMIN_HTML,

        payments=payment_rows,

        logs=log_rows,

        days=days,

        interval=interval,

        gmail=GMAIL_EMAIL,

        upi=UPI_ID
    )


# ============================================================
# ADMIN HTML
# ============================================================

ADMIN_HTML = """
<!DOCTYPE html>

<html>

<head>

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>FamApp Gateway</title>

<style>

body {
    background:#080808;
    color:#eee;
    font-family:Arial,sans-serif;
    margin:0;
    padding:15px;
}

.container {
    max-width:1250px;
    margin:auto;
}

.card {
    background:#151515;
    border:1px solid #292929;
    border-radius:14px;
    padding:16px;
    margin-bottom:15px;
    overflow:auto;
}

input,
button {
    background:#0d0d0d;
    color:white;
    border:1px solid #444;
    border-radius:8px;
    padding:10px;
    margin:4px;
}

button {
    cursor:pointer;
}

table {
    width:100%;
    border-collapse:collapse;
}

th,
td {
    padding:8px;
    border-bottom:1px solid #292929;
    text-align:left;
    white-space:nowrap;
    font-size:12px;
}

.endpoint {
    background:#0d0d0d;
    padding:10px;
    margin:5px 0;
    border-radius:8px;
    border:1px solid #292929;
}

pre {
    white-space:pre-wrap;
    word-break:break-word;
}

a {
    color:white;
}

</style>

</head>

<body>

<div class="container">

<h1>💰 FamApp Payment Gateway</h1>

<div class="card">

<h3>System</h3>

Gmail:
<b>{{ gmail }}</b>

<br>

UPI:
<b>{{ upi }}</b>

<br>

Old scan:
<b>{{ days }} days</b>

<br>

Cron interval:
<b>{{ interval }} seconds</b>

</div>


<div class="card">

<h2>Scanner</h2>

<button onclick="scanNew()">
⚡ Scan New
</button>

<button onclick="scanOld()">
📥 Scan Old
</button>

<br>

<input
id="days"
type="number"
min="1"
max="3650"
value="{{ days }}"
>

<input
id="interval"
type="number"
min="60"
value="{{ interval }}"
>

<button onclick="saveSettings()">
Save Settings
</button>

</div>


<div class="card">

<h2>🔗 API Endpoints</h2>

<div class="endpoint">
GET /health
</div>

<div class="endpoint">
GET /api
</div>

<div class="endpoint">
GET /api/payment?amount=60
</div>

<div class="endpoint">
GET /api/qr?amount=60&txn=ORDER123
</div>

<div class="endpoint">
GET /api/verify/&lt;UTR_OR_TXN&gt;
</div>

<div class="endpoint">
GET /api/payments
</div>

<div class="endpoint">
GET /api/logs
</div>

<div class="endpoint">
GET /api/scan
</div>

<div class="endpoint">
GET /api/scan-old
</div>

<div class="endpoint">
GET /api/settings
</div>

<div class="endpoint">
GET /api/cron/scan
</div>

</div>


<div class="card">

<h2>💳 Payments</h2>

<table>

<tr>

<th>ID</th>
<th>Amount</th>
<th>Sender</th>
<th>Transaction</th>
<th>UTR</th>
<th>Status</th>
<th>Date</th>
<th>Detected</th>

</tr>

{% for p in payments %}

<tr>

<td>{{ p["id"] }}</td>

<td>₹{{ p["amount"] }}</td>

<td>{{ p["sender"] }}</td>

<td>{{ p["transaction_id"] }}</td>

<td>{{ p["utr"] }}</td>

<td>{{ p["status"] }}</td>

<td>{{ p["payment_date"] }}</td>

<td>{{ p["detected_at"] }}</td>

</tr>

{% endfor %}

</table>

</div>


<div class="card">

<h2>📋 Logs</h2>

<pre>
{% for x in logs %}
{{ x["log_time"] }} | {{ x["level"] }} | {{ x["message"] }}
{% endfor %}
</pre>

</div>


<div class="card">

<a href="/admin/logout">
🚪 Logout
</a>

</div>

</div>


<script>

async function scanNew() {

    const r =
        await fetch("/api/scan");

    const d =
        await r.json();

    alert(
        JSON.stringify(
            d,
            null,
            2
        )
    );

    location.reload();
}


async function scanOld() {

    const r =
        await fetch("/api/scan-old");

    const d =
        await r.json();

    alert(
        JSON.stringify(
            d,
            null,
            2
        )
    );

    location.reload();
}


async function saveSettings() {

    const days =
        document.getElementById(
            "days"
        ).value;

    const interval =
        document.getElementById(
            "interval"
        ).value;

    const r =
        await fetch(
            "/api/settings",
            {
                method:"POST",

                headers:{
                    "Content-Type":
                        "application/json"
                },

                body:JSON.stringify({

                    scan_days:days,

                    scan_interval:interval

                })
            }
        );

    const d =
        await r.json();

    alert(
        JSON.stringify(
            d,
            null,
            2
        )
    );

    location.reload();
}

</script>

</body>

</html>
"""


# ============================================================
# VERCEL ENTRYPOINT
# ============================================================

# Vercel imports this Flask object:
#
# app
#
# No app.run() here.
# ============================================================
