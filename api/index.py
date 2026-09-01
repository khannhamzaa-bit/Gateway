import os
import re
import io
import imaplib
import email
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import psycopg2
from psycopg2.extras import RealDictCursor
import qrcode

from flask import (
    Flask,
    jsonify,
    request,
    session,
    redirect,
    render_template_string
)


# ============================================================
# CONFIG
# ============================================================

GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "hamza.ali.khan6200@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "kdnc botx dmpa qeep")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:pathan@786khan@db.pckwcojlzslvolnxrluh.supabase.co:5432/postgres")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "786")
FLASK_SECRET = os.getenv("FLASK_SECRET", "change-this-secret")

UPI_ID = os.getenv("UPI_ID", "khannhamzaa@fam")
UPI_NAME = os.getenv("UPI_NAME", "Hamza")

DEFAULT_SCAN_DAYS = int(
    os.getenv("DEFAULT_SCAN_DAYS", "100")
)

DEFAULT_SCAN_INTERVAL = int(
    os.getenv("DEFAULT_SCAN_INTERVAL", "10")
)


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = FLASK_SECRET

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s"
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        sslmode="require"
    )


def init_db():

    con = get_db()

    try:

        with con.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id BIGSERIAL PRIMARY KEY,
                    amount NUMERIC(12,2),
                    sender TEXT,
                    transaction_id TEXT,
                    utr TEXT,
                    payment_date TEXT,
                    email_uid TEXT UNIQUE,
                    email_subject TEXT,
                    status TEXT DEFAULT 'DETECTED',
                    detected_at TIMESTAMPTZ DEFAULT NOW(),
                    verified_at TIMESTAMPTZ
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_payments_utr
                ON payments(utr)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_payments_txn
                ON payments(transaction_id)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS gateway_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS gateway_logs (
                    id BIGSERIAL PRIMARY KEY,
                    log_time TIMESTAMPTZ DEFAULT NOW(),
                    level TEXT,
                    message TEXT
                )
            """)

            cur.execute("""
                INSERT INTO gateway_settings(key, value)
                VALUES ('scan_days', %s)
                ON CONFLICT(key) DO NOTHING
            """, (str(DEFAULT_SCAN_DAYS),))

            cur.execute("""
                INSERT INTO gateway_settings(key, value)
                VALUES ('scan_interval', %s)
                ON CONFLICT(key) DO NOTHING
            """, (str(DEFAULT_SCAN_INTERVAL),))

        con.commit()

    finally:
        con.close()


def db_log(message, level="INFO"):

    logging.log(
        getattr(logging, level, logging.INFO),
        message
    )

    try:

        con = get_db()

        try:

            with con.cursor() as cur:

                cur.execute("""
                    INSERT INTO gateway_logs(level, message)
                    VALUES (%s, %s)
                """, (
                    level,
                    message
                ))

            con.commit()

        finally:
            con.close()

    except Exception as e:

        logging.error(
            "DATABASE LOG ERROR: %s",
            e
        )


def get_setting(key, default):

    con = get_db()

    try:

        with con.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT value
                FROM gateway_settings
                WHERE key = %s
            """, (key,))

            row = cur.fetchone()

            if row:
                return row["value"]

            return str(default)

    finally:
        con.close()


def set_setting(key, value):

    con = get_db()

    try:

        with con.cursor() as cur:

            cur.execute("""
                INSERT INTO gateway_settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT(key)
                DO UPDATE SET value = EXCLUDED.value
            """, (
                key,
                str(value)
            ))

        con.commit()

    finally:
        con.close()


# ============================================================
# DATABASE STARTUP
# ============================================================

@app.before_request
def startup_database():

    try:
        init_db()

    except Exception as e:

        logging.error(
            "DATABASE INIT ERROR: %s",
            e
        )


# ============================================================
# GMAIL
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

    mail.select(
        "INBOX",
        readonly=True
    )

    db_log("GMAIL CONNECTED")

    return mail


# ============================================================
# EMAIL BODY
# ============================================================

def get_email_text(msg):

    parts = []

    if msg.is_multipart():

        for part in msg.walk():

            content_type = part.get_content_type()

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

                parts.append(
                    payload.decode(
                        charset,
                        errors="ignore"
                    )
                )

            except Exception:
                pass

    else:

        try:

            payload = msg.get_payload(
                decode=True
            )

            if payload:

                charset = (
                    msg.get_content_charset()
                    or "utf-8"
                )

                parts.append(
                    payload.decode(
                        charset,
                        errors="ignore"
                    )
                )

        except Exception:
            pass

    text = " ".join(parts)

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

