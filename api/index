# ============================================================
# FAMAPP PAYMENT GATEWAY — VERCEL EDITION (NO ORDER ID)
# Verification by UTR + TXN + Amount + Sender only
# ============================================================

import os
import re
import io
import time
import uuid
import json
import base64
import hashlib
import imaplib
import email
import sqlite3
import threading
from datetime import datetime, timedelta
from email.header import decode_header
from urllib.parse import quote

import qrcode
import requests

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    render_template_string
)


# ============================================================
# CONFIG
# ============================================================

GMAIL_EMAIL = os.environ.get(
    "GMAIL_EMAIL",
    "hamza.ali.khan6200@gmail.com"
)

GMAIL_APP_PASSWORD = os.environ.get(
    "GMAIL_APP_PASSWORD",
    "kdnc botx dmpa qeep"
)

UPI_ID = os.environ.get("UPI_ID", "khannhamzaa@fam")
UPI_NAME = os.environ.get("UPI_NAME", "Hamza")


# ------------------------------------------------------------
# ADMIN KEY (query / form / JSON body — no headers)
# ------------------------------------------------------------

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin@key")


# ------------------------------------------------------------
# Session secret for admin cookie
# ------------------------------------------------------------

SECRET = os.environ.get(
    "SECRET",
    hashlib.sha256(
        (ADMIN_KEY + "famapp-salt").encode()
    ).hexdigest()
)


# ------------------------------------------------------------
# Database (Vercel: /tmp is writable)
# ------------------------------------------------------------

DATABASE = "/tmp/payments.db"


# ------------------------------------------------------------
# Server
# ------------------------------------------------------------

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))


# ------------------------------------------------------------
# Gmail scanner
# ------------------------------------------------------------

SCAN_INTERVAL = 20
EMAIL_DAYS = 7


# ============================================================
# OPTIONAL OFFICIAL API
# ============================================================

VERIFY_API_URL = os.environ.get("VERIFY_API_URL", "")
VERIFY_API_KEY = os.environ.get("VERIFY_API_KEY", "")


# ============================================================
# VERIFICATION CONFIG
# ============================================================

UTR_MIN_LENGTH = 12
UTR_MAX_LENGTH = 22

TXN_MIN_LENGTH = 6
TXN_MAX_LENGTH = 64

REQUIRE_UTR = True
REQUIRE_TRANSACTION_ID = True

STRICT_UTR_FORMAT = True
STRICT_TXN_FORMAT = True


# ============================================================
# ADVANCED PROTECTION
# ============================================================

MAX_VERIFY_ATTEMPTS = 20
MAX_SCAN_ATTEMPTS = 30
RATE_WINDOW = 3600
MIN_SCAN_INTERVAL = 15

_rate_store = {}
_scan_lock = threading.Lock()
_last_scan_time = 0


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# RATE LIMITER
# ============================================================

def get_client_ip():
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit(bucket, ip, max_calls, window=RATE_WINDOW):
    now = time.time()
    key = f"{bucket}:{ip}"

    with _scan_lock:
        entries = _rate_store.get(key, [])
        entries = [t for t in entries if now - t < window]

        if len(entries) >= max_calls:
            _rate_store[key] = entries
            return False, 0

        entries.append(now)
        _rate_store[key] = entries
        return True, max_calls - len(entries)


def cleanup_rate_store():
    now = time.time()
    with _scan_lock:
        for key in list(_rate_store.keys()):
            entries = [
                t for t in _rate_store[key]
                if now - t < RATE_WINDOW
            ]
            if entries:
                _rate_store[key] = entries
            else:
                _rate_store.pop(key, None)


# ============================================================
# ADMIN TOKEN (cookie based)
# ============================================================

def make_admin_token():
    expiry = int(time.time()) + 3600
    payload = f"{expiry}"
    sig = hashlib.sha256(
        (payload + SECRET).encode()
    ).hexdigest()[:32]
    raw = f"{payload}.{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_admin_token(token):
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.split(".", 1)
        expected = hashlib.sha256(
            (payload + SECRET).encode()
        ).hexdigest()[:32]
        if sig != expected:
            return False
        if int(payload) < int(time.time()):
            return False
        return True
    except Exception:
        return False


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def setup_database():
    conn = get_db()

    # --------------------------------------------------------
    # Payments table — NO order_id anymore
    # Each row = one unique verified/unmatched payment
    # --------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'VERIFIED',
            sender_name TEXT,
            transaction_id TEXT,
            utr TEXT,
            gmail_uid TEXT UNIQUE,
            payment_date TEXT,
            verification_source TEXT,
            created_at TEXT NOT NULL,
            verified_at TEXT,
            note TEXT DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_utr
        ON payments(utr)
        WHERE utr IS NOT NULL AND utr != ''
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        unique_transaction_id
        ON payments(transaction_id)
        WHERE transaction_id IS NOT NULL AND transaction_id != ''
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL,
            utr TEXT,
            transaction_id TEXT,
            sender_name TEXT,
            gmail_uid TEXT,
            ip TEXT,
            result TEXT,
            reason TEXT,
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT,
            event TEXT,
            detail TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# SECURITY LOG
# ============================================================

