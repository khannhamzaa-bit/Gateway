# ============================================================
# FAMAPP PAYMENT GATEWAY — VERCEL EDITION (v5)
# Purpose = Order ID (single field)
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
    render_template_string,
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

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin@key")

SECRET = os.environ.get(
    "SECRET",
    hashlib.sha256((ADMIN_KEY + "famapp-salt").encode()).hexdigest()
)

DATABASE = "/tmp/payments.db"

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))

SCAN_INTERVAL = 20
EMAIL_DAYS = 7
PAYMENT_EXPIRY_MINUTES = 15


# ============================================================
# VERIFICATION CONFIG
# ============================================================

VERIFY_API_URL = os.environ.get("VERIFY_API_URL", "")
VERIFY_API_KEY = os.environ.get("VERIFY_API_KEY", "")

UTR_MIN_LENGTH = 12
UTR_MAX_LENGTH = 22
TXN_MIN_LENGTH = 6
TXN_MAX_LENGTH = 64

STRICT_UTR_FORMAT = True
STRICT_TXN_FORMAT = True


# ============================================================
# PROTECTION
# ============================================================

MAX_SCAN_ATTEMPTS = 60
RATE_WINDOW = 3600
MIN_SCAN_INTERVAL = 10

_rate_store = {}
_scan_lock = threading.Lock()
_last_scan_time = 0


# ============================================================
# QR CACHE
# ============================================================