def parse_famapp(raw_email, uid):

    try:

        msg = email.message_from_bytes(
            raw_email
        )

        subject = str(
            msg.get("Subject", "")
        )

        body = get_email_text(msg)

        text = (
            subject
            + " "
            + body
        )

        lower = text.lower()

        if "famapp" not in lower:
            return None

        if "successfully received" not in lower:
            return None

        # ----------------------------------------------------
        # AMOUNT
        # ----------------------------------------------------

        amount_match = re.search(
            r"₹\s*([\d,]+(?:\.\d+)?)",
            text,
            re.I
        )

        if not amount_match:
            return None

        amount = float(
            amount_match.group(1)
            .replace(",", "")
        )

        # ----------------------------------------------------
        # SENDER
        # ----------------------------------------------------

        sender = ""

        sender_match = re.search(
            r"successfully\s+received"
            r"\s*₹\s*[\d,.]+"
            r"\s*from\s*(.*?)"
            r"\s*Transaction\s*ID",
            text,
            re.I
        )

        if sender_match:

            sender = (
                sender_match.group(1)
                .strip()
            )

        # ----------------------------------------------------
        # TRANSACTION ID
        # ----------------------------------------------------

        transaction_id = ""

        txn_match = re.search(
            r"Transaction\s*ID\s*:\s*"
            r"([A-Za-z0-9_-]+)",
            text,
            re.I
        )

        if txn_match:

            transaction_id = (
                txn_match.group(1)
                .strip()
            )

        # ----------------------------------------------------
        # UTR
        # ----------------------------------------------------

        utr = ""

        utr_match = re.search(
            r"UTR\s*:\s*"
            r"([A-Za-z0-9_-]+)",
            text,
            re.I
        )

        if utr_match:

            utr = (
                utr_match.group(1)
                .strip()
            )

        # ----------------------------------------------------
        # DATE
        # ----------------------------------------------------

        payment_date = ""

        date_match = re.search(
            r"Date\s*:\s*(.*?)"
            r"\s*(?:Updated\s*Balance|UTR\s*:)",
            text,
            re.I
        )

        if date_match:

            payment_date = (
                date_match.group(1)
                .strip()
            )

        if not utr and not transaction_id:
            return None

        return {
            "amount": amount,
            "sender": sender,
            "transaction_id": transaction_id,
            "utr": utr,
            "payment_date": payment_date,
            "email_uid": str(uid),
            "email_subject": subject
        }

    except Exception as e:

        db_log(
            f"PARSER ERROR: {e}",
            "ERROR"
        )

        return None


# ============================================================
# SAVE PAYMENT
# ============================================================

def save_payment(payment):

    con = get_db()

    try:

        with con.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                INSERT INTO payments (
                    amount,
                    sender,
                    transaction_id,
                    utr,
                    payment_date,
                    email_uid,
                    email_subject,
                    status
                )
                VALUES (
                    %s,%s,%s,%s,%s,%s,%s,'DETECTED'
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
                payment["email_subject"]
            ))

            row = cur.fetchone()

        con.commit()

        if row:

            db_log(
                "PAYMENT FOUND | "
                f"₹{payment['amount']:.2f} | "
                f"TXN={payment['transaction_id']} | "
                f"UTR={payment['utr']}"
            )

            return True

        return False

    finally:
        con.close()


# ============================================================
# GMAIL SCAN
# ============================================================

def scan_gmail(days=None):

    if days is None:

        days = int(
            get_setting(
                "scan_days",
                DEFAULT_SCAN_DAYS
            )
        )

    days = max(
        1,
        min(days, 365)
    )

    db_log(
        f"GMAIL SCAN START | LAST {days} DAYS"
    )

    mail = None

    try:

        mail = gmail_login()

        since = (
            datetime.utcnow()
            - timedelta(days=days)
        ).strftime(
            "%d-%b-%Y"
        )

        status, data = mail.uid(
            "search",
            None,
            f"SINCE {since}"
        )

        if status != "OK":

            raise RuntimeError(
                "Gmail search failed"
            )

        uids = data[0].split()

        found = 0
        saved = 0

        db_log(
            f"GMAIL UID COUNT={len(uids)}"
        )

        for uid in reversed(uids):

            uid_text = (
                uid.decode()
                if isinstance(uid, bytes)
                else str(uid)
            )

            status, result = mail.uid(
                "fetch",
                uid,
                "(RFC822)"
            )

            if status != "OK":
                continue

            raw = None

            for item in result:

                if isinstance(item, tuple):

                    raw = item[1]
                    break

            if not raw:
                continue

            payment = parse_famapp(
                raw,
                uid_text
            )

            if not payment:
                continue

            found += 1

            if save_payment(payment):
                saved += 1

        db_log(
            f"GMAIL SCAN COMPLETE | "
            f"FOUND={found} | SAVED={saved}"
        )

        return {
            "success": True,
            "days": days,
            "checked_emails": len(uids),
            "famapp_found": found,
            "saved": saved
        }

    except Exception as e:

        db_log(
            f"SCAN ERROR: {e}",
            "ERROR"
        )

        return {
            "success": False,
            "error": str(e)
        }

    finally:

        if mail:

            try:
                mail.logout()
            except Exception:
                pass


