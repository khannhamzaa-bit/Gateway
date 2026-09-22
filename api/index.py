# ============================================================
# FAMAPP PAYMENT GATEWAY — VERCEL PYTHON EDITION
# Storage: SQLite (/tmp). UTR is the verification key.
# order_id is AUTO-GENERATED and IS the UPI purpose/reference.
# No hint. No KV. Nothing prepended/appended to order_id.
# ============================================================

import os
import re
import io
import time
import random
import hashlib
import imaplib
import email
import sqlite3
import threading
from datetime import datetime, timedelta
from email.header import decode_header
from urllib.parse import quote

import qrcode
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

GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

UPI_ID = os.environ.get("UPI_ID", "khannhamzaa@fam")
UPI_NAME = os.environ.get("UPI_NAME", "Hamza")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin@key")

DATABASE = os.environ.get("DATABASE_PATH", "/tmp/payments.db")

PAYMENT_EXPIRY_MINUTES = 15
EMAIL_DAYS = 7
SCAN_INTERVAL = 20
MIN_SCAN_INTERVAL = 10

UTR_MIN_LENGTH = 12
UTR_MAX_LENGTH = 22
TXN_MIN_LENGTH = 6
TXN_MAX_LENGTH = 64

ORDER_ID_RE = re.compile(r"^ORD\d{12}[0-9A-F]{4}$")

QR_CACHE_MAX = 200


# ============================================================
# STATE
# ============================================================

_scan_lock = threading.Lock()
_last_scan_time = 0.0

_qr_cache = {}
_qr_cache_lock = threading.Lock()

_app = Flask(__name__)


def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


# ============================================================
# DATABASE  (SQLite in /tmp)
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE, timeout=30, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def setup_database():
    conn = get_db()

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            amount REAL,
            utr TEXT,
            transaction_id TEXT,
            sender_name TEXT,
            gmail_uid TEXT,
            result TEXT,
            reason TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


try:
    setup_database()
except Exception as e:
    log(f"INIT | DB_ERROR | {e}")


# ============================================================
# ORDER ID  (auto-generated; IS the reference / UPI purpose)
# ============================================================

def generate_order_id():
    """
    ORD + YYMMDDHHMMSS (12 digits, UTC) + 4 uppercase hex.
    Example: ORD260922162603CBCC
    """
    now = datetime.utcnow()
    ts = now.strftime("%y%m%d%H%M%S")
    rand = f"{random.randint(0, 0xFFFF):04X}"
    return f"ORD{ts}{rand}"


# ============================================================
# UPI / QR
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


def get_qr_bytes(data):
    key = hashlib.md5(data.encode()).hexdigest()

    with _qr_cache_lock:
        if key in _qr_cache:
            return _qr_cache[key]

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
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
        return None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        return mail
    except Exception as e:
        log(f"GMAIL | LOGIN_FAILED | {e}")
        return None


def decode_text(value):
    if not value:
        return ""
    out = []
    try:
        for part, enc in decode_header(value):
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(str(part))
    except Exception:
        return str(value)
    return "".join(out)