def security_log(event, detail=""):
    try:
        ip = get_client_ip()
    except Exception:
        ip = "unknown"

    log(f"SECURITY | {event} | IP={ip} | {detail}")

    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO security_events
                (ip, event, detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                ip, event, detail,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ============================================================
# UPI LINK
# ============================================================

def make_upi_link(amount, note=""):
    link = (
        "upi://pay"
        "?pa=" + quote(UPI_ID)
        + "&pn=" + quote(UPI_NAME)
        + "&am=" + quote(f"{float(amount):.2f}")
        + "&cu=INR"
    )
    if note:
        link += "&tn=" + quote(note)
    return link


# ============================================================
# QR
# ============================================================

def create_qr(data):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image()
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


# ============================================================
# GMAIL
# ============================================================

def connect_gmail():
    if not GMAIL_APP_PASSWORD:
        log("GMAIL | LOGIN=FAILED | password missing")
        return None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        log("GMAIL | LOGIN=SUCCESS")
        return mail
    except Exception as e:
        log(f"GMAIL | LOGIN=FAILED | {e}")
        return None


def decode_text(value):
    if not value:
        return ""
    result = []
    try:
        for part, encoding in decode_header(value):
            if isinstance(part, bytes):
                result.append(
                    part.decode(
                        encoding or "utf-8",
                        errors="ignore"
                    )
                )
            else:
                result.append(str(part))
    except Exception:
        return str(value)
    return "".join(result)


def get_email_body(message):
    text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() not in (
                "text/plain", "text/html"
            ):
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text += " " + payload.decode(
                    charset, errors="ignore"
                )
            except Exception:
                pass
    else:
        try:
            payload = message.get_payload(decode=True)
            if payload:
                text = payload.decode(
                    message.get_content_charset() or "utf-8",
                    errors="ignore"
                )
        except Exception:
            text = str(message.get_payload())

    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_famapp_email(raw_email, gmail_uid):
    try:
        message = email.message_from_bytes(raw_email)
    except Exception as e:
        log(f"EMAIL | PARSE_ERROR | {e}")
        return None

    subject = decode_text(message.get("Subject", ""))
    body = get_email_body(message)
    sender = message.get("From", "")
    text = subject + " " + body
    lower = text.lower()

    if "famapp" not in lower and "fam app" not in lower:
        return None
    if "successfully received" not in lower:
        return None

    amount_match = re.search(
        r"₹\s*([0-9,]+(?:\.[0-9]+)?)",
        text, re.IGNORECASE
    )
    if not amount_match:
        return None
    try:
        amount = round(
            float(amount_match.group(1).replace(",", "")), 2
        )
    except Exception:
        return None

    sender_match = re.search(
        r"successfully\s+received"
        r"\s*₹\s*[0-9,]+(?:\.[0-9]+)?"
        r"\s*from\s*(.*?)"
        r"\s*Transaction\s*ID",
        text, re.IGNORECASE
    )
    sender_name = sender_match.group(1).strip() if sender_match else ""

    tx_match = re.search(
        r"Transaction\s*ID\s*:\s*([A-Za-z0-9_-]+)",
        text, re.IGNORECASE
    )
    transaction_id = tx_match.group(1).strip() if tx_match else ""

    utr_match = re.search(
        r"UTR\s*:\s*([A-Za-z0-9_-]+)",
        text, re.IGNORECASE
    )
    utr = utr_match.group(1).strip() if utr_match else ""

    date_match = re.search(
        r"Date\s*:\s*(.*?)"
        r"\s*(?:Updated\s*Balance|UTR\s*:)",
        text, re.IGNORECASE
    )
    payment_date = date_match.group(1).strip() if date_match else ""

    return {
        "gmail_uid": str(gmail_uid),
        "sender": sender,
        "sender_name": sender_name,
        "amount": amount,
        "transaction_id": transaction_id,
        "utr": utr,
        "payment_date": payment_date
    }


# ============================================================
# UTR + TXN VALIDATION
# ============================================================

def validate_utr_format(utr):
    if not utr:
        return False, "UTR_MISSING"

    utr = utr.strip().upper()

    if not re.fullmatch(r"[A-Z0-9]+", utr):
        return False, "UTR_INVALID_CHARS"

    if not (UTR_MIN_LENGTH <= len(utr) <= UTR_MAX_LENGTH):
        return False, f"UTR_INVALID_LENGTH ({len(utr)})"

    if len(utr) == 12 and utr.isdigit():
        return True, "UTR_VALID_UPI"

    if len(utr) == 22:
        if re.fullmatch(r"[A-Z]{4}[A-Z0-9]{18}", utr):
            return True, "UTR_VALID_IMPS_NEFT"
        return False, "UTR_INVALID_IMPS_FORMAT"

    if 12 <= len(utr) <= 22:
        return True, "UTR_VALID_GENERIC"

    return False, "UTR_INVALID_FORMAT"


def validate_transaction_id(txn_id):
    if not txn_id:
        return False, "TXN_ID_MISSING"

    txn_id = txn_id.strip().upper()

    if not (TXN_MIN_LENGTH <= len(txn_id) <= TXN_MAX_LENGTH):
        return False, f"TXN_ID_INVALID_LENGTH ({len(txn_id)})"

    if not re.fullmatch(r"[A-Z0-9_-]+", txn_id):
        return False, "TXN_ID_INVALID_CHARS"

    return True, "TXN_ID_VALID"


def cross_verify_payment(payment):
    """
    Cross-verify individual payment consistency.
    (No order to match against — checks internal consistency only.)
    """
    try:
        pay_amt = round(float(payment["amount"]), 2)
    except Exception:
        return False, "AMOUNT_PARSE_ERROR"

    if pay_amt <= 0:
        return False, "AMOUNT_INVALID"

    if STRICT_UTR_FORMAT:
        ok, reason = validate_utr_format(payment.get("utr", ""))
        if not ok:
            return False, reason

    if STRICT_TXN_FORMAT:
        ok, reason = validate_transaction_id(
            payment.get("transaction_id", "")
        )
        if not ok:
            return False, reason

    if not payment.get("sender_name"):
        return False, "SENDER_NAME_MISSING"

    if not payment.get("payment_date"):
        return False, "PAYMENT_DATE_MISSING"

    return True, "CROSS_VERIFY_PASSED"


# ============================================================
# REUSE CHECKS
# ============================================================

def check_utr_reuse(utr):
    if not utr:
        return False, None
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM payments WHERE utr=? LIMIT 1",
        (utr,)
    ).fetchone()
    conn.close()
    return (True, row["id"]) if row else (False, None)