# ============================================================
# FIND PAYMENT
# ============================================================

def find_payment(identifier):

    con = get_db()

    try:

        with con.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM payments
                WHERE utr = %s
                   OR transaction_id = %s
                ORDER BY id DESC
                LIMIT 1
            """, (
                identifier,
                identifier
            ))

            row = cur.fetchone()

            return dict(row) if row else None

    finally:
        con.close()


# ============================================================
# VERIFY
# ============================================================

@app.route(
    "/api/verify/<path:identifier>"
)
def verify(identifier):

    identifier = identifier.strip()

    if not identifier:

        return jsonify({
            "success": False,
            "verified": False,
            "error": "Identifier required"
        }), 400

    db_log(
        f"VERIFICATION REQUEST | ID={identifier}"
    )

    # First check existing database
    payment = find_payment(identifier)

    # If not found, automatically scan configured history
    if not payment:

        db_log(
            f"PAYMENT NOT IN DATABASE | "
            f"STARTING GMAIL SCAN | ID={identifier}"
        )

        scan_result = scan_gmail()

        payment = find_payment(identifier)

        if not payment:

            db_log(
                f"PAYMENT NOT FOUND | ID={identifier}",
                "WARNING"
            )

            return jsonify({
                "success": True,
                "verified": False,
                "identifier": identifier,
                "scan": scan_result,
                "message": "Payment not found"
            })

    if payment["utr"] == identifier:

        matched_by = "UTR"

    elif payment["transaction_id"] == identifier:

        matched_by = "TRANSACTION_ID"

    else:

        matched_by = "UNKNOWN"

    # Mark verification
    con = get_db()

    try:

        with con.cursor() as cur:

            cur.execute("""
                UPDATE payments
                SET status = 'VERIFIED',
                    verified_at = NOW()
                WHERE id = %s
            """, (
                payment["id"],
            ))

        con.commit()

    finally:
        con.close()

    db_log(
        f"PAYMENT VERIFIED | "
        f"BY={matched_by} | "
        f"ID={identifier}"
    )

    payment = find_payment(identifier)

    return jsonify({
        "success": True,
        "verified": True,
        "matched_by": matched_by,
        "payment": payment
    })


# ============================================================
# CREATE PAYMENT
# ============================================================

@app.route(
    "/api/payment",
    methods=["GET", "POST"]
)
def create_payment():

    data = (
        request.get_json(silent=True)
        or request.args
        or request.form
    )

    amount = data.get("amount")

    if amount is None:

        return jsonify({
            "success": False,
            "error": "amount is required"
        }), 400

    try:

        amount = float(amount)

        if amount <= 0:
            raise ValueError

    except Exception:

        return jsonify({
            "success": False,
            "error": "Invalid amount"
        }), 400

    order_id = (
        "PAY-"
        + datetime.utcnow().strftime(
            "%Y%m%d%H%M%S%f"
        )
    )

    return jsonify({
        "success": True,
        "order_id": order_id,
        "amount": amount,
        "upi_id": UPI_ID,
        "upi_name": UPI_NAME,
        "qr_url": (
            "/api/qr"
            f"?amount={quote(f'{amount:.2f}')}"
            f"&txn={quote(order_id)}"
        )
    })


# ============================================================
# QR
# ============================================================

@app.route("/api/qr")
def qr():

    amount = request.args.get("amount")
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

        amount_float = float(amount)

        if amount_float <= 0:
            raise ValueError

    except Exception:

        return jsonify({
            "success": False,
            "error": "Invalid amount"
        }), 400

    upi_url = (
        "upi://pay"
        "?pa=" + quote(UPI_ID)
        + "&pn=" + quote(UPI_NAME)
        + "&am=" + quote(
            f"{amount_float:.2f}"
        )
        + "&cu=INR"
        + "&tn=" + quote(txn)
    )

    image = qrcode.make(
        upi_url
    )

    output = io.BytesIO()

    image.save(
        output,
        format="PNG"
    )

    output.seek(0)

    return app.response_class(
        output.getvalue(),
        mimetype="image/png",
        headers={
            "Cache-Control":
                "no-store"
        }
    )


# ============================================================
# PAYMENTS
# ============================================================

@app.route("/api/payments")
def payments():

    con = get_db()

    try:

        with con.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM payments
                ORDER BY id DESC
                LIMIT 500
            """)

            rows = cur.fetchall()

            return jsonify({
                "success": True,
                "count": len(rows),
                "payments": [
                    dict(row)
                    for row in rows
                ]
            })

    finally:
        con.close()