def get_email_body(message):
    text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text += " " + payload.decode(charset, errors="ignore")
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
    try:
        message = email.message_from_bytes(raw_email)
    except Exception:
        return None

    subject = decode_text(message.get("Subject", ""))
    body = get_email_body(message)
    text = subject + " " + body
    lower = text.lower()

    if "famapp" not in lower and "fam app" not in lower:
        return None
    if "successfully received" not in lower:
        return None

    am = re.search(r"₹\s*([0-9,]+(?:\.[0-9]+)?)", text)
    if not am:
        return None
    try:
        amount = round(float(am.group(1).replace(",", "")), 2)
    except Exception:
        return None

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

    txn = ""
    tm = re.search(
        r"Transaction\s*ID\s*:\s*([A-Za-z0-9_-]+)",
        text, re.IGNORECASE,
    )
    if tm:
        txn = tm.group(1).strip()

    utr = ""
    um = re.search(r"UTR\s*:\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    if um:
        utr = um.group(1).strip()

    order_id = ""
    pm = re.search(
        r"Purpose\s*:\s*([A-Za-z0-9_\-]+)",
        text, re.IGNORECASE,
    )
    if pm:
        order_id = pm.group(1).strip()

    payment_date = ""
    dm = re.search(
        r"Date\s*:\s*(.*?)\s*(?:Updated\s*Balance|UTR\s*:)",
        text, re.IGNORECASE,
    )
    if dm:
        payment_date = dm.group(1).strip()

    return {
        "gmail_uid": str(gmail_uid),
        "sender_name": sender_name,
        "amount": amount,
        "transaction_id": txn,
        "utr": utr,
        "order_id": order_id,
        "payment_date": payment_date,
    }


# ============================================================
# DATE PARSING
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


def human_age(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    if seconds < 86400:
        return f"{seconds // 3600} hours"
    return f"{seconds // 86400} days"


# ============================================================
# VALIDATION
# ============================================================

def validate_utr(utr):
    if not utr:
        return False, "UTR_MISSING"
    utr = utr.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+", utr):
        return False, "UTR_INVALID_CHARS"
    if not (UTR_MIN_LENGTH <= len(utr) <= UTR_MAX_LENGTH):
        return False, f"UTR_INVALID_LENGTH ({len(utr)})"
    if len(utr) == 12 and utr.isdigit():
        return True, "UTR_VALID_UPI"
    if len(utr) == 22 and re.fullmatch(r"[A-Z]{4}[A-Z0-9]{18}", utr):
        return True, "UTR_VALID_IMPS"
    if 12 <= len(utr) <= 22:
        return True, "UTR_VALID"
    return False, "UTR_INVALID"


def validate_txn(txn):
    if not txn:
        return False, "TXN_MISSING"
    txn = txn.strip().upper()
    if not (TXN_MIN_LENGTH <= len(txn) <= TXN_MAX_LENGTH):
        return False, f"TXN_INVALID_LENGTH ({len(txn)})"
    if not re.fullmatch(r"[A-Z0-9_-]+", txn):
        return False, "TXN_INVALID_CHARS"
    return True, "TXN_VALID"


# ============================================================
# LOG
# ============================================================

def log_verification(payment, order_id, result, reason):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO verification_logs (
                order_id, amount, utr, transaction_id,
                sender_name, gmail_uid, result, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                payment.get("amount"),
                payment.get("utr"),
                payment.get("transaction_id"),
                payment.get("sender_name"),
                payment.get("gmail_uid"),
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
# ORDER HELPERS
# ============================================================

def get_order_by_id(order_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id=? LIMIT 1",
        (order_id,),
    ).fetchone()
    conn.close()
    return row


def get_payment_by_order(order_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM payments WHERE order_id=? "
        "ORDER BY id DESC LIMIT 1",
        (order_id,),
    ).fetchone()
    conn.close()
    return row


def create_order(amount):
    """Auto-generate a unique order_id and persist it."""
    now = datetime.now()
    expires = now + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

    for _ in range(5):
        order_id = generate_order_id()
        if not ORDER_ID_RE.match(order_id):
            continue
        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, amount, status, created_at, expires_at
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
        except sqlite3.IntegrityError:
            conn.close()
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return {
            "order_id": order_id,
            "amount": amount,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return None


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
                created_at, note
            ) VALUES (?, 'UNMATCHED', ?, ?, ?, ?, ?, ?, ?, ?)
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
# VERIFY PAYMENT (UTR is the key; order_id = Purpose)
# ============================================================

def verify_payment(payment):

    if not payment.get("utr"):
        log_verification(payment, None, "REJECTED", "UTR_MISSING")
        return False
    if not payment.get("transaction_id"):
        log_verification(payment, None, "REJECTED", "TXN_MISSING")
        return False

    ok, reason = validate_utr(payment["utr"])
    if not ok:
        log_verification(payment, None, "REJECTED", reason)
        return False

    ok, reason = validate_txn(payment["transaction_id"])
    if not ok:
        log_verification(payment, None, "REJECTED", reason)
        return False

    conn = get_db()
    dup = conn.execute(
        "SELECT id FROM payments WHERE utr=? LIMIT 1",
        (payment["utr"],),
    ).fetchone()
    if dup:
        conn.close()
        log_verification(payment, None, "DUPLICATE", "UTR_USED")
        return False

    dup = conn.execute(
        "SELECT id FROM payments WHERE transaction_id=? LIMIT 1",
        (payment["transaction_id"],),
    ).fetchone()
    if dup:
        conn.close()
        log_verification(payment, None, "DUPLICATE", "TXN_USED")
        return False

    dup = conn.execute(
        "SELECT id FROM payments WHERE gmail_uid=? LIMIT 1",
        (payment["gmail_uid"],),
    ).fetchone()
    conn.close()
    if dup:
        log_verification(payment, None, "DUPLICATE", "GMAIL_SEEN")
        return False

    order_id = payment.get("order_id", "")
    order = get_order_by_id(order_id)

    if not order:
        log(f"PAYMENT | NO_ORDER | order_id={order_id or '-'}")
        log_verification(payment, None, "UNMATCHED", "NO_ORDER")
        save_unmatched(payment)
        return False

    if order["status"] == "VERIFIED":
        log_verification(payment, order_id, "DUPLICATE", "ORDER_PAID")
        return False
    if order["status"] in ("CANCELLED", "EXPIRED"):
        log_verification(
            payment, order_id, "REJECTED", f"ORDER_{order['status']}"
        )
        return False

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
                payment, order_id, "REJECTED", "ORDER_EXPIRED"
            )
            return False
    except Exception:
        pass

    pay_dt = parse_payment_date(payment.get("payment_date", ""))
    if pay_dt:
        age = now_ist() - pay_dt
        if age > timedelta(minutes=PAYMENT_EXPIRY_MINUTES):
            log_verification(
                payment, order_id, "REJECTED",
                f"PAYMENT_OLD ({int(age.total_seconds()//60)}m)",
            )
            return False
    else:
        log_verification(
            payment, order_id, "REJECTED", "DATE_UNPARSEABLE"
        )
        return False

    try:
        pay_amt = round(float(payment["amount"]), 2)
        ord_amt = round(float(order["amount"]), 2)
    except Exception:
        log_verification(
            payment, order_id, "REJECTED", "AMOUNT_PARSE_ERR"
        )
        return False

    if pay_amt != ord_amt:
        log_verification(
            payment, order_id, "REJECTED",
            f"AMOUNT_MISMATCH ({pay_amt} vs {ord_amt})",
        )
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO payments (
                amount, status, sender_name, transaction_id, utr,
                gmail_uid, payment_date, order_id,
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
                order["order_id"],
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
            payment, order_id, "REJECTED", "INTEGRITY"
        )
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass

    log(f"PAYMENT | VERIFIED | order={order_id} | ₹{payment['amount']}")
    log_verification(payment, order_id, "VERIFIED", "ORDER_MATCHED")
    return True


# ============================================================
# GMAIL SCAN
# ============================================================

def scan_gmail(force=False):
    global _last_scan_time

    if not force:
        with _scan_lock:
            now = time.time()
            if now - _last_scan_time < MIN_SCAN_INTERVAL:
                return {"skipped": True}
            _last_scan_time = now

    log("SCANNER | START")
    mail = connect_gmail()
    if not mail:
        return {"error": "gmail login failed"}

    found = 0
    verified = 0

    try:
        mail.select("INBOX", readonly=True)
        since = (
            datetime.now() - timedelta(days=EMAIL_DAYS)
        ).strftime("%d-%b-%Y")

        status, data = mail.uid("search", None, f"SINCE {since}")
        if status != "OK":
            mail.logout()
            return {"error": "search failed"}

        uids = data[0].split()

        for uid in reversed(uids):
            try:
                status, md = mail.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = None
                for item in md:
                    if isinstance(item, tuple):
                        raw = item[1]
                        break
                if not raw:
                    continue
                uid_text = (
                    uid.decode() if isinstance(uid, bytes)
                    else str(uid)
                )
                payment = parse_famapp_email(raw, uid_text)
                if not payment:
                    continue
                found += 1
                if verify_payment(payment):
                    verified += 1
            except Exception as e:
                log(f"SCANNER | EMAIL_ERR | {e}")

        log(f"SCANNER | DONE | FOUND={found} | VERIFIED={verified}")
    except Exception as e:
        log(f"SCANNER | ERROR | {e}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return {"found": found, "verified": verified}


def background_scanner():
    while True:
        try:
            scan_gmail(force=True)
        except Exception as e:
            log(f"BG_SCANNER | ERROR | {e}")
        time.sleep(SCAN_INTERVAL)


# ============================================================
# ROUTES
# ============================================================

@_app.route("/")
def home():
    return jsonify({
        "name": "FamApp Gateway",
        "status": "online",
        "order_id_pattern": "^ORD\\d{12}[0-9A-F]{4}$",
        "expiry_minutes": PAYMENT_EXPIRY_MINUTES,
        "endpoints": {
            "create_order": "/api/create-order?amount=100",
            "qr": "/api/qr?amount=100&order_id=ORD...",
            "order_status": "/api/order/<order_id>",
            "cancel_order": "POST /api/cancel-order",
            "verify_utr": "/api/verify-utr?utr=...",
            "verify_txn": "/api/verify-txn?txn=...",
            "scan": "POST /api/scan?admin_key=...",
            "admin_panel": "/admin?admin_key=...",
        },
    })


@_app.route("/api/qr")
def quick_qr():
    try:
        amount = round(float(request.args.get("amount", 0)), 2)
    except Exception:
        return jsonify({"success": False, "error": "Invalid amount"}), 400
    if amount <= 0 or amount > 1000000:
        return jsonify({"success": False, "error": "Invalid amount"}), 400

    order_id = (request.args.get("order_id") or "").strip()[:60]
    if not order_id:
        return jsonify({"success": False, "error": "order_id required"}), 400

    upi_link = make_upi_link(amount, order_id)
    qr_bytes = get_qr_bytes(upi_link)

    return send_file(
        io.BytesIO(qr_bytes),
        mimetype="image/png",
        max_age=3600,
    )


@_app.route("/api/create-order", methods=["GET", "POST"])
def create_order_api():
    data = request.get_json(silent=True) or request.form or request.args

    try:
        amount = round(float(data.get("amount", 0)), 2)
    except Exception:
        return jsonify({"success": False, "error": "Invalid amount"}), 400

    if amount <= 0 or amount > 1000000:
        return jsonify({"success": False, "error": "Amount out of range"}), 400

    order = create_order(amount)
    if not order:
        return jsonify({
            "success": False,
            "error": "Could not allocate a unique order id, please retry",
        }), 500

    log(f"ORDER | CREATED | {order['order_id']} | ₹{amount}")

    return jsonify({
        "success": True,
        "order_id": order["order_id"],
        "amount": amount,
        "qr_url": (
            f"/api/qr?amount={amount}"
            f"&order_id={quote(order['order_id'])}"
        ),
    })


@_app.route("/api/order/<order_id>")
def order_status(order_id):
    row = get_order_by_id(order_id)
    if not row:
        return jsonify({"success": False, "error": "Order not found"}), 404

    o = dict(row)

    remaining = 0
    try:
        expires = datetime.strptime(
            o["expires_at"], "%Y-%m-%d %H:%M:%S"
        )
        remaining = int((expires - datetime.now()).total_seconds())
    except Exception:
        remaining = 0

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

    pay = get_payment_by_order(order_id)
    payment_obj = dict(pay) if pay else None

    utr_ok, utr_reason = validate_utr(o.get("utr") or "")
    txn_ok, txn_reason = validate_txn(o.get("transaction_id") or "")

    age_seconds = None
    age_human_str = "unknown"
    within_expiry = False
    if o.get("payment_date"):
        pdt = parse_payment_date(o["payment_date"])
        if pdt:
            age_seconds = int((now_ist() - pdt).total_seconds())
            age_human_str = human_age(age_seconds)
            within_expiry = age_seconds <= PAYMENT_EXPIRY_MINUTES * 60

    return jsonify({
        "success": True,
        "order": {
            "order_id": o["order_id"],
            "amount": o["amount"],
            "status": o["status"],
            "created_at": o["created_at"],
            "expires_at": o["expires_at"],
            "verified_at": o["verified_at"],
            "remaining_seconds": o["remaining_seconds"],
            "expired": o["expired"],
            "utr": o.get("utr"),
            "transaction_id": o.get("transaction_id"),
            "sender_name": o.get("sender_name"),
            "payment_date": o.get("payment_date"),
            "payment": payment_obj,
            "verification": {
                "utr_valid": utr_ok,
                "utr_reason": utr_reason,
                "txn_valid": txn_ok,
                "txn_reason": txn_reason,
                "age_seconds": age_seconds,
                "age_human": age_human_str,
                "within_expiry": within_expiry,
                "expiry_minutes": PAYMENT_EXPIRY_MINUTES,
            },
        },
    })


@_app.route("/api/cancel-order", methods=["GET", "POST"])
def cancel_order_api():
    data = request.get_json(silent=True) or request.form or request.args
    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"success": False, "error": "order_id required"}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id=? LIMIT 1",
        (order_id,),
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "error": "Order not found"}), 404

    conn.execute(
        "UPDATE orders SET status='CANCELLED' "
        "WHERE id=? AND status='PENDING'",
        (row["id"],),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "order_id": order_id,
        "status": "CANCELLED",
    })