def check_txn_id_reuse(txn_id):
    if not txn_id:
        return False, None
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM payments WHERE transaction_id=? LIMIT 1",
        (txn_id,)
    ).fetchone()
    conn.close()
    return (True, row["id"]) if row else (False, None)


# ============================================================
# AUDIT LOG
# ============================================================

def log_verification_attempt(
    payment, result, reason, ip=None
):
    if ip is None:
        try:
            ip = get_client_ip()
        except Exception:
            ip = "system"

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO verification_logs (
                amount, utr, transaction_id,
                sender_name, gmail_uid, ip,
                result, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment.get("amount"),
                payment.get("utr"),
                payment.get("transaction_id"),
                payment.get("sender_name"),
                payment.get("gmail_uid"),
                ip,
                result,
                reason,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
        )
        conn.commit()
    except Exception as e:
        log(f"VERIFY_LOG | ERROR={e}")
    finally:
        conn.close()


# ============================================================
# DUPLICATE CHECK
# ============================================================

def check_duplicate(payment):
    conn = get_db()

    if payment["utr"]:
        row = conn.execute(
            "SELECT id FROM payments WHERE utr=? LIMIT 1",
            (payment["utr"],)
        ).fetchone()
        if row:
            conn.close()
            return True

    if payment["transaction_id"]:
        row = conn.execute(
            "SELECT id FROM payments "
            "WHERE transaction_id=? LIMIT 1",
            (payment["transaction_id"],)
        ).fetchone()
        if row:
            conn.close()
            return True

    row = conn.execute(
        "SELECT id FROM payments WHERE gmail_uid=? LIMIT 1",
        (payment["gmail_uid"],)
    ).fetchone()
    conn.close()

    return bool(row)


# ============================================================
# OFFICIAL API
# ============================================================

def verify_with_official_api(payment):
    if not VERIFY_API_URL:
        return {
            "checked": False,
            "verified": False,
            "status": "API_NOT_CONFIGURED"
        }

    payload = {
        "utr": payment["utr"],
        "transaction_id": payment["transaction_id"],
        "amount": payment["amount"],
        "upi_id": UPI_ID
    }

    headers = {"Content-Type": "application/json"}
    if VERIFY_API_KEY:
        headers["Authorization"] = "Bearer " + VERIFY_API_KEY

    try:
        r = requests.post(
            VERIFY_API_URL,
            json=payload,
            headers=headers,
            timeout=15
        )
        if r.status_code != 200:
            return {
                "checked": True,
                "verified": False,
                "status": "API_HTTP_ERROR"
            }
        data = r.json()
        return {
            "checked": True,
            "verified": bool(data.get("verified", False)),
            "status": str(data.get("status", "UNKNOWN")),
            "data": data
        }
    except Exception as e:
        log(f"VERIFY_API | ERROR={e}")
        return {
            "checked": True,
            "verified": False,
            "status": "API_ERROR"
        }


# ============================================================
# VERIFY PAYMENT (no order matching)
# ============================================================