_qr_cache = {}
_qr_cache_lock = threading.Lock()
QR_CACHE_MAX = 200


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ============================================================
# HELPERS
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
                t for t in _rate_store[key] if now - t < RATE_WINDOW
            ]
            if entries:
                _rate_store[key] = entries
            else:
                _rate_store.pop(key, None)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE, timeout=30, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def setup_database():
    conn = get_db()

    # --------------------------------------------------------
    # orders — order_id == purpose (single field)
    # --------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'PENDING',
            utr TEXT,
            transaction_id TEXT,
            sender_name TEXT,
            payment_date TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            verified_at TEXT,
            note TEXT DEFAULT ''
        )
    """)

    # --------------------------------------------------------
    # payments — verified transactions
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
            order_id TEXT,
            verification_source TEXT,
            created_at TEXT NOT NULL,
            verified_at TEXT,
            note TEXT DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS unique_utr
        ON payments(utr) WHERE utr IS NOT NULL AND utr != ''
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS unique_transaction_id
        ON payments(transaction_id)
        WHERE transaction_id IS NOT NULL AND transaction_id != ''
    """)

    # --------------------------------------------------------
    # verification log
    # --------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
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

    # --------------------------------------------------------
    # security events
    # --------------------------------------------------------
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
            INSERT INTO security_events (ip, event, detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (ip, event, detail,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ============================================================
# ORDER ID (= Purpose)
# ============================================================

def generate_order_id(hint=""):
    """
    Generate the order ID. This IS the purpose.

    If hint is provided (e.g. "TG123456789"),
    the order_id becomes:  TG123456789-ORD260922ABCD

    Otherwise:             ORD260922ABCD
    """
    base = (
        "ORD"
        + datetime.now().strftime("%y%m%d%H%M%S")
        + uuid.uuid4().hex[:4].upper()
    )
    hint = (hint or "").strip()[:20]
    if hint:
        # Sanitize hint to be UPI-note-safe
        hint = re.sub(r"[^A-Za-z0-9]", "", hint)
        if hint:
            return f"{hint}-{base}"
    return base


# ============================================================
# UPI LINK
# ============================================================

def make_upi_link(amount, order_id):
    return (
        "upi://pay"
        "?pa=" + quote(UPI_ID)
        + "&pn=" + quote(UPI_NAME)
        + "&am=" + quote(f"{float(amount):.2f}")
        + "&cu=INR"
        + "&tn=" + quote(order_id)
    )


# ============================================================
# FAST QR
# ============================================================

def _make_qr_key(data):
    return hashlib.md5(data.encode()).hexdigest()


def get_qr_bytes(data):
    key = _make_qr_key(data)
    with _qr_cache_lock:
        if key in _qr_cache:
            return _qr_cache[key]

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    data_bytes = buf.getvalue()

    with _qr_cache_lock:
        if len(_qr_cache) >= QR_CACHE_MAX:
            _qr_cache.pop(next(iter(_qr_cache)))
        _qr_cache[key] = data_bytes

    return data_bytes


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
        for part, enc in decode_header(value):
            if isinstance(part, bytes):
                result.append(
                    part.decode(enc or "utf-8", errors="ignore")
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
                    errors="ignore",
                )
        except Exception:
            text = str(message.get_payload())
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def parse_famapp_email(raw_email, gmail_uid):
    """
    Parse FamApp email.

    Sample:
        Transaction ID : FMPIB6649064781
        Date : 02:37 PM IST, 22 September 2026
        Updated Balance : ₹586.88
        UTR : 841140558066
        Purpose : TG8896221941-ORD260922ABCD
    """
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

    # Amount
    am = re.search(r"₹\s*([0-9,]+(?:\.[0-9]+)?)", text)
    if not am:
        return None
    try:
        amount = round(float(am.group(1).replace(",", "")), 2)
    except Exception:
        return None

    # Sender
    sender_name = ""
    sm = re.search(
        r"successfully\s+received"
        r"\s*₹\s*[0-9,]+(?:\.[0-9]+)?"
        r"\s*from\s*(.*?)"
        r"\s*Transaction\s*ID",
        text, re.IGNORECASE,
    )
    if sm:
        sender_name = sm.group(1).strip()

    # Transaction ID
    txn = ""
    tm = re.search(
        r"Transaction\s*ID\s*:\s*([A-Za-z0-9_-]+)",
        text, re.IGNORECASE,
    )
    if tm:
        txn = tm.group(1).strip()

    # UTR
    utr = ""
    um = re.search(
        r"UTR\s*:\s*([A-Za-z0-9_-]+)",
        text, re.IGNORECASE,
    )
    if um:
        utr = um.group(1).strip()

    # Purpose (= order_id)
    order_id = ""
    pm = re.search(
        r"Purpose\s*:\s*([A-Za-z0-9_\-]+)",
        text, re.IGNORECASE,
    )
    if pm:
        order_id = pm.group(1).strip()

    # Date
    payment_date = ""
    dm = re.search(
        r"Date\s*:\s*(.*?)"
        r"\s*(?:Updated\s*Balance|UTR\s*:)",
        text, re.IGNORECASE,
    )
    if dm:
        payment_date = dm.group(1).strip()

    return {
        "gmail_uid": str(gmail_uid),
        "sender": sender,
        "sender_name": sender_name,
        "amount": amount,
        "transaction_id": txn,
        "utr": utr,
        "order_id": order_id,
        "payment_date": payment_date,
    }


# ============================================================
# DATE PARSER
# ============================================================

DATE_FORMATS = (
    "%I:%M %p IST, %d %B %Y",
    "%I:%M %p IST, %d %b %Y",
    "%I:%M %p IST, %d-%m-%Y",
    "%I:%M %p IST, %d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d %b %Y %I:%M %p",
    "%d %b %Y, %I:%M %p",
    "%d %B %Y %I:%M %p",
    "%d %B %Y, %I:%M %p",
)


def parse_payment_date(raw):
    if not raw:
        return None
    raw = re.sub(r"\s+", " ", raw.strip())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def now_ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


# ============================================================
# VALIDATION
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


# ============================================================
# AUDIT LOG
# ============================================================

def log_verification(payment, order_id, result, reason, ip=None):
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
                order_id, amount, utr, transaction_id,
                sender_name, gmail_uid, ip, result, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                payment.get("amount"),
                payment.get("utr"),
                payment.get("transaction_id"),
                payment.get("sender_name"),
                payment.get("gmail_uid"),
                ip,
                result,
                reason,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    except Exception as e:
        log(f"VERIFY_LOG | ERROR={e}")
    finally:
        conn.close()


# ============================================================
# OFFICIAL API (optional)
# ============================================================

def verify_with_official_api(payment):
    if not VERIFY_API_URL:
        return {"checked": False, "verified": False,
                "status": "API_NOT_CONFIGURED"}
    payload = {
        "utr": payment["utr"],
        "transaction_id": payment["transaction_id"],
        "amount": payment["amount"],
        "upi_id": UPI_ID,
    }
    headers = {"Content-Type": "application/json"}
    if VERIFY_API_KEY:
        headers["Authorization"] = "Bearer " + VERIFY_API_KEY
    try:
        r = requests.post(
            VERIFY_API_URL, json=payload, headers=headers, timeout=15
        )
        if r.status_code != 200:
            return {"checked": True, "verified": False,
                    "status": "API_HTTP_ERROR"}
        data = r.json()
        return {
            "checked": True,
            "verified": bool(data.get("verified", False)),
            "status": str(data.get("status", "UNKNOWN")),
            "data": data,
        }
    except Exception as e:
        log(f"VERIFY_API | ERROR={e}")
        return {"checked": True, "verified": False,
                "status": "API_ERROR"}


# ============================================================
# ORDER LOOKUP
# ============================================================

def get_order_by_id(order_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id=? LIMIT 1",
        (order_id,),
    ).fetchone()
    conn.close()
    return row


# ============================================================
# CREATE ORDER
# ============================================================

def create_order(amount, hint=""):
    order_id = generate_order_id(hint)

    now = datetime.now()
    expires = now + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

    conn = get_db()
    conn.execute(
        """
        INSERT INTO orders (
            order_id, amount, status,
            created_at, expires_at
        ) VALUES (?, ?, 'PENDING', ?, ?)
        """,
        (
            order_id,
            amount,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            expires.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()

    return {
        "order_id": order_id,
        "amount": amount,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
# SAVE UNMATCHED
# ============================================================

def save_unmatched(payment):
    conn = get_db()
    if payment["utr"]:
        existing = conn.execute(
            "SELECT id FROM payments WHERE utr=?",
            (payment["utr"],),
        ).fetchone()
        if existing:
            conn.close()
            return
    try:
        conn.execute(
            """
            INSERT INTO payments (
                amount, status, sender_name, transaction_id, utr,
                gmail_uid, payment_date, order_id,
                verification_source, created_at, note
            ) VALUES (?, 'UNMATCHED', ?, ?, ?, ?, ?, ?, 'GMAIL', ?, ?)
            """,
            (
                payment["amount"],
                payment["sender_name"],
                payment["transaction_id"],
                payment["utr"],
                payment["gmail_uid"],
                payment["payment_date"],
                payment.get("order_id", ""),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "FamApp payment with no matching order",
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ============================================================
# VERIFY PAYMENT (Order ID + 15-min expiry)
# ============================================================

def verify_payment(payment):
    """
    Match incoming FamApp email to an order via the
    Purpose field (= order_id).
    """

    # ---- Mandatory ----
    if not payment.get("utr"):
        log_verification(payment, None, "REJECTED", "UTR_MISSING")
        return False
    if not payment.get("transaction_id"):
        log_verification(payment, None, "REJECTED", "TXN_ID_MISSING")
        return False

    # ---- Format ----
    ok, reason = validate_utr_format(payment["utr"])
    if not ok:
        log_verification(payment, None, "REJECTED", reason)
        return False

    ok, reason = validate_transaction_id(payment["transaction_id"])
    if not ok:
        log_verification(payment, None, "REJECTED", reason)
        return False

    # ---- Duplicates ----
    conn = get_db()
    dup = conn.execute(
        "SELECT id FROM payments WHERE utr=? LIMIT 1",
        (payment["utr"],),
    ).fetchone()
    if dup:
        conn.close()
        log_verification(payment, None, "DUPLICATE", "UTR_ALREADY_USED")
        return False

    dup = conn.execute(
        "SELECT id FROM payments WHERE transaction_id=? LIMIT 1",
        (payment["transaction_id"],),
    ).fetchone()
    if dup:
        conn.close()
        log_verification(payment, None, "DUPLICATE", "TXN_ALREADY_USED")
        return False

    dup = conn.execute(
        "SELECT id FROM payments WHERE gmail_uid=? LIMIT 1",
        (payment["gmail_uid"],),
    ).fetchone()
    conn.close()
    if dup:
        log_verification(payment, None, "DUPLICATE", "GMAIL_UID_SEEN")
        return False

    # ---- Match order by order_id (= purpose) ----
    order_id = payment.get("order_id", "")
    order = get_order_by_id(order_id)

    if not order:
        log(
            "PAYMENT | STATUS=NO_MATCHING_ORDER | "
            f"ORDER_ID={order_id or '-'} | UTR={payment['utr']}"
        )
        log_verification(
            payment, None, "UNMATCHED", "NO_MATCHING_ORDER"
        )
        save_unmatched(payment)
        return False

    # ---- Order status ----
    if order["status"] == "VERIFIED":
        log_verification(
            payment, order["order_id"], "DUPLICATE",
            "ORDER_ALREADY_PAID",
        )
        return False
    if order["status"] == "CANCELLED":
        log_verification(
            payment, order["order_id"], "REJECTED", "ORDER_CANCELLED"
        )
        return False
    if order["status"] == "EXPIRED":
        log_verification(
            payment, order["order_id"], "REJECTED", "ORDER_EXPIRED"
        )
        return False

    # ---- Order expiry (created_at + 15 min) ----
    try:
        created = datetime.strptime(
            order["created_at"], "%Y-%m-%d %H:%M:%S"
        )
        if datetime.now() > created + timedelta(
            minutes=PAYMENT_EXPIRY_MINUTES
        ):
            conn = get_db()
            conn.execute(
                "UPDATE orders SET status='EXPIRED' WHERE id=?",
                (order["id"],),
            )
            conn.commit()
            conn.close()
            log_verification(
                payment, order["order_id"], "REJECTED",
                "ORDER_EXPIRED",
            )
            return False
    except Exception:
        pass

    # ---- 15-min payment age ----
    pay_dt = parse_payment_date(payment.get("payment_date", ""))
    if pay_dt:
        age = now_ist() - pay_dt
        if age > timedelta(minutes=PAYMENT_EXPIRY_MINUTES):
            log_verification(
                payment, order["order_id"], "REJECTED",
                f"PAYMENT_TOO_OLD ({int(age.total_seconds()//60)}m)",
            )
            return False
    else:
        log_verification(
            payment, order["order_id"], "REJECTED",
            "PAYMENT_DATE_UNPARSEABLE",
        )
        return False

    # ---- Amount ----
    try:
        pay_amt = round(float(payment["amount"]), 2)
        ord_amt = round(float(order["amount"]), 2)
    except Exception:
        log_verification(
            payment, order["order_id"], "REJECTED",
            "AMOUNT_PARSE_ERROR",
        )
        return False

    if pay_amt != ord_amt:
        log_verification(
            payment, order["order_id"], "REJECTED",
            f"AMOUNT_MISMATCH (paid={pay_amt}, order={ord_amt})",
        )
        return False

    # ---- Optional official API ----
    api_result = verify_with_official_api(payment)
    if api_result["checked"] and not api_result["verified"]:
        log_verification(
            payment, order["order_id"], "REJECTED",
            "API_VERIFICATION_FAILED",
        )
        return False

    verification_source = (
        "GMAIL+ORDER_ID+UTR+TXN+OFFICIAL_API"
        if api_result["checked"]
        else "GMAIL+ORDER_ID+UTR+TXN"
    )

    # ---- Save ----
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO payments (
                amount, status, sender_name, transaction_id, utr,
                gmail_uid, payment_date, order_id,
                verification_source, created_at, verified_at, note
            ) VALUES (?, 'VERIFIED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment["amount"],
                payment["sender_name"],
                payment["transaction_id"],
                payment["utr"],
                payment["gmail_uid"],
                payment["payment_date"],
                order["order_id"],
                verification_source,
                now_str,
                now_str,
                f"Matched order {order['order_id']}",
            ),
        )

        conn.execute(
            """
            UPDATE orders
            SET status='VERIFIED',
                utr=?,
                transaction_id=?,
                sender_name=?,
                payment_date=?,
                verified_at=?,
                note='Payment received'
            WHERE id=? AND status='PENDING'
            """,
            (
                payment["utr"],
                payment["transaction_id"],
                payment["sender_name"],
                payment["payment_date"],
                now_str,
                order["id"],
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        log_verification(
            payment, order["order_id"], "REJECTED", "INTEGRITY_ERROR"
        )
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log(
        "PAYMENT | VERIFIED | "
        f"ORDER={order['order_id']} | "
        f"AMOUNT=₹{payment['amount']:.2f} | "
        f"UTR={payment['utr']} | TXN={payment['transaction_id']}"
    )
    log_verification(
        payment, order["order_id"], "VERIFIED", "ORDER_MATCHED"
    )
    return True


# ============================================================
# GMAIL SCAN
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
                status, md = mail.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw_email = None
                for item in md:
                    if isinstance(item, tuple):
                        raw_email = item[1]
                        break
                if not raw_email:
                    continue

                uid_text = (
                    uid.decode() if isinstance(uid, bytes)
                    else str(uid)
                )
                payment = parse_famapp_email(raw_email, uid_text)
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


def background_scanner():
    log("BACKGROUND_SCANNER | STARTED")
    while True:
        try:
            scan_gmail()
            cleanup_rate_store()
        except Exception as e:
            log(f"BACKGROUND_SCANNER | ERROR={e}")
        time.sleep(SCAN_INTERVAL)


try:
    setup_database()
except Exception as e:
    log(f"INIT | DB_SETUP_ERROR={e}")


# ============================================================
# API — QR
# ============================================================

@app.route("/api/qr")
def quick_qr():
    try:
        amount = round(float(request.args.get("amount", 0)), 2)
    except Exception:
        return jsonify({"success": False,
                        "error": "Invalid amount"}), 400
    if amount <= 0 or amount > 1000000:
        return jsonify({"success": False,
                        "error": "Invalid amount"}), 400

    order_id = (request.args.get("order_id") or "").strip()[:60]
    if not order_id:
        return jsonify({"success": False,
                        "error": "order_id required"}), 400

    upi_link = make_upi_link(amount, order_id)
    qr_bytes = get_qr_bytes(upi_link)

    return send_file(
        io.BytesIO(qr_bytes),
        mimetype="image/png",
        max_age=3600,
    )


# ============================================================
# API — Create Order
# ============================================================

@app.route("/api/create-order", methods=["GET", "POST"])
def create_order_api():
    ip = get_client_ip()
    allowed, _ = rate_limit("create-order", ip, 200)
    if not allowed:
        return jsonify({"success": False,
                        "error": "Too many requests"}), 429

    data = (
        request.get_json(silent=True)
        or request.form
        or request.args
    )

    try:
        amount = round(float(data.get("amount", 0)), 2)
    except Exception:
        return jsonify({"success": False,
                        "error": "Invalid amount"}), 400

    if amount <= 0 or amount > 1000000:
        return jsonify({"success": False,
                        "error": "Amount out of range"}), 400

    hint = (data.get("hint") or "").strip()[:20]

    order = create_order(amount, hint=hint)

    log(
        f"ORDER | CREATED | ID={order['order_id']} | "
        f"AMOUNT=₹{amount}"
    )

    return jsonify({
        "success": True,
        "order_id": order["order_id"],
        "amount": amount,
        "created_at": order["created_at"],
        "expires_at": order["expires_at"],
        "expiry_minutes": PAYMENT_EXPIRY_MINUTES,
        "qr_url": (
            f"/api/qr?amount={amount}"
            f"&order_id={quote(order['order_id'])}"
        ),
    })


# ============================================================
# API — Order Status (main verification endpoint)
# ============================================================

@app.route("/api/order/<order_id>")
def order_status(order_id):
    row = get_order_by_id(order_id)
    if not row:
        return jsonify({"success": False,
                        "error": "Order not found"}), 404

    o = dict(row)

    # Compute remaining time
    try:
        expires = datetime.strptime(
            o["expires_at"], "%Y-%m-%d %H:%M:%S"
        )
        remaining = int((expires - datetime.now()).total_seconds())
    except Exception:
        remaining = 0

    # Auto-mark expired if still pending
    if o["status"] == "PENDING" and remaining <= 0:
        conn = get_db()
        conn.execute(
            "UPDATE orders SET status='EXPIRED' WHERE id=?",
            (o["id"],),
        )
        conn.commit()
        conn.close()
        o["status"] = "EXPIRED"

    o["remaining_seconds"] = max(remaining, 0)
    o["expired"] = remaining <= 0

    return jsonify({"success": True, "order": o})


# ============================================================
# API — Cancel Order
# ============================================================

@app.route("/api/cancel-order", methods=["GET", "POST"])
def cancel_order_api():
    data = (
        request.get_json(silent=True)
        or request.form
        or request.args
    )
    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"success": False,
                        "error": "order_id required"}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id=? LIMIT 1",
        (order_id,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False,
                        "error": "Order not found"}), 404

    conn.execute(
        "UPDATE orders SET status='CANCELLED' "
        "WHERE id=? AND status='PENDING'",
        (row["id"],),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "order_id": order_id,
                    "status": "CANCELLED"})


# ============================================================
# LOOKUP HELPERS
# ============================================================

@app.route("/api/verify-utr")
def verify_utr_lookup():
    utr = (request.args.get("utr") or "").strip().upper()
    if not utr:
        return jsonify({"success": False,
                        "error": "utr required"}), 400
    ok, reason = validate_utr_format(utr)
    if not ok:
        return jsonify({"success": False,
                        "valid_format": False,
                        "reason": reason})
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM payments WHERE utr=? LIMIT 1", (utr,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": True, "found": False,
                        "utr": utr, "valid_format": True,
                        "reason": reason})
    return jsonify({"success": True, "found": True,
                    "utr": utr, "valid_format": True,
                    "reason": reason,
                    "payment": dict(row)})


@app.route("/api/verify-txn")
def verify_txn_lookup():
    txn = (request.args.get("txn") or "").strip().upper()
    if not txn:
        return jsonify({"success": False,
                        "error": "txn required"}), 400
    ok, reason = validate_transaction_id(txn)
    if not ok:
        return jsonify({"success": False,
                        "valid_format": False,
                        "reason": reason})
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM payments WHERE transaction_id=? LIMIT 1",
        (txn,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": True, "found": False,
                        "transaction_id": txn,
                        "valid_format": True,
                        "reason": reason})
    return jsonify({"success": True, "found": True,
                    "transaction_id": txn,
                    "valid_format": True,
                    "reason": reason,
                    "payment": dict(row)})


# ============================================================
# ADMIN
# ============================================================

def _admin_check():
    key = (
        request.args.get("admin_key")
        or (request.get_json(silent=True) or {}).get("admin_key")
        or (request.form.to_dict() or {}).get("admin_key")
        or ""
    ).strip()
    return key == ADMIN_KEY


@app.route("/api/scan", methods=["GET", "POST"])
def manual_scan():
    if not _admin_check():
        security_log("UNAUTHORIZED_SCAN")
        return jsonify({"success": False,
                        "error": "Invalid admin_key"}), 401

    ip = get_client_ip()
    allowed, _ = rate_limit("scan", ip, MAX_SCAN_ATTEMPTS)
    if not allowed:
        return jsonify({"success": False,
                        "error": "Too many scans"}), 429

    scan_gmail()
    return jsonify({
        "success": True,
        "message": "Scan completed",
        "range": f"Last {EMAIL_DAYS} days",
    })


@app.route("/api/payments")
def payments_api():
    if not _admin_check():
        return jsonify({"success": False,
                        "error": "Invalid admin_key"}), 401
    limit = min(int(request.args.get("limit", 200)), 2000)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM payments ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify({
        "success": True,
        "count": len(rows),
        "payments": [dict(r) for r in rows],
    })


@app.route("/api/orders")
def orders_api():
    if not _admin_check():
        return jsonify({"success": False,
                        "error": "Invalid admin_key"}), 401
    limit = min(int(request.args.get("limit", 200)), 2000)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify({
        "success": True,
        "count": len(rows),
        "orders": [dict(r) for r in rows],
    })


@app.route("/api/verification-logs")
def verification_logs():
    if not _admin_check():
        return jsonify({"success": False,
                        "error": "Invalid admin_key"}), 401
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM verification_logs "
            "ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return jsonify({"success": True, "count": len(rows),
                        "logs": [dict(r) for r in rows]})
    finally:
        conn.close()