@_app.route("/api/verify-utr")
def verify_utr_lookup():
    utr = (request.args.get("utr") or "").strip().upper()
    if not utr:
        return jsonify({"success": False, "error": "utr required"}), 400
    ok, reason = validate_utr(utr)
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
        return jsonify({
            "success": True, "found": False,
            "utr": utr, "valid_format": True,
            "reason": reason,
        })
    return jsonify({
        "success": True, "found": True,
        "utr": utr, "valid_format": True,
        "reason": reason,
        "payment": dict(row),
    })


@_app.route("/api/verify-txn")
def verify_txn_lookup():
    txn = (request.args.get("txn") or "").strip().upper()
    if not txn:
        return jsonify({"success": False, "error": "txn required"}), 400
    ok, reason = validate_txn(txn)
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
        return jsonify({
            "success": True, "found": False,
            "transaction_id": txn, "valid_format": True,
            "reason": reason,
        })
    return jsonify({
        "success": True, "found": True,
        "transaction_id": txn, "valid_format": True,
        "reason": reason,
        "payment": dict(row),
    })


# ============================================================
# ADMIN API
# ============================================================

def _admin_check():
    key = (
        request.args.get("admin_key")
        or (request.get_json(silent=True) or {}).get("admin_key")
        or (request.form.to_dict() or {}).get("admin_key")
        or ""
    ).strip()
    return key == ADMIN_KEY