def verify_payment(payment):

    # --------------------------------------------------------
    # STEP 0 — Mandatory fields
    # --------------------------------------------------------
    if REQUIRE_UTR and not payment.get("utr"):
        log_verification_attempt(
            payment, "REJECTED", "UTR_MISSING"
        )
        return False

    if REQUIRE_TRANSACTION_ID and not payment.get("transaction_id"):
        log_verification_attempt(
            payment, "REJECTED", "TXN_ID_MISSING"
        )
        return False

    # --------------------------------------------------------
    # STEP 1 — Duplicate protection
    # --------------------------------------------------------
    if check_duplicate(payment):
        log_verification_attempt(
            payment, "DUPLICATE", "ALREADY_USED"
        )
        return False

    # --------------------------------------------------------
    # STEP 2 — UTR / TXN reuse guards
    # --------------------------------------------------------
    reused, existing = check_utr_reuse(payment.get("utr"))
    if reused:
        log_verification_attempt(
            payment, "REJECTED", "UTR_REUSED"
        )
        return False

    reused, existing = check_txn_id_reuse(
        payment.get("transaction_id")
    )
    if reused:
        log_verification_attempt(
            payment, "REJECTED", "TXN_ID_REUSED"
        )
        return False

    # --------------------------------------------------------
    # STEP 3 — UTR format
    # --------------------------------------------------------
    ok, reason = validate_utr_format(payment["utr"])
    if not ok:
        log_verification_attempt(
            payment, "REJECTED", reason
        )
        return False

    # --------------------------------------------------------
    # STEP 4 — TXN format
    # --------------------------------------------------------
    ok, reason = validate_transaction_id(
        payment["transaction_id"]
    )
    if not ok:
        log_verification_attempt(
            payment, "REJECTED", reason
        )
        return False

    # --------------------------------------------------------
    # STEP 5 — Internal cross verification
    # --------------------------------------------------------
    passed, reason = cross_verify_payment(payment)
    if not passed:
        log_verification_attempt(
            payment, "REJECTED", reason
        )
        return False

    # --------------------------------------------------------
    # STEP 6 — Optional official API
    # --------------------------------------------------------
    api_result = verify_with_official_api(payment)

    if api_result["checked"]:
        if not api_result["verified"]:
            log_verification_attempt(
                payment, "REJECTED", "API_VERIFICATION_FAILED"
            )
            return False
        verification_source = "GMAIL+UTR+TXN+OFFICIAL_API"
    else:
        verification_source = "GMAIL+UTR+TXN"

    # --------------------------------------------------------
    # STEP 7 — Save to DB
    # --------------------------------------------------------
    conn = get_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn.execute(
            """
            INSERT INTO payments (
                amount, status, sender_name,
                transaction_id, utr, gmail_uid,
                payment_date, verification_source,
                created_at, verified_at, note
            ) VALUES (?, 'VERIFIED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment["amount"],
                payment["sender_name"],
                payment["transaction_id"],
                payment["utr"],
                payment["gmail_uid"],
                payment["payment_date"],
                verification_source,
                now_str,
                now_str,
                f"Auto verified ({reason})"
            )
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        log_verification_attempt(
            payment, "REJECTED", "INTEGRITY_ERROR"
        )
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log(
        "PAYMENT | VERIFIED | "
        f"AMOUNT=₹{payment['amount']:.2f} | "
        f"UTR={payment['utr']} | "
        f"TXN={payment['transaction_id']} | "
        f"SENDER={payment['sender_name']}"
    )

    log_verification_attempt(
        payment, "VERIFIED", reason
    )

    return True


# ============================================================
# GMAIL SCANNER
# ============================================================

def scan_gmail():
    global _last_scan_time

    with _scan_lock:
        now = time.time()
        if now - _last_scan_time < MIN_SCAN_INTERVAL:
            log("SCANNER | SKIPPED (cooldown)")
            return
        _last_scan_time = now

    log("SCANNER | START")
    mail = connect_gmail()
    if not mail:
        return

    try:
        mail.select("INBOX", readonly=True)

        since_date = (
            datetime.now() - timedelta(days=EMAIL_DAYS)
        ).strftime("%d-%b-%Y")

        status, data = mail.uid(
            "search", None, f"SINCE {since_date}"
        )
        if status != "OK":
            mail.logout()
            return

        uids = data[0].split()

        famapp_found = 0
        verified = 0

        for uid in reversed(uids):
            try:
                status, message_data = mail.uid(
                    "fetch", uid, "(RFC822)"
                )
                if status != "OK":
                    continue

                raw_email = None
                for item in message_data:
                    if isinstance(item, tuple):
                        raw_email = item[1]
                        break
                if not raw_email:
                    continue

                uid_text = (
                    uid.decode()
                    if isinstance(uid, bytes)
                    else str(uid)
                )

                payment = parse_famapp_email(
                    raw_email, uid_text
                )
                if not payment:
                    continue

                famapp_found += 1

                if verify_payment(payment):
                    verified += 1

            except Exception as e:
                log(f"SCANNER | EMAIL_ERROR={e}")

        log(
            f"SCANNER | DONE | FOUND={famapp_found} | "
            f"VERIFIED={verified}"
        )

        try:
            mail.logout()
        except Exception:
            pass

    except Exception as e:
        log(f"SCANNER | ERROR={e}")
        try:
            mail.logout()
        except Exception:
            pass


# ============================================================
# BACKGROUND SCANNER (for local use)
# ============================================================

def background_scanner():
    log("BACKGROUND_SCANNER | STARTED")
    while True:
        try:
            scan_gmail()
            cleanup_rate_store()
        except Exception as e:
            log(f"BACKGROUND_SCANNER | ERROR={e}")
        time.sleep(SCAN_INTERVAL)


# ============================================================
# INIT
# ============================================================

try:
    setup_database()
except Exception as e:
    log(f"INIT | DB_SETUP_ERROR={e}")


# ============================================================
# QUICK QR (no order id)
# ============================================================

@app.route("/api/qr")
def quick_qr():
    try:
        amount = round(
            float(request.args.get("amount", 0)), 2
        )
    except Exception:
        return jsonify({
            "success": False,
            "error": "Invalid amount"
        }), 400

    if amount <= 0 or amount > 1000000:
        return jsonify({
            "success": False,
            "error": "Amount must be between 0 and 1,000,000"
        }), 400

    note = (request.args.get("note") or "").strip()[:50]

    upi_link = make_upi_link(amount, note)
    return send_file(create_qr(upi_link), mimetype="image/png")


# ============================================================
# UPI LINK (JSON)
# ============================================================

@app.route("/api/upi-link")
def upi_link_api():
    try:
        amount = round(
            float(request.args.get("amount", 0)), 2
        )
    except Exception:
        return jsonify({
            "success": False,
            "error": "Invalid amount"
        }), 400

    if amount <= 0:
        return jsonify({
            "success": False,
            "error": "Amount must be greater than zero"
        }), 400

    note = (request.args.get("note") or "").strip()[:50]
    link = make_upi_link(amount, note)

    return jsonify({
        "success": True,
        "amount": amount,
        "upi_id": UPI_ID,
        "upi_name": UPI_NAME,
        "note": note,
        "upi_link": link,
        "qr_url": f"/api/qr?amount={amount}"
                  + (f"&note={quote(note)}" if note else "")
    })


# ============================================================
# PAYMENTS LIST (admin protected)
# ============================================================

@app.route("/api/payments")
def payments_api():
    provided_key = (
        request.args.get("admin_key") or ""
    ).strip()

    if provided_key != ADMIN_KEY:
        security_log("UNAUTHORIZED_PAYMENTS_API")
        return jsonify({
            "success": False,
            "error": "Invalid or missing admin_key"
        }), 401

    limit = min(
        int(request.args.get("limit", 500)),
        2000
    )

    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM payments
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()
    conn.close()

    return jsonify({
        "success": True,
        "count": len(rows),
        "payments": [dict(r) for r in rows]
    })


# ============================================================
# SEARCH PAYMENT BY UTR / TXN
# ============================================================

@app.route("/api/verify-utr")
def verify_utr_lookup():
    utr = (request.args.get("utr") or "").strip().upper()

    if not utr:
        return jsonify({
            "success": False,
            "error": "utr query param required"
        }), 400

    ok, reason = validate_utr_format(utr)
    if not ok:
        return jsonify({
            "success": False,
            "valid_format": False,
            "reason": reason
        })

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM payments WHERE utr=? LIMIT 1",
        (utr,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({
            "success": True,
            "found": False,
            "utr": utr,
            "valid_format": True,
            "reason": reason
        })

    return jsonify({
        "success": True,
        "found": True,
        "utr": utr,
        "valid_format": True,
        "reason": reason,
        "payment": dict(row)
    })


@app.route("/api/verify-txn")
def verify_txn_lookup():
    txn = (request.args.get("txn") or "").strip().upper()

    if not txn:
        return jsonify({
            "success": False,
            "error": "txn query param required"
        }), 400

    ok, reason = validate_transaction_id(txn)
    if not ok:
        return jsonify({
            "success": False,
            "valid_format": False,
            "reason": reason
        })

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM payments WHERE transaction_id=? LIMIT 1",
        (txn,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({
            "success": True,
            "found": False,
            "transaction_id": txn,
            "valid_format": True,
            "reason": reason
        })

    return jsonify({
        "success": True,
        "found": True,
        "transaction_id": txn,
        "valid_format": True,
        "reason": reason,
        "payment": dict(row)
    })


# ============================================================
# MANUAL ADD PAYMENT (admin)
# ============================================================

@app.route("/api/manual-add", methods=["GET", "POST"])
def manual_add():
    data = {}
    data.update(request.args.to_dict())
    data.update(request.form.to_dict())
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        data.update(j)

    provided_key = (data.get("admin_key") or "").strip()
    if provided_key != ADMIN_KEY:
        security_log("UNAUTHORIZED_MANUAL_ADD")
        return jsonify({
            "success": False,
            "error": "Invalid or missing admin_key"
        }), 401

    ip = get_client_ip()
    allowed, _ = rate_limit(
        "manual-add", ip, MAX_VERIFY_ATTEMPTS
    )
    if not allowed:
        security_log("RATE_LIMIT_MANUAL_ADD")
        return jsonify({
            "success": False,
            "error": "Too many requests"
        }), 429

    try:
        amount = round(float(data.get("amount", 0)), 2)
    except Exception:
        return jsonify({
            "success": False,
            "error": "Invalid amount"
        }), 400

    utr = (data.get("utr") or "").strip().upper()
    txn = (data.get("transaction_id") or "").strip().upper()
    sender = (data.get("sender_name") or "").strip()
    pdate = (data.get("payment_date") or "").strip()

    if not utr or not txn:
        return jsonify({
            "success": False,
            "error": "utr and transaction_id are required"
        }), 400

    ok, reason = validate_utr_format(utr)
    if not ok:
        return jsonify({
            "success": False,
            "error": f"Invalid UTR: {reason}"
        }), 400

    ok, reason = validate_transaction_id(txn)
    if not ok:
        return jsonify({
            "success": False,
            "error": f"Invalid TXN: {reason}"
        }), 400

    reused, _ = check_utr_reuse(utr)
    if reused:
        return jsonify({
            "success": False,
            "error": "UTR already used"
        }), 409

    reused, _ = check_txn_id_reuse(txn)
    if reused:
        return jsonify({
            "success": False,
            "error": "Transaction ID already used"
        }), 409

    conn = get_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            """
            INSERT INTO payments (
                amount, status, sender_name,
                transaction_id, utr, gmail_uid,
                payment_date, verification_source,
                created_at, verified_at, note
            ) VALUES (?, 'VERIFIED', ?, ?, ?, NULL, ?, 'MANUAL_ADMIN', ?, ?, ?)
            """,
            (
                amount, sender, txn, utr,
                pdate, now_str, now_str,
                "Manually added by admin"
            )
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        conn.close()
        return jsonify({
            "success": False,
            "error": f"Integrity error: {e}"
        }), 409
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log(
        f"MANUAL_ADD | AMOUNT=₹{amount} | "
        f"UTR={utr} | TXN={txn}"
    )

    log_verification_attempt(
        {
            "amount": amount,
            "utr": utr,
            "transaction_id": txn,
            "sender_name": sender,
            "gmail_uid": None
        },
        "VERIFIED",
        "MANUAL_ADMIN"
    )

    return jsonify({
        "success": True,
        "amount": amount,
        "utr": utr,
        "transaction_id": txn,
        "status": "VERIFIED"
    })


# ============================================================
# VERIFICATION LOGS (admin)
# ============================================================

@app.route("/api/verification-logs")
def verification_logs():
    provided_key = (
        request.args.get("admin_key") or ""
    ).strip()

    if provided_key != ADMIN_KEY:
        security_log("UNAUTHORIZED_LOGS_API")
        return jsonify({
            "success": False,
            "error": "Invalid or missing admin_key"
        }), 401

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM verification_logs
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
        return jsonify({
            "success": True,
            "count": len(rows),
            "logs": [dict(r) for r in rows]
        })
    except Exception:
        return jsonify({
            "success": True,
            "count": 0,
            "logs": []
        })
    finally:
        conn.close()


# ============================================================
# SECURITY EVENTS (admin)
# ============================================================

@app.route("/api/security-events")
def security_events_api():
    provided_key = (
        request.args.get("admin_key") or ""
    ).strip()

    if provided_key != ADMIN_KEY:
        security_log("UNAUTHORIZED_SECURITY_API")
        return jsonify({
            "success": False,
            "error": "Invalid or missing admin_key"
        }), 401

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT * FROM security_events
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
        return jsonify({
            "success": True,
            "count": len(rows),
            "events": [dict(r) for r in rows]
        })
    except Exception:
        return jsonify({
            "success": True,
            "count": 0,
            "events": []
        })
    finally:
        conn.close()


# ============================================================
# MANUAL SCAN
# ============================================================

@app.route("/api/scan", methods=["GET", "POST"])
def manual_scan():
    data = {}
    data.update(request.args.to_dict())
    data.update(request.form.to_dict())
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        data.update(j)

    provided_key = (data.get("admin_key") or "").strip()
    if provided_key != ADMIN_KEY:
        security_log("UNAUTHORIZED_SCAN")
        return jsonify({
            "success": False,
            "error": "Invalid or missing admin_key"
        }), 401

    ip = get_client_ip()
    allowed, _ = rate_limit(
        "scan", ip, MAX_SCAN_ATTEMPTS
    )
    if not allowed:
        security_log("RATE_LIMIT_SCAN")
        return jsonify({
            "success": False,
            "error": "Too many scans. Try later."
        }), 429

    scan_gmail()

    return jsonify({
        "success": True,
        "message": "Gmail scan completed",
        "range": f"Last {EMAIL_DAYS} days"
    })


# ============================================================
# ADMIN PANEL (protected via ?admin_key=)
# ============================================================

@app.route("/admin")
def admin():
    provided_key = (
        request.args.get("admin_key") or ""
    ).strip()

    # --------------------------------------------------------
    # Login page if no / wrong key
    # --------------------------------------------------------
    if provided_key != ADMIN_KEY:
        if provided_key:
            security_log("ADMIN_BAD_KEY")

        return render_template_string("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login — FamApp</title>
<style>
body {
    background:#0d0d0d; color:#eee;
    font-family:-apple-system,Arial,sans-serif;
    display:flex; align-items:center; justify-content:center;
    min-height:100vh; margin:0;
}
.box {
    background:#161616; padding:32px;
    border-radius:14px; border:1px solid #2a2a2a;
    width:100%; max-width:340px;
}
h2 { margin:0 0 20px; }
input {
    width:100%; padding:12px;
    background:#0d0d0d; color:#eee;
    border:1px solid #333; border-radius:8px;
    font-size:15px; box-sizing:border-box;
}
button {
    width:100%; margin-top:14px;
    padding:12px; background:#2d6cdf;
    color:#fff; border:0; border-radius:8px;
    font-weight:600; font-size:15px; cursor:pointer;
}
button:hover { background:#3a7cf0; }
.err { color:#ff6b6b; margin-top:12px; font-size:14px; }
</style>
</head>
<body>
<form class="box" method="GET" action="/admin">
    <h2>🔒 Admin Login</h2>
    <input type="password" name="admin_key"
           placeholder="Enter admin key" autofocus required>
    <button type="submit">Unlock</button>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
</form>
</body>
</html>
""", error="Invalid key" if provided_key else "")

    # --------------------------------------------------------
    # Dashboard
    # --------------------------------------------------------
    conn = get_db()

    payments = conn.execute(
        "SELECT * FROM payments ORDER BY id DESC LIMIT 300"
    ).fetchall()

    try:
        logs = conn.execute(
            """
            SELECT * FROM verification_logs
            ORDER BY id DESC LIMIT 100
            """
        ).fetchall()
    except Exception:
        logs = []

    try:
        events = conn.execute(
            """
            SELECT * FROM security_events
            ORDER BY id DESC LIMIT 100
            """
        ).fetchall()
    except Exception:
        events = []

    conn.close()

    total = len(payments)
    total_amount = sum(p["amount"] for p in payments)
    verified = sum(1 for p in payments if p["status"] == "VERIFIED")

    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FamApp Admin</title>