# ============================================================
# LOGS
# ============================================================

@app.route("/api/logs")
def logs():

    if not admin_required():

        return jsonify({
            "success": False,
            "error": "Admin login required"
        }), 401

    con = get_db()

    try:

        with con.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM gateway_logs
                ORDER BY id DESC
                LIMIT 500
            """)

            rows = cur.fetchall()

            return jsonify({
                "success": True,
                "logs": [
                    dict(row)
                    for row in rows
                ]
            })

    finally:
        con.close()


# ============================================================
# MANUAL SCAN
# ============================================================

@app.route("/api/scan")
def manual_scan():

    if not admin_required():

        return jsonify({
            "success": False,
            "error": "Admin login required"
        }), 401

    result = scan_gmail()

    return jsonify(result)


# ============================================================
# OLD SCAN
# ============================================================

@app.route("/api/scan-old")
def old_scan():

    if not admin_required():

        return jsonify({
            "success": False,
            "error": "Admin login required"
        }), 401

    days = int(
        get_setting(
            "scan_days",
            DEFAULT_SCAN_DAYS
        )
    )

    result = scan_gmail(days)

    return jsonify({
        "success":
            result.get("success", False),
        "days":
            days,
        "result":
            result
    })


# ============================================================
# SETTINGS
# ============================================================

@app.route(
    "/api/settings",
    methods=["GET", "POST"]
)
def settings():

    if not admin_required():

        return jsonify({
            "success": False,
            "error": "Admin login required"
        }), 401

    if request.method == "POST":

        data = (
            request.get_json(silent=True)
            or request.form
        )

        if "scan_days" in data:

            try:

                days = int(
                    data["scan_days"]
                )

                days = max(
                    1,
                    min(days, 365)
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
                    data["scan_interval"]
                )

                interval = max(
                    10,
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

        db_log(
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
# HEALTH
# ============================================================

@app.route("/health")
def health():

    database = False

    try:

        con = get_db()

        con.close()

        database = True

    except Exception:
        pass

    return jsonify({
        "status": "online",
        "database": database,
        "gmail_configured":
            bool(
                GMAIL_EMAIL
                and GMAIL_APP_PASSWORD
            ),
        "upi_id": UPI_ID,
        "scan_days": int(
            get_setting(
                "scan_days",
                DEFAULT_SCAN_DAYS
            )
        ) if database else DEFAULT_SCAN_DAYS
    })


# ============================================================
# API LIST
# ============================================================

@app.route("/api")
def api_list():

    return jsonify({

        "name":
            "FamApp Payment Gateway",

        "version":
            "2.0",

        "upi_id":
            UPI_ID,

        "endpoints": {

            "health":
                "GET /health",

            "api":
                "GET /api",

            "create_payment":
                "GET /api/payment?amount=60",

            "qr":
                "GET /api/qr?amount=60&txn=ORDER123",

            "verify":
                "GET /api/verify/<UTR_OR_TXN>",

            "payments":
                "GET /api/payments",

            "scan":
                "GET /api/scan",

            "old_scan":
                "GET /api/scan-old",

            "logs":
                "GET /api/logs",

            "settings":
                "GET/POST /api/settings",

            "admin":
                "GET /admin",

            "login":
                "GET/POST /admin/login",

            "logout":
                "GET /admin/logout"
        }
    })


# ============================================================
# ADMIN AUTH
# ============================================================

def admin_required():

    return session.get("admin") is True


# ============================================================
# ADMIN LOGIN
# ============================================================

@app.route(
    "/admin/login",
    methods=["GET", "POST"]
)
def admin_login():

    error = ""

    if request.method == "POST":

        password = request.form.get(
            "password",
            ""
        )

        if password == ADMIN_PASSWORD:

            session["admin"] = True

            return redirect("/admin")

        error = "Invalid password"

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
    justify-content:center;
    align-items:center;
    min-height:100vh;
}
.box{
    background:#161616;
    padding:25px;
    border-radius:15px;
    width:300px;
}
input,button{
    width:100%;
    padding:12px;
    margin-top:10px;
    box-sizing:border-box;
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
placeholder="Admin password"
required
>

<button>
Login
</button>

</form>

<p class="error">
{{ error }}
</p>

</div>

</body>
</html>
""", error=error)