@_app.route("/api/scan", methods=["GET", "POST"])
def manual_scan():
    if not _admin_check():
        return jsonify({"success": False, "error": "Invalid admin_key"}), 401
    result = scan_gmail(force=True)
    return jsonify({
        "success": True,
        "message": "Scan completed",
        "range": f"Last {EMAIL_DAYS} days",
        "result": result,
    })


@_app.route("/api/payments")
def payments_api():
    if not _admin_check():
        return jsonify({"success": False, "error": "Invalid admin_key"}), 401
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


@_app.route("/api/orders")
def orders_api():
    if not _admin_check():
        return jsonify({"success": False, "error": "Invalid admin_key"}), 401
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


@_app.route("/api/verification-logs")
def verification_logs():
    if not _admin_check():
        return jsonify({"success": False, "error": "Invalid admin_key"}), 401
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM verification_logs "
            "ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return jsonify({
            "success": True,
            "count": len(rows),
            "logs": [dict(r) for r in rows],
        })
    finally:
        conn.close()


# ============================================================
# ADMIN PANEL
# ============================================================

@_app.route("/admin")
def admin():
    provided = (request.args.get("admin_key") or "").strip()
    if provided != ADMIN_KEY:
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
body{background:#0d0d0d;color:#eee;margin:0;font-family:Arial;padding:20px}
h1{margin:0 0 6px}
h2{margin:30px 0 12px;font-size:16px;color:#aaa;text-transform:uppercase}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
.card{background:#161616;border:1px solid #2a2a2a;border-radius:12px;
padding:16px 20px;flex:1;min-width:150px}
.card .lbl{font-size:11px;color:#888;text-transform:uppercase}
.card b{display:block;font-size:24px;margin-top:6px}
.green b{color:#42e695}.yellow b{color:#ffd166}.blue b{color:#5eb0ff}
table{width:100%;border-collapse:collapse;background:#161616;
border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:10px 12px;border-bottom:1px solid #262626;text-align:left}
th{background:#1f1f1f;color:#aaa;font-size:11px;text-transform:uppercase}
.mono{font-family:monospace;font-size:12px}
.tag{padding:2px 8px;border-radius:6px;font-size:10px;font-weight:600}
.ok{background:#0f3a23;color:#42e695}
.bad{background:#3a1010;color:#ff6b6b}
.warn{background:#3a2e0f;color:#ffd166}
button{padding:10px 16px;border:0;border-radius:8px;
background:#2d6cdf;color:#fff;font-weight:600;cursor:pointer}
</style></head><body>

<h1>💰 FamApp Admin</h1>
<p>UPI: <b>{{ upi }}</b> · Expiry: {{ expiry }} min</p>

<div class="stats">
<div class="card green"><div class="lbl">Verified</div><b>{{ verified }}</b></div>
<div class="card blue"><div class="lbl">Total</div><b>₹{{ "%.2f"|format(total_amt) }}</b></div>
<div class="card yellow"><div class="lbl">Pending</div><b>{{ pending }}</b></div>
</div>

<button onclick="scan()">🔄 Scan Gmail</button>

<h2>💳 Payments</h2>
<table>
<tr><th>ID</th><th>Amount</th><th>Status</th><th>Sender</th>
<th>UTR</th><th>TXN</th><th>Order</th><th>Date</th></tr>
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
<tr><th>Order ID</th><th>Amount</th><th>Status</th><th>UTR</th>
<th>TXN</th><th>Created</th><th>Expires</th></tr>
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
  const r = await fetch("/api/scan?admin_key={{ key }}", {method:"POST"});
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
# EXPORT for Vercel (WSGI handler)
# ============================================================

app = _app


# ============================================================
# LOCAL DEV ENTRY
# ============================================================

if __name__ == "__main__":
    log("FamApp Gateway — local dev")
    threading.Thread(target=background_scanner, daemon=True).start()
    _app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        debug=False,
        threaded=True,
    )