# ============================================================
# ADMIN PANEL
# ============================================================

@app.route("/admin")
def admin():
    provided = (request.args.get("admin_key") or "").strip()
    if provided != ADMIN_KEY:
        if provided:
            security_log("ADMIN_BAD_KEY")
        return render_template_string("""
<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Admin</title>
<style>
body{background:#0d0d0d;color:#eee;font-family:Arial;
display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0}
.box{background:#161616;padding:32px;border-radius:14px;
border:1px solid #2a2a2a;width:100%;max-width:340px}
h2{margin:0 0 20px}
input{width:100%;padding:12px;background:#0d0d0d;color:#eee;
border:1px solid #333;border-radius:8px;font-size:15px;
box-sizing:border-box}
button{width:100%;margin-top:14px;padding:12px;
background:#2d6cdf;color:#fff;border:0;border-radius:8px;
font-weight:600;cursor:pointer}
.err{color:#ff6b6b;margin-top:12px}
</style></head><body>
<form class="box" method="GET" action="/admin">
<h2>🔒 Admin Login</h2>
<input type="password" name="admin_key" placeholder="Key"
autofocus required>
<button>Unlock</button>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
</form></body></html>
""", error="Invalid key" if provided else "")

    conn = get_db()
    payments = conn.execute(
        "SELECT * FROM payments ORDER BY id DESC LIMIT 200"
    ).fetchall()
    orders = conn.execute(
        "SELECT * FROM orders ORDER BY id DESC LIMIT 200"
    ).fetchall()
    logs = conn.execute(
        "SELECT * FROM verification_logs ORDER BY id DESC LIMIT 100"
    ).fetchall()
    conn.close()

    total_amt = sum(p["amount"] for p in payments)
    verified = sum(
        1 for p in payments if p["status"] == "VERIFIED"
    )
    pending = sum(
        1 for o in orders if o["status"] == "PENDING"
    )

    return render_template_string("""
<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>FamApp Admin</title>
<style>
body{background:#0d0d0d;color:#eee;margin:0;
font-family:Arial;padding:20px}
h1{margin:0 0 6px}
h2{margin:30px 0 12px;font-size:16px;color:#aaa;
text-transform:uppercase;letter-spacing:1px}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.card{background:#161616;border:1px solid #2a2a2a;
border-radius:12px;padding:16px 20px;flex:1;min-width:150px}
.card .lbl{font-size:11px;color:#888;text-transform:uppercase}
.card b{display:block;font-size:24px;margin-top:6px}
.green b{color:#42e695}.yellow b{color:#ffd166}
.blue b{color:#5eb0ff}
table{width:100%;border-collapse:collapse;background:#161616;
border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:10px 12px;border-bottom:1px solid #262626;
text-align:left}
th{background:#1f1f1f;color:#aaa;font-size:11px;
text-transform:uppercase}
.mono{font-family:monospace;font-size:12px}
.tag{padding:2px 8px;border-radius:6px;font-size:10px;
font-weight:600}
.ok{background:#0f3a23;color:#42e695}
.bad{background:#3a1010;color:#ff6b6b}
.warn{background:#3a2e0f;color:#ffd166}
button{padding:10px 16px;border:0;border-radius:8px;
background:#2d6cdf;color:#fff;font-weight:600;cursor:pointer}
</style></head><body>

<h1>💰 FamApp Admin</h1>
<p>UPI: <b>{{ upi }}</b> · Expiry: {{ expiry }} min</p>

<div class="stats">
<div class="card green"><div class="lbl">Verified</div>
<b>{{ verified }}</b></div>
<div class="card blue"><div class="lbl">Total</div>
<b>₹{{ "%.2f"|format(total_amt) }}</b></div>
<div class="card yellow"><div class="lbl">Pending</div>
<b>{{ pending }}</b></div>
</div>

<button onclick="scan()">🔄 Scan Gmail</button>

<h2>💳 Payments</h2>
<table>
<tr><th>ID</th><th>Amount</th><th>Status</th><th>Sender</th>
<th>UTR</th><th>TXN</th><th>Order ID</th>
<th>Payment Date</th></tr>
{% for p in payments %}
<tr>
<td class="mono">{{ p["id"] }}</td>
<td>₹{{ "%.2f"|format(p["amount"]) }}</td>
<td><span class="tag {% if p["status"]=='VERIFIED' %}ok{% else %}warn{% endif %}">{{ p["status"] }}</span></td>
<td>{{ p["sender_name"] or "-" }}</td>
<td class="mono">{{ p["utr"] or "-" }}</td>
<td class="mono">{{ p["transaction_id"] or "-" }}</td>
<td class="mono">{{ p["order_id"] or "-" }}</td>
<td class="mono">{{ p["payment_date"] or "-" }}</td>
</tr>
{% endfor %}
</table>

<h2>📦 Orders</h2>
<table>
<tr><th>Order ID</th><th>Amount</th><th>Status</th>
<th>UTR</th><th>TXN</th><th>Created</th><th>Expires</th></tr>
{% for o in orders %}
<tr>
<td class="mono">{{ o["order_id"] }}</td>
<td>₹{{ "%.2f"|format(o["amount"]) }}</td>
<td><span class="tag {% if o["status"]=='VERIFIED' %}ok{% elif o["status"]=='PENDING' %}warn{% else %}bad{% endif %}">{{ o["status"] }}</span></td>
<td class="mono">{{ o["utr"] or "-" }}</td>
<td class="mono">{{ o["transaction_id"] or "-" }}</td>
<td class="mono">{{ o["created_at"] }}</td>
<td class="mono">{{ o["expires_at"] }}</td>
</tr>
{% endfor %}
</table>

<h2>🛡️ Verification Logs</h2>
<table>
<tr><th>Time</th><th>Order</th><th>Amount</th><th>UTR</th>
<th>TXN</th><th>Result</th><th>Reason</th></tr>
{% for l in logs %}
<tr>
<td class="mono">{{ l["created_at"] }}</td>
<td class="mono">{{ l["order_id"] or "-" }}</td>
<td>₹{{ "%.2f"|format(l["amount"] or 0) }}</td>
<td class="mono">{{ l["utr"] or "-" }}</td>
<td class="mono">{{ l["transaction_id"] or "-" }}</td>
<td><span class="tag {% if l["result"]=='VERIFIED' %}ok{% else %}bad{% endif %}">{{ l["result"] }}</span></td>
<td class="mono">{{ l["reason"] or "-" }}</td>
</tr>
{% endfor %}
</table>

<script>
async function scan(){
  const r = await fetch("/api/scan?admin_key={{ key }}");
  const d = await r.json();
  alert(d.message || d.error);
  location.reload();
}
</script>
</body></html>
""",
        payments=payments,
        orders=orders,
        logs=logs,
        verified=verified,
        total_amt=total_amt,
        pending=pending,
        upi=UPI_ID,
        key=ADMIN_KEY,
        expiry=PAYMENT_EXPIRY_MINUTES,
    )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "name": "FamApp Gateway v5",
        "status": "online",
        "note": "order_id = purpose",
        "expiry_minutes": PAYMENT_EXPIRY_MINUTES,
        "endpoints": {
            "create_order": "/api/create-order?amount=100&hint=TG123",
            "qr": "/api/qr?amount=100&order_id=...",
            "order_status": "/api/order/<order_id>",
            "cancel_order": "POST /api/cancel-order",
            "verify_utr": "/api/verify-utr?utr=...",
            "verify_txn": "/api/verify-txn?txn=...",
            "admin_panel": "/admin?admin_key=admin@key",
        },
    })


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    log("FamApp Gateway v5 — local dev")
    threading.Thread(target=background_scanner, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