<style>
* { box-sizing:border-box; }
body {
    background:#0d0d0d; color:#eee; margin:0;
    font-family:-apple-system,Arial,sans-serif;
    padding:20px;
}
h1 { margin:0 0 6px; }
h2 { margin:32px 0 12px; font-size:16px; color:#aaa;
     text-transform:uppercase; letter-spacing:1px; }
.sub { color:#888; font-size:13px; margin-bottom:22px; }
.stats { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:22px; }
.card {
    background:#161616; border:1px solid #2a2a2a;
    border-radius:12px; padding:16px 20px;
    flex:1; min-width:150px;
}
.card .lbl { font-size:11px; color:#888;
             text-transform:uppercase; letter-spacing:1px; }
.card b { display:block; font-size:26px; margin-top:6px; }
.green b { color:#42e695; }
.yellow b { color:#ffd166; }
.red b { color:#ff6b6b; }
.blue b { color:#5eb0ff; }
table {
    width:100%; border-collapse:collapse;
    background:#161616; border-radius:10px;
    overflow:hidden; font-size:13px;
}
th,td {
    padding:10px 12px; border-bottom:1px solid #262626;
    text-align:left; vertical-align:top;
}
th {
    background:#1f1f1f; color:#aaa; font-size:11px;
    text-transform:uppercase; letter-spacing:0.5px;
}
tr:last-child td { border-bottom:none; }
tr:hover td { background:#1c1c1c; }
.mono { font-family:'Courier New',monospace; font-size:12px; }
.tag {
    display:inline-block; padding:2px 8px; border-radius:6px;
    font-size:10px; font-weight:600;
}
.tag.ok { background:#0f3a23; color:#42e695; }
.tag.bad { background:#3a1010; color:#ff6b6b; }
.tag.warn { background:#3a2e0f; color:#ffd166; }
button {
    padding:10px 16px; border:0; border-radius:8px;
    background:#2d6cdf; color:#fff; font-weight:600;
    cursor:pointer; font-size:14px;
}
button:hover { background:#3a7cf0; }
.topbar {
    display:flex; justify-content:space-between;
    align-items:center; flex-wrap:wrap; gap:12px;
}
.tabs { margin:22px 0 0; display:flex; gap:8px; flex-wrap:wrap; }
.tab {
    padding:8px 16px; background:#161616;
    border:1px solid #2a2a2a; border-radius:8px;
    color:#aaa; cursor:pointer; font-size:13px;
}
.tab.active { background:#2d6cdf; color:#fff; border-color:#2d6cdf; }
.panel { display:none; }
.panel.active { display:block; }
@media(max-width:800px){
    table { display:block; overflow-x:auto; white-space:nowrap; }
    .stats { flex-direction:column; }
}
</style>
</head>
<body>

<div class="topbar">
    <div>
        <h1>💰 FamApp Admin Panel</h1>
        <div class="sub">
            UPI: <b>{{ upi }}</b> ·
            Key: <span class="mono">{{ key_masked }}</span>
        </div>
    </div>
    <button onclick="scan()">🔄 Scan Gmail</button>
</div>

<div class="stats">
    <div class="card green">
        <div class="lbl">Verified Payments</div>
        <b>{{ verified }}</b>
    </div>
    <div class="card blue">
        <div class="lbl">Total Received</div>
        <b>₹{{ "%.2f"|format(total_amount) }}</b>
    </div>
    <div class="card">
        <div class="lbl">Total Records</div>
        <b>{{ total }}</b>
    </div>
</div>

<div class="tabs">
    <div class="tab active" onclick="showTab('payments',this)">💳 Payments</div>
    <div class="tab" onclick="showTab('logs',this)">🛡️ Verification Logs</div>
    <div class="tab" onclick="showTab('security',this)">🚨 Security Events</div>
    <div class="tab" onclick="showTab('add',this)">➕ Manual Add</div>
</div>

<!-- ================= PAYMENTS ================= -->
<div id="payments" class="panel active">
<h2>Payments ({{ total }})</h2>
<table>
<tr>
    <th>ID</th><th>Amount</th><th>Status</th>
    <th>Sender</th><th>Transaction ID</th><th>UTR</th>
    <th>Payment Date</th><th>Source</th>
    <th>Created</th><th>Verified</th>
</tr>
{% for p in payments %}
<tr>
    <td class="mono">{{ p["id"] }}</td>
    <td>₹{{ "%.2f"|format(p["amount"]) }}</td>
    <td>
        {% if p["status"] == "VERIFIED" %}
            <span class="tag ok">VERIFIED</span>
        {% else %}
            <span class="tag warn">{{ p["status"] }}</span>
        {% endif %}
    </td>
    <td>{{ p["sender_name"] or "-" }}</td>
    <td class="mono">{{ p["transaction_id"] or "-" }}</td>
    <td class="mono">{{ p["utr"] or "-" }}</td>
    <td>{{ p["payment_date"] or "-" }}</td>
    <td class="mono">{{ p["verification_source"] or "-" }}</td>
    <td class="mono">{{ p["created_at"] }}</td>
    <td class="mono">{{ p["verified_at"] or "-" }}</td>
</tr>
{% endfor %}
</table>
</div>

<!-- ================= LOGS ================= -->
<div id="logs" class="panel">
<h2>Verification Logs (latest 100)</h2>
<table>
<tr>
    <th>Time</th><th>Amount</th><th>Sender</th>
    <th>UTR</th><th>TXN</th><th>Result</th>
    <th>Reason</th><th>IP</th>
</tr>
{% for l in logs %}
<tr>
    <td class="mono">{{ l["created_at"] }}</td>
    <td>₹{{ "%.2f"|format(l["amount"] or 0) }}</td>
    <td>{{ l["sender_name"] or "-" }}</td>
    <td class="mono">{{ l["utr"] or "-" }}</td>
    <td class="mono">{{ l["transaction_id"] or "-" }}</td>
    <td>
        {% if l["result"] == "VERIFIED" %}
            <span class="tag ok">VERIFIED</span>
        {% elif l["result"] == "DUPLICATE" %}
            <span class="tag warn">DUPLICATE</span>
        {% else %}
            <span class="tag bad">{{ l["result"] }}</span>
        {% endif %}
    </td>
    <td class="mono">{{ l["reason"] or "-" }}</td>
    <td class="mono">{{ l["ip"] or "-" }}</td>
</tr>
{% endfor %}
</table>
</div>

<!-- ================= SECURITY ================= -->
<div id="security" class="panel">
<h2>Security Events (latest 100)</h2>
<table>
<tr>
    <th>Time</th><th>IP</th><th>Event</th><th>Detail</th>
</tr>
{% for e in events %}
<tr>
    <td class="mono">{{ e["created_at"] }}</td>
    <td class="mono">{{ e["ip"] or "-" }}</td>
    <td><span class="tag bad">{{ e["event"] }}</span></td>
    <td class="mono">{{ e["detail"] or "-" }}</td>
</tr>
{% endfor %}
</table>
</div>

<!-- ================= MANUAL ADD ================= -->
<div id="add" class="panel">
<h2>Manually Add Payment</h2>
<table>
<tr><th style="width:180px">Field</th><th>Value</th></tr>
<tr><td>Amount</td>
    <td><input id="m_amount" type="number" step="0.01" placeholder="100"></td></tr>
<tr><td>UTR</td>
    <td><input id="m_utr" placeholder="123456789012"></td></tr>
<tr><td>Transaction ID</td>
    <td><input id="m_txn" placeholder="TXN123456"></td></tr>
<tr><td>Sender Name</td>
    <td><input id="m_sender" placeholder="Hamza"></td></tr>
<tr><td>Payment Date</td>
    <td><input id="m_date" placeholder="2026-01-01 12:00"></td></tr>
<tr><td></td>
    <td><button onclick="manualAdd()">➕ Add Payment</button></td></tr>
</table>
<div id="add_result" style="margin-top:14px" class="mono"></div>
</div>

<script>
const ADMIN_KEY = {{ key_json|safe }};

function showTab(name, el) {
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.getElementById(name).classList.add("active");
    el.classList.add("active");
}

async function scan() {
    try {
        const r = await fetch("/api/scan?admin_key=" + encodeURIComponent(ADMIN_KEY));
        const d = await r.json();
        alert(d.message || d.error || "Done");
        location.reload();
    } catch(e) {
        alert("Scan failed");
    }
}

async function manualAdd() {
    const body = {
        admin_key: ADMIN_KEY,
        amount: document.getElementById("m_amount").value,
        utr: document.getElementById("m_utr").value,
        transaction_id: document.getElementById("m_txn").value,
        sender_name: document.getElementById("m_sender").value,
        payment_date: document.getElementById("m_date").value
    };
    try {
        const r = await fetch("/api/manual-add", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body)
        });
        const d = await r.json();
        document.getElementById("add_result").innerText =
            d.success ? "✅ Added successfully" : "❌ " + d.error;
        if (d.success) setTimeout(() => location.reload(), 800);
    } catch(e) {
        document.getElementById("add_result").innerText = "❌ Request failed";
    }
}
</script>

</body>
</html>
"""

    key_masked = ADMIN_KEY[:2] + "*" * (len(ADMIN_KEY) - 4) + ADMIN_KEY[-2:] \
        if len(ADMIN_KEY) > 4 else "****"

    return render_template_string(
        html,
        payments=payments,
        logs=logs,
        events=events,
        total=total,
        verified=verified,
        total_amount=total_amount,
        upi=UPI_ID,
        key_masked=key_masked,
        key_json=json.dumps(ADMIN_KEY)
    )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "name": "FamApp Payment Gateway",
        "status": "online",
        "upi_id": UPI_ID,
        "upi_name": UPI_NAME,
        "gmail": GMAIL_EMAIL,
        "email_range": f"Last {EMAIL_DAYS} days",
        "order_id_system": "DISABLED",
        "utr_verification": True,
        "txn_verification": True,
        "endpoints": {
            "quick_qr": "/api/qr?amount=100",
            "upi_link": "/api/upi-link?amount=100&note=Test",
            "verify_utr": "/api/verify-utr?utr=123456789012",
            "verify_txn": "/api/verify-txn?txn=TXN123456",
            "payments": "/api/payments?admin_key=admin@key",
            "verification_logs": "/api/verification-logs?admin_key=admin@key",
            "security_events": "/api/security-events?admin_key=admin@key",
            "manual_add": "POST /api/manual-add",
            "scan": "/api/scan?admin_key=admin@key",
            "admin_panel": "/admin?admin_key=admin@key"
        }
    })


# ============================================================
# VERCEL HANDLER
# ============================================================

if __name__ == "__main__":
    log("==========================================")
    log("FAMAPP GATEWAY — LOCAL DEV MODE")
    log(f"UPI={UPI_ID}")
    log(f"GMAIL={GMAIL_EMAIL}")
    log("ORDER_ID_SYSTEM=DISABLED")
    log("==========================================")

    # Local run: start background scanner thread
    t = threading.Thread(target=background_scanner, daemon=True)
    t.start()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True
    )