# ============================================================
# ADMIN LOGOUT
# ============================================================

@app.route("/admin/logout")
def admin_logout():

    session.clear()

    return redirect(
        "/admin/login"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

@app.route("/admin")
def admin():

    if not admin_required():

        return redirect(
            "/admin/login"
        )

    con = get_db()

    try:

        with con.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM payments
                ORDER BY id DESC
                LIMIT 100
            """)

            payments = cur.fetchall()

            cur.execute("""
                SELECT *
                FROM gateway_logs
                ORDER BY id DESC
                LIMIT 150
            """)

            logs = cur.fetchall()

    finally:
        con.close()

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
        payments=payments,
        logs=logs,
        days=days,
        interval=interval,
        gmail=GMAIL_EMAIL,
        upi=UPI_ID
    )


# ============================================================
# ADMIN HTML
# ============================================================

ADMIN_HTML = r"""
<!DOCTYPE html>

<html>

<head>

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>FamApp Gateway</title>

<style>

body{
    margin:0;
    padding:15px;
    background:#090909;
    color:#eee;
    font-family:Arial;
}

.container{
    max-width:1200px;
    margin:auto;
}

.card{
    background:#151515;
    border:1px solid #292929;
    border-radius:14px;
    padding:16px;
    margin-bottom:15px;
    overflow:auto;
}

input,button{
    background:#101010;
    color:white;
    border:1px solid #444;
    border-radius:8px;
    padding:10px;
    margin:4px;
}

button{
    cursor:pointer;
}

table{
    width:100%;
    border-collapse:collapse;
}

th,td{
    padding:8px;
    border-bottom:1px solid #292929;
    font-size:12px;
    text-align:left;
    white-space:nowrap;
}

pre{
    white-space:pre-wrap;
    word-break:break-word;
    max-height:500px;
    overflow:auto;
}

.endpoint{
    background:#0d0d0d;
    border:1px solid #292929;
    border-radius:8px;
    padding:10px;
    margin:5px 0;
}

</style>

</head>

<body>

<div class="container">

<h1>💰 FamApp Payment Gateway</h1>

<div class="card">

<b>Gmail:</b> {{ gmail }}

<br>

<b>UPI:</b> {{ upi }}

<br>

<b>Old Scan:</b> {{ days }} days

<br>

<b>Scan Interval:</b> {{ interval }} seconds

</div>


<div class="card">

<h2>⚙️ Scanner</h2>

<button onclick="scanNew()">
Scan Gmail
</button>

<button onclick="scanOld()">
Scan Old Emails
</button>

<br><br>

<input
id="days"
type="number"
min="1"
max="365"
value="{{ days }}"
>

<input
id="interval"
type="number"
min="10"
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
GET /api/scan
</div>

<div class="endpoint">
GET /api/scan-old
</div>

<div class="endpoint">
GET /api/logs
</div>

<div class="endpoint">
GET /api/settings
</div>

<div class="endpoint">
POST /api/settings
</div>

<div class="endpoint">
GET /admin
</div>

</div>


<div class="card">

<h2>💳 Payments</h2>

<table>

<tr>
<th>ID</th>
<th>Amount</th>
<th>Sender</th>
<th>TXN</th>
<th>UTR</th>
<th>Status</th>
<th>Date</th>
</tr>

{% for p in payments %}

<tr>

<td>{{ p.id }}</td>

<td>₹{{ p.amount }}</td>

<td>{{ p.sender }}</td>

<td>{{ p.transaction_id }}</td>

<td>{{ p.utr }}</td>

<td>{{ p.status }}</td>

<td>{{ p.payment_date }}</td>

</tr>

{% endfor %}

</table>

</div>


<div class="card">

<h2>📋 Logs</h2>

<pre>{% for x in logs %}{{ x.log_time }} | {{ x.level }} | {{ x.message }}
{% endfor %}</pre>

</div>


<a href="/admin/logout">
Logout
</a>

</div>


<script>

async function scanNew(){

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


async function scanOld(){

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


async function saveSettings(){

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

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8080"
            )
        ),
        debug=False
    )
