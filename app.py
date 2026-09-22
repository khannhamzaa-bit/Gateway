import os
import hmac
import hashlib
import secrets
import json
import logging
import socket
import urllib.parse
import imaplib
import email
import asyncio
import re
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import Optional
from email.header import decode_header

import asyncpg
import qrcode
from io import BytesIO
import base64
import httpx

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# OS CODEX — UPI PAYMENT GATEWAY
# Developer: @khannhamzaa
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OS-CODEX")


# ============================================================
# ⚙️ CONFIG
# ============================================================

DATABASE_URL = "postgresql://neondb_owner:npg_USgbsX5pDRO4@ep-bold-art-avpvzvhx-pooler.c-11.us-east-1.aws.neon.tech/neondb"

MERCHANT_NAME   = "HAMZA KHAN"
MERCHANT_UPI_ID = "khannhamzaa@fam"

ADMIN_KEY      = "os-codex@123"
WEBHOOK_SECRET = "change_me_random_secret"

MAX_PAYMENT_AMOUNT     = 100000.0
PAYMENT_EXPIRY_MINUTES = 15

ALLOWED_ORIGINS = ["*"]

GMAIL_EMAIL = "hamza.ali.khan6200@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "kdncbotxdmpaqeep")
FAMAPP_SENDER = "famapp"
EMAIL_CHECK_INTERVAL = 30

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "")

BRAND      = "OS CODEX"
DEVELOPER  = "@khannhamzaa"
POWERED_BY = "OS CODEX"

# ============================================================
# END CONFIG
# ============================================================


# ============================================================
# DNS FIX FOR VERCEL
# ============================================================

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo


# ============================================================
# DB POOL
# ============================================================

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = DATABASE_URL.split("?")[0]
        _pool = await asyncpg.create_pool(
            dsn=dsn, min_size=1, max_size=3, ssl="require", command_timeout=20,
        )
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS merchants (
                id SERIAL PRIMARY KEY,
                merchant_id VARCHAR(64) UNIQUE NOT NULL,
                api_key_hash VARCHAR(128) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                webhook_url TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                order_id VARCHAR(64) UNIQUE NOT NULL,
                merchant_id VARCHAR(64) NOT NULL,
                idempotency_key VARCHAR(128),
                amount NUMERIC(12,2) NOT NULL,
                currency VARCHAR(8) NOT NULL DEFAULT 'INR',
                status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                transaction_id VARCHAR(128),
                utr VARCHAR(128),
                upi_reference VARCHAR(128),
                payer_name VARCHAR(255),
                verified_by VARCHAR(32),
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                webhook_received INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                merchant_webhook_url TEXT,
                UNIQUE(merchant_id, idempotency_key)
            )
        """)
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS utr VARCHAR(128)")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payer_name VARCHAR(255)")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS verified_by VARCHAR(32)")

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_o1 ON orders(order_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_o2 ON orders(transaction_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_o3 ON orders(utr)")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_utr ON orders(utr) WHERE utr IS NOT NULL")


@asynccontextmanager
async def lifespan(app):
    try:
        await init_db()
        logger.info("DB ready")
    except Exception as e:
        logger.warning("DB init skipped: %s", e)

    email_task = None
    if GMAIL_EMAIL and GMAIL_APP_PASSWORD:
        email_task = asyncio.create_task(scan_gmail_for_payments())

    yield

    if email_task:
        email_task.cancel()
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ============================================================
# APP
# ============================================================

app = FastAPI(title="OS CODEX", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ============================================================
# HELPERS
# ============================================================

def now(): return datetime.now(timezone.utc)
def now_iso(): return now().isoformat()
def hash_key(v): return hashlib.sha256(v.encode()).hexdigest()


def generate_order_id():
    return "ORD_" + now().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(5).upper()


def generate_upi_qr(amount: float, order_id: str):
    params = {
        "pa": MERCHANT_UPI_ID, "pn": MERCHANT_NAME,
        "tr": order_id, "am": f"{amount:.2f}", "cu": "INR",
    }
    upi_uri = "upi://pay?" + urllib.parse.urlencode(params)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}", upi_uri


async def find_merchant_by_api_key(api_key: str) -> Optional[str]:
    if not api_key:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT merchant_id FROM merchants WHERE api_key_hash=$1",
            hash_key(api_key))
    return row["merchant_id"] if row else None


def order_to_dict(row):
    """Convert asyncpg row to a clean dict for API response."""
    if not row:
        return None
    return {
        "order_id": row["order_id"],
        "merchant_id": row["merchant_id"],
        "amount": float(row["amount"]),
        "currency": row["currency"],
        "status": row["status"],
        "transaction_id": row["transaction_id"],
        "utr": row["utr"],
        "upi_reference": row["upi_reference"],
        "payer_name": row["payer_name"],
        "verified_by": row["verified_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "expired": row["status"] == "EXPIRED",
    }


async def _mark_order_success(order_id: str, txn_id: str = None, source: str = "MANUAL",
                              utr: str = None, payer: str = None):
    """Mark order SUCCESS. Enforces duplicate UTR/TXN protection."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", order_id)
        if not order:
            return False, "Order not found"
        if order["status"] == "SUCCESS":
            return True, "Already SUCCESS"

        # duplicate checks
        if utr:
            dupe = await conn.fetchrow(
                "SELECT order_id FROM orders WHERE utr=$1 AND status='SUCCESS' AND order_id<>$2",
                utr, order_id)
            if dupe:
                return False, f"UTR already used for order {dupe['order_id']}"
        if txn_id:
            dupe = await conn.fetchrow(
                "SELECT order_id FROM orders WHERE transaction_id=$1 AND status='SUCCESS' AND order_id<>$2",
                txn_id, order_id)
            if dupe:
                return False, f"TXN already used for order {dupe['order_id']}"

        await conn.execute(
            """
            UPDATE orders
            SET status='SUCCESS',
                transaction_id = COALESCE($1, transaction_id),
                utr = COALESCE($2, utr),
                payer_name = COALESCE($3, payer_name),
                verified_by = $4,
                webhook_received = 1,
                verified = 1,
                updated_at = $5
            WHERE order_id = $6
            """,
            txn_id, utr, payer, source, now(), order_id)
        merchant_webhook = order["merchant_webhook_url"]
        amount = float(order["amount"])

    if merchant_webhook:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(merchant_webhook, json={
                    "event": "payment.success", "order_id": order_id,
                    "amount": amount, "currency": "INR",
                    "transaction_id": txn_id, "utr": utr, "payer_name": payer,
                    "status": "SUCCESS", "verified_by": source,
                    "timestamp": now_iso(), "gateway": BRAND, "developer": DEVELOPER,
                })
        except Exception as e:
            logger.warning("Merchant webhook failed: %s", e)

    await _notify_telegram_admin(
        order_id=order_id, amount=amount,
        transaction_id=txn_id or utr, payer=payer, source=source)
    return True, "OK"


async def _expire_order_if_needed(order_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", order_id)
        if not order:
            return None
        if order["status"] == "PENDING" and now() > order["expires_at"]:
            await conn.execute(
                "UPDATE orders SET status='EXPIRED', updated_at=$1 WHERE order_id=$2",
                now(), order_id)
            await _notify_telegram_expiry(order_id, float(order["amount"]))
            d = dict(order)
            d["status"] = "EXPIRED"
            return d
        return dict(order)


async def _find_order_by_any(order_id=None, utr=None, txn=None):
    """Look up order by order_id, utr, or transaction_id (priority in that order)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = None
        if order_id:
            row = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", order_id)
        if not row and utr:
            row = await conn.fetchrow("SELECT * FROM orders WHERE utr=$1", utr)
        if not row and txn:
            row = await conn.fetchrow("SELECT * FROM orders WHERE transaction_id=$1", txn)
        return dict(row) if row else None


# ============================================================
# 1. HEALTH
# ============================================================

@app.get("/api/health")
async def health():
    return {
        "success": True, "service": BRAND, "powered_by": POWERED_BY,
        "developer": DEVELOPER, "status": "online",
        "features": {
            "email_auto_verify": bool(GMAIL_EMAIL and GMAIL_APP_PASSWORD),
            "gmail_account": GMAIL_EMAIL if GMAIL_EMAIL else None,
            "telegram_notify": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID),
            "manual_utr": True,
            "utr_window_minutes": PAYMENT_EXPIRY_MINUTES,
            "expiry_minutes": PAYMENT_EXPIRY_MINUTES,
        },
    }


@app.get("/api/db-test")
async def db_test():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
        return {"success": True, "db": "connected", "value": val}
    except Exception as e:
        return {"success": False, "db": "error", "message": str(e)}


# ============================================================
# 2. CREATE MERCHANT
# ============================================================

@app.get("/api/admin/create-merchant")
async def create_merchant(
    admin_key: str = Query(...),
    name: str = Query(...),
    webhook_url: Optional[str] = Query(None),
):
    if admin_key != ADMIN_KEY:
        raise HTTPException(401, {"code": "INVALID_ADMIN_KEY", "message": "Wrong admin key"})

    raw_key = "key_live_" + secrets.token_hex(24)
    merchant_id = "MERCH_" + secrets.token_hex(6).upper()

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO merchants (merchant_id, api_key_hash, name, webhook_url, created_at)
               VALUES ($1,$2,$3,$4,$5)""",
            merchant_id, hash_key(raw_key), name, webhook_url, now())

    return {"success": True, "merchant_id": merchant_id, "api_key": raw_key, "name": name,
            "gateway": BRAND, "developer": DEVELOPER}


@app.get("/api/admin/merchants")
async def list_merchants(admin_key: str = Query(...)):
    if admin_key != ADMIN_KEY:
        raise HTTPException(401, {"code": "INVALID_ADMIN_KEY", "message": "Wrong admin key"})
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT merchant_id, name, webhook_url, created_at FROM merchants ORDER BY id DESC")
    return {"success": True, "count": len(rows), "merchants": [
        {"merchant_id": r["merchant_id"], "name": r["name"],
         "webhook_url": r["webhook_url"], "created_at": r["created_at"].isoformat()}
        for r in rows]}


@app.get("/api/admin/orders")
async def list_orders(
    admin_key: str = Query(...),
    status: Optional[str] = Query(None),
    limit: int = Query(100),
):
    if admin_key != ADMIN_KEY:
        raise HTTPException(401, {"code": "INVALID_ADMIN_KEY", "message": "Wrong admin key"})
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE status=$1 ORDER BY id DESC LIMIT $2",
                status.upper(), limit)
        else:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY id DESC LIMIT $1", limit)
    return {"success": True, "count": len(rows), "orders": [order_to_dict(r) for r in rows]}


@app.get("/api/admin/stats")
async def admin_stats(admin_key: str = Query(...)):
    if admin_key != ADMIN_KEY:
        raise HTTPException(401, {"code": "INVALID_ADMIN_KEY", "message": "Wrong admin key"})
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        success_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='SUCCESS'")
        pending_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='PENDING'")
        expired_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='EXPIRED'")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='SUCCESS'")
        today_revenue = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='SUCCESS' AND created_at > NOW() - INTERVAL '1 day'")
        week_revenue = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='SUCCESS' AND created_at > NOW() - INTERVAL '7 days'")
    return {"success": True, "stats": {
        "total_orders": total_orders, "success_orders": success_orders,
        "pending_orders": pending_orders, "expired_orders": expired_orders,
        "total_revenue": float(total_revenue or 0),
        "today_revenue": float(today_revenue or 0),
        "week_revenue": float(week_revenue or 0),
    }, "gateway": BRAND, "developer": DEVELOPER}


# ============================================================
# 3. CREATE PAYMENT
# ============================================================

@app.get("/api/pay/create")
async def create_payment_get(
    request: Request,
    amount: float = Query(...),
    api_key: str = Query(...),
    idempotency_key: Optional[str] = Query(None),
):
    merchant_id = await find_merchant_by_api_key(api_key)
    if not merchant_id:
        raise HTTPException(401, {"code": "INVALID_API_KEY", "message": "Invalid api_key"})
    if amount <= 0:
        raise HTTPException(400, {"code": "INVALID_AMOUNT", "message": "amount must be > 0"})
    if amount > MAX_PAYMENT_AMOUNT:
        raise HTTPException(400, {"code": "AMOUNT_EXCEEDED", "message": "amount too large"})
    return await _create_order(merchant_id, amount, idempotency_key, request)


class PaymentBody(BaseModel):
    api_key: str
    amount: float
    idempotency_key: Optional[str] = None


@app.post("/api/pay/create")
async def create_payment_post(body: PaymentBody, request: Request):
    merchant_id = await find_merchant_by_api_key(body.api_key)
    if not merchant_id:
        raise HTTPException(401, {"code": "INVALID_API_KEY", "message": "Invalid api_key"})
    if body.amount <= 0:
        raise HTTPException(400, {"code": "INVALID_AMOUNT", "message": "amount must be > 0"})
    if body.amount > MAX_PAYMENT_AMOUNT:
        raise HTTPException(400, {"code": "AMOUNT_EXCEEDED", "message": "amount too large"})
    return await _create_order(merchant_id, body.amount, body.idempotency_key, request)


async def _create_order(merchant_id, amount, idempotency_key, request):
    pool = await get_pool()
    base_url = str(request.base_url).rstrip("/")

    async with pool.acquire() as conn:
        if idempotency_key:
            existing = await conn.fetchrow(
                "SELECT * FROM orders WHERE merchant_id=$1 AND idempotency_key=$2",
                merchant_id, idempotency_key)
            if existing:
                qr, upi = generate_upi_qr(float(existing["amount"]), existing["order_id"])
                return {"success": True, "order_id": existing["order_id"],
                        "amount": float(existing["amount"]), "currency": "INR",
                        "status": existing["status"],
                        "payment_url": f"{base_url}/pay/{existing['order_id']}",
                        "upi_uri": upi, "qr_code": qr,
                        "gateway": BRAND, "developer": DEVELOPER}

        merchant = await conn.fetchrow("SELECT webhook_url FROM merchants WHERE merchant_id=$1", merchant_id)
        if not merchant:
            raise HTTPException(404, {"code": "MERCHANT_NOT_FOUND", "message": "Merchant not found"})

        order_id = generate_order_id()
        created = now()
        expires = created + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

        await conn.execute(
            """INSERT INTO orders
               (order_id, merchant_id, idempotency_key, amount, currency, status,
                created_at, updated_at, expires_at, merchant_webhook_url)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            order_id, merchant_id, idempotency_key, amount, "INR", "PENDING",
            created, created, expires, merchant["webhook_url"])

    qr, upi = generate_upi_qr(amount, order_id)
    return {"success": True, "order_id": order_id, "amount": amount, "currency": "INR",
            "status": "PENDING", "payment_url": f"{base_url}/pay/{order_id}",
            "upi_uri": upi, "qr_code": qr,
            "expires_at": expires.isoformat(),
            "expiry_minutes": PAYMENT_EXPIRY_MINUTES,
            "gateway": BRAND, "developer": DEVELOPER}


# ============================================================
# 4. STATUS
# ============================================================

@app.get("/api/pay/status")
async def payment_status(order_id: str = Query(...)):
    order = await _expire_order_if_needed(order_id)
    if not order:
        raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": "Order not found"})
    st = order["status"]
    # compute seconds remaining
    expires_at = order["expires_at"]
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            expires_at = None
    remaining = 0
    if expires_at and st == "PENDING":
        remaining = max(0, int((expires_at - now()).total_seconds()))

    return {"success": True, "order_id": order["order_id"],
            "amount": float(order["amount"]), "currency": order["currency"],
            "status": st,
            "transaction_id": order["transaction_id"],
            "utr": order["utr"],
            "upi_reference": order["upi_reference"],
            "payer_name": order["payer_name"],
            "verified_by": order["verified_by"],
            "verified": True if st == "SUCCESS" else bool(order["verified"]),
            "created_at": order["created_at"].isoformat() if hasattr(order["created_at"], "isoformat") else order["created_at"],
            "expires_at": order["expires_at"].isoformat() if hasattr(order["expires_at"], "isoformat") else order["expires_at"],
            "seconds_remaining": remaining,
            "expired": st == "EXPIRED",
            "can_verify": st == "PENDING",
            "gateway": BRAND, "developer": DEVELOPER}


# ============================================================
# 5. LOOKUP (info only, no verification)
# ============================================================

@app.get("/api/pay/lookup")
async def pay_lookup(
    order_id: Optional[str] = Query(None),
    utr: Optional[str] = Query(None),
    transaction_id: Optional[str] = Query(None),
):
    """Look up full payment details WITHOUT changing status."""
    if not (order_id or utr or transaction_id):
        raise HTTPException(400, {"code": "MISSING_PARAM",
                                  "message": "Provide order_id, utr, or transaction_id"})

    order = await _find_order_by_any(order_id, utr, transaction_id)
    if not order:
        raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": "No matching order"})

    # auto-expire on read
    if order["status"] == "PENDING":
        exp = order["expires_at"]
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except Exception:
                exp = None
        if exp and now() > exp:
            order = await _expire_order_if_needed(order["order_id"])
            if not order:
                raise HTTPException(500, {"code": "ERROR", "message": "Expire failed"})

    return {"success": True, "payment": order_to_dict(order),
            "gateway": BRAND, "developer": DEVELOPER}


# ============================================================
# 6. VERIFY WITH UTR / TXN / ORDER ID (15-min window)
# ============================================================

class VerifyBody(BaseModel):
    order_id: Optional[str] = None
    utr: Optional[str] = None
    transaction_id: Optional[str] = None
    secret: Optional[str] = None
    amount: Optional[float] = None     # optional — pass to enforce amount match


@app.post("/api/pay/verify-utr")
async def verify_utr(body: VerifyBody):
    """
    Verify a payment using order_id, utr, or transaction_id.

    Rules:
      - Verification only allowed within PAYMENT_EXPIRY_MINUTES (15 min)
      - If order is EXPIRED / older than 15 min → info-only + expired flag
      - Duplicate UTR / TXN rejected
      - Amount can optionally be checked against the order
      - Response always includes full payment details
    """
    if WEBHOOK_SECRET and body.secret != WEBHOOK_SECRET:
        raise HTTPException(401, {"code": "INVALID_SECRET", "message": "Wrong secret"})

    order_id = (body.order_id or "").strip()
    utr = (body.utr or "").strip()
    txn = (body.transaction_id or "").strip()

    if not (order_id or utr or txn):
        raise HTTPException(400, {"code": "MISSING_PARAM",
                                  "message": "Provide order_id, utr, or transaction_id"})

    if utr and len(utr) < 6:
        raise HTTPException(400, {"code": "INVALID_UTR", "message": "UTR too short (min 6 chars)"})

    # 1. Find the order
    order = await _find_order_by_any(order_id, utr, txn)
    if not order:
        raise HTTPException(404, {"code": "ORDER_NOT_FOUND",
                                  "message": "No order found for the provided identifier"})

    # 2. Auto-expire if needed
    if order["status"] == "PENDING":
        exp = order["expires_at"]
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except Exception:
                exp = None
        if exp and now() > exp:
            order = await _expire_order_if_needed(order["order_id"])
            if not order:
                order = await _find_order_by_any(order_id=order["order_id"]) if order else None

    current_order_id = order["order_id"]
    status = order["status"]

    # 3. Already success → return details
    if status == "SUCCESS":
        return {
            "success": True,
            "verified": True,
            "message": "Payment already verified",
            "payment": order_to_dict(order),
            "gateway": BRAND, "developer": DEVELOPER,
        }

    # 4. Expired / failed → info only
    if status in ("EXPIRED", "FAILED"):
        return {
            "success": True,
            "verified": False,
            "expired": True,
            "message": f"Order is {status} — verification window closed ({PAYMENT_EXPIRY_MINUTES} min). Info shown only.",
            "payment": order_to_dict(order),
            "gateway": BRAND, "developer": DEVELOPER,
        }

    # 5. Amount check (optional)
    if body.amount is not None:
        if abs(float(order["amount"]) - float(body.amount)) > 0.001:
            return {
                "success": False,
                "verified": False,
                "message": f"Amount mismatch: order is ₹{float(order['amount']):.2f}, submitted ₹{float(body.amount):.2f}",
                "payment": order_to_dict(order),
                "gateway": BRAND, "developer": DEVELOPER,
            }

    # 6. Attempt verification
    final_txn = txn or order["transaction_id"] or f"MANUAL_{int(now().timestamp())}"
    final_utr = utr or order["utr"]

    ok, msg = await _mark_order_success(
        order_id=current_order_id,
        txn_id=final_txn,
        source="UTR",
        utr=final_utr,
    )

    # refresh
    order = await _find_order_by_any(order_id=current_order_id)

    if not ok:
        return {
            "success": False,
            "verified": False,
            "message": msg,
            "payment": order_to_dict(order),
            "gateway": BRAND, "developer": DEVELOPER,
        }

    return {
        "success": True,
        "verified": True,
        "message": "Payment verified successfully",
        "payment": order_to_dict(order),
        "gateway": BRAND, "developer": DEVELOPER,
    }


# ============================================================
# 7. VERIFY (GET) — paste-in-browser
# ============================================================

@app.get("/api/pay/verify")
async def verify_get(
    order_id: Optional[str] = Query(None),
    utr: Optional[str] = Query(None),
    transaction_id: Optional[str] = Query(None),
    amount: Optional[float] = Query(None),
    secret: Optional[str] = Query(None),
):
    return await verify_utr(VerifyBody(
        order_id=order_id, utr=utr, transaction_id=transaction_id,
        amount=amount, secret=secret,
    ))


# ============================================================
# 8. WEBHOOK (PSP)
# ============================================================

class WebhookBody(BaseModel):
    order_id: str
    transaction_id: str
    amount: float
    status: str
    utr: Optional[str] = None
    upi_reference: Optional[str] = None
    payer_name: Optional[str] = None
    secret: Optional[str] = None


@app.post("/api/pay/webhook")
async def provider_webhook(body: WebhookBody):
    if WEBHOOK_SECRET and body.secret != WEBHOOK_SECRET:
        raise HTTPException(401, {"code": "INVALID_SECRET", "message": "Wrong webhook secret"})

    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", body.order_id)
        if not order:
            raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": "Order not found"})
        if abs(float(order["amount"]) - float(body.amount)) > 0.001:
            raise HTTPException(400, {"code": "AMOUNT_MISMATCH", "message": "Amount mismatch"})
        if order["webhook_received"] == 1 and order["transaction_id"] == body.transaction_id:
            return {"success": True, "message": "Already processed"}

        if body.status == "SUCCESS":
            final, verified = "SUCCESS", 1
        elif body.status in ("FAILED", "FAILURE", "CANCELLED"):
            final, verified = "FAILED", 1
        else:
            final, verified = "PENDING", 0

        await conn.execute(
            """UPDATE orders
               SET status=$1, transaction_id=$2, utr=COALESCE($3,utr),
                   upi_reference=$4, payer_name=$5, verified_by='WEBHOOK',
                   webhook_received=1, verified=$6, updated_at=$7
               WHERE order_id=$8""",
            final, body.transaction_id, body.utr, body.upi_reference,
            body.payer_name, verified, now(), body.order_id)

    if final == "SUCCESS":
        await _notify_telegram_admin(
            order_id=body.order_id, amount=float(order["amount"]),
            transaction_id=body.transaction_id, payer=body.payer_name, source="Webhook")

    return {"success": True, "message": "Webhook processed",
            "order_id": body.order_id, "status": final,
            "gateway": BRAND, "developer": DEVELOPER}


# ============================================================
# 9. GMAIL AUTO-VERIFY (amount detection)
# ============================================================

def _decode_email_header(raw):
    parts = decode_header(raw or "")
    out = ""
    for content, enc in parts:
        out += content.decode(enc or "utf-8", errors="ignore") if isinstance(content, bytes) else content
    return out


def _extract_email_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    continue
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return ""


async def scan_gmail_for_payments():
    while True:
        try:
            await _check_emails_once()
        except Exception as e:
            logger.warning("Email scan failed: %s", e)
        await asyncio.sleep(EMAIL_CHECK_INTERVAL)


async def _check_emails_once():
    def _imap_work():
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
        M.select("INBOX")
        typ, data = M.search(None, f'(UNSEEN FROM "{FAMAPP_SENDER}")')
        if typ != "OK" or not data or not data[0]:
            M.logout()
            return []
        results = []
        for num in data[0].split():
            typ, msg_data = M.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_email_header(msg.get("Subject", ""))
            body = _extract_email_body(msg)
            full = f"{subject}\n{body}"

            amt_match = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", full)
            if not amt_match:
                continue
            amount = float(amt_match.group(1).replace(",", ""))

            txn_match = re.search(r"(?:Transaction\s*ID|Txn|UTR|Ref(?:erence)?)[\s:]+([A-Z0-9]+)", full, re.I)
            txn_id = txn_match.group(1) if txn_match else f"EML_{int(now().timestamp())}"

            utr_match = re.search(r"UTR[\s:]+([A-Z0-9]+)", full, re.I)
            utr = utr_match.group(1) if utr_match else None

            ref_match = re.search(r"UPI[\s:]*([A-Z0-9]+)", full, re.I)
            upi_ref = ref_match.group(1) if ref_match else None

            from_match = re.search(r"from\s+([A-Za-z][A-Za-z .]+)", body)
            payer = from_match.group(1).strip() if from_match else None

            results.append({
                "amount": amount, "transaction_id": txn_id, "utr": utr,
                "upi_reference": upi_ref, "payer": payer, "email_uid": num.decode(),
            })
        for r in results:
            try:
                M.store(r["email_uid"], "+FLAGS", "\\Seen")
            except Exception:
                pass
        M.logout()
        return results

    emails = await asyncio.to_thread(_imap_work)
    if not emails:
        return
    pool = await get_pool()
    for em in emails:
        async with pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT 1 FROM orders WHERE transaction_id=$1 OR (utr IS NOT NULL AND utr=$2)",
                em["transaction_id"], em["utr"])
            if exists:
                continue
            # Match by EXACT amount
            order = await conn.fetchrow(
                """
                SELECT * FROM orders
                WHERE status='PENDING'
                  AND amount = $1
                  AND created_at > NOW() - INTERVAL '30 minutes'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                em["amount"])
            if not order:
                logger.info("Email (₹%.2f) — no matching PENDING order", em["amount"])
                continue
        await _mark_order_success(
            order_id=order["order_id"], txn_id=em["transaction_id"],
            source="EMAIL", utr=em["utr"], payer=em["payer"])
        logger.info("Auto-verified %s from email", order["order_id"])


@app.get("/api/email-test")
async def email_test():
    if not (GMAIL_EMAIL and GMAIL_APP_PASSWORD):
        return {"success": False, "error": "Gmail not configured"}
    try:
        await _check_emails_once()
        return {"success": True, "message": "Scan complete"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
# 10. TELEGRAM NOTIFY
# ============================================================

async def _notify_telegram_admin(order_id, amount, transaction_id=None,
                                 upi_reference=None, payer=None, source=None):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID):
        return
    text = f"💰 *PAYMENT RECEIVED*\n\n🆔 Order: `{order_id}`\n💵 Amount: *₹{amount:.2f}*\n"
    if payer: text += f"👤 From: {payer}\n"
    if transaction_id: text += f"🧾 Txn: `{transaction_id}`\n"
    if upi_reference: text += f"📎 UPI Ref: `{upi_reference}`\n"
    if source: text += f"✅ Verified via: *{source}*\n"
    text += f"\n— {BRAND} · {DEVELOPER}"
    try:
        async with httpx.AsyncClient(timeout=10) as tg:
            await tg.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": int(TELEGRAM_ADMIN_ID), "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        logger.warning("Telegram notify failed: %s", e)


async def _notify_telegram_expiry(order_id, amount):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID):
        return
    text = (f"⌛ *PAYMENT EXPIRED*\n\n🆔 Order: `{order_id}`\n💵 Amount: *₹{amount:.2f}*\n"
            f"⏱️ Window: {PAYMENT_EXPIRY_MINUTES} minutes\n❌ Verification window closed\n\n— {BRAND} · {DEVELOPER}")
    try:
        async with httpx.AsyncClient(timeout=10) as tg:
            await tg.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": int(TELEGRAM_ADMIN_ID), "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        logger.warning("Telegram expiry notify failed: %s", e)


# ============================================================
# 11. PAYMENT PAGE
# ============================================================

@app.get("/pay/{order_id}", response_class=HTMLResponse)
async def payment_page(order_id: str):
    order = await _expire_order_if_needed(order_id)
    if not order:
        return HTMLResponse("<h2>Payment order not found</h2>", status_code=404)

    qr, upi_uri = generate_upi_qr(float(order["amount"]), order_id)
    status = order["status"]
    is_active = status == "PENDING"
    is_success = status == "SUCCESS"
    is_expired = status in ("EXPIRED", "FAILED")

    expires_at = order["expires_at"]
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except Exception:
            expires_at = None

    secs = max(0, int((expires_at - now()).total_seconds())) if (is_active and expires_at) else 0

    created_str = order["created_at"].strftime("%d %b %Y, %I:%M %p UTC") if hasattr(order["created_at"], "strftime") else str(order["created_at"])
    expires_str = expires_at.strftime("%d %b %Y, %I:%M %p UTC") if expires_at else "—"

    if is_success:
        top_block = f"""
        <div class="result success-block">
          <div class="result-icon">✅</div>
          <div class="result-title">Payment Successful</div>
          <div class="result-amount">₹{float(order["amount"]):.2f}</div>
          <div class="result-sub">Verified via {order["verified_by"] or "—"}</div>
        </div>
        """
    elif is_expired:
        top_block = f"""
        <div class="result expired-block">
          <div class="result-icon">⌛</div>
          <div class="result-title">Payment Expired</div>
          <div class="result-amount">₹{float(order["amount"]):.2f}</div>
          <div class="result-sub">Verification window ({PAYMENT_EXPIRY_MINUTES} min) closed. Details shown below.</div>
        </div>
        """
    else:
        top_block = f"""
        <div class="qr-wrap">
          <img class="qr" src="{qr}" alt="UPI QR">
          <a class="pay" href="{upi_uri}">Pay with UPI App</a>
          <div class="timer">⏱️ Time remaining: <b id="timer">{secs}</b>s</div>

          <div class="box">
            <h4>✍️ Already paid? Enter your UTR / TXN</h4>
            <input id="utrInput" type="text" placeholder="UTR / Transaction ID" autocomplete="off">
            <button onclick="submitUTR()">Verify Payment</button>
            <small>UTR is the 12-digit number shown in your UPI app after payment.</small>
            <div id="utrMsg" style="margin-top:10px;font-size:13px;"></div>
          </div>
        </div>
        """

    return HTMLResponse(f"""
<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{BRAND} — Payment Details</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:linear-gradient(135deg,#0f172a,#1e293b);
font-family:'Segoe UI',Arial,sans-serif;color:#e2e8f0;
display:flex;justify-content:center;align-items:center;min-height:100vh;padding:16px}}
.card{{width:100%;max-width:430px;background:#111827;padding:26px;border-radius:20px;
box-shadow:0 20px 60px rgba(0,0,0,.6);border:1px solid #1f2937}}
.brand{{font-size:12px;letter-spacing:2px;color:#64748b;margin-bottom:4px;text-align:center}}
.brand b{{color:#a855f7}}
h2{{margin:6px 0 18px;text-align:center;font-size:22px}}
.details{{background:#0b1220;border:1px solid #1f2937;border-radius:12px;padding:16px;margin-bottom:18px;font-size:13px}}
.row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1f2937}}
.row:last-child{{border-bottom:none}}
.row .k{{color:#94a3b8}}
.row .v{{color:#e2e8f0;font-weight:600;text-align:right;max-width:60%;word-break:break-all}}
.row .v.ok{{color:#10b981}}
.row .v.bad{{color:#ef4444}}
.row .v.warn{{color:#facc15}}
.amount{{font-size:36px;font-weight:800;color:#10b981;text-align:center;margin:6px 0 12px}}
.qr-wrap{{text-align:center}}
.qr{{width:220px;max-width:85%;border-radius:12px;background:#fff;padding:10px}}
.pay{{display:block;margin:18px 0;padding:15px;background:linear-gradient(90deg,#a855f7,#ec4899);
color:#fff;text-decoration:none;border-radius:12px;font-weight:700}}
.timer{{margin:8px 0;font-size:14px;color:#facc15}}
.timer b{{font-size:16px}}
.box{{margin-top:18px;padding:16px;background:#1e293b;border-radius:12px;text-align:left}}
.box h4{{margin:0 0 10px;color:#a855f7;font-size:14px}}
.box input{{width:100%;padding:12px;border-radius:8px;border:1px solid #334155;
background:#0f172a;color:#e2e8f0;font-size:14px}}
.box button{{width:100%;margin-top:10px;padding:12px;border:0;border-radius:8px;
background:#a855f7;color:#fff;font-weight:700;cursor:pointer;font-size:14px}}
.box button:hover{{background:#9333ea}}
.box small{{color:#64748b;font-size:11px;display:block;margin-top:6px}}
.result{{padding:22px;border-radius:14px;text-align:center;margin-bottom:18px}}
.success-block{{background:rgba(16,185,129,.08);border:1px solid #10b981}}
.expired-block{{background:rgba(239,68,68,.08);border:1px solid #ef4444}}
.result-icon{{font-size:40px;margin-bottom:8px}}
.result-title{{font-size:18px;font-weight:700;letter-spacing:.5px}}
.success-block .result-title{{color:#10b981}}
.expired-block .result-title{{color:#ef4444}}
.result-amount{{font-size:32px;font-weight:800;color:#e2e8f0;margin:8px 0}}
.result-sub{{font-size:13px;color:#94a3b8;margin-top:6px}}
.footer{{margin-top:20px;font-size:12px;color:#64748b;text-align:center}}
</style></head><body>
<div class="card">
  <div class="brand">OS <b>CODEX</b></div>
  <h2>{MERCHANT_NAME}</h2>

  <div class="amount">₹{float(order["amount"]):.2f}</div>

  {top_block}

  <div class="details">
    <div class="row"><span class="k">Order ID</span><span class="v">{order_id}</span></div>
    <div class="row"><span class="k">Status</span>
      <span class="v {'ok' if is_success else 'bad' if is_expired else 'warn'}" id="statusText">{status}</span>
    </div>
    <div class="row"><span class="k">UPI ID</span><span class="v">{MERCHANT_UPI_ID}</span></div>
    <div class="row"><span class="k">Created</span><span class="v">{created_str}</span></div>
    <div class="row"><span class="k">Expires</span><span class="v">{expires_str}</span></div>
    {f'<div class="row"><span class="k">Transaction ID</span><span class="v">{order["transaction_id"]}</span></div>' if order["transaction_id"] else ''}
    {f'<div class="row"><span class="k">UTR</span><span class="v">{order["utr"]}</span></div>' if order["utr"] else ''}
    {f'<div class="row"><span class="k">Payer</span><span class="v">{order["payer_name"]}</span></div>' if order["payer_name"] else ''}
    {f'<div class="row"><span class="k">Verified Via</span><span class="v">{order["verified_by"]}</span></div>' if order["verified_by"] else ''}
  </div>

  <div class="footer">{BRAND} · Developer {DEVELOPER}</div>
</div>

<script>
const ORDER_ID = "{order_id}";
let remaining = {secs};
let timerHandle = null;
const isActive = {str(is_active).lower()};

function startTimer() {{
  const el = document.getElementById("timer");
  if (!el) return;
  timerHandle = setInterval(() => {{
    remaining -= 1;
    if (remaining <= 0) {{ clearInterval(timerHandle); location.reload(); return; }}
    el.innerText = remaining;
    if (remaining < 60) el.style.color = "#ef4444";
  }}, 1000);
}}

async function checkStatus() {{
  try {{
    const r = await fetch("/api/pay/status?order_id=" + ORDER_ID);
    const d = await r.json();
    if (!d.success) return;
    const sEl = document.getElementById("statusText");
    if (sEl && sEl.innerText !== d.status) {{
      sEl.innerText = d.status;
      if (["SUCCESS","EXPIRED","FAILED"].includes(d.status)) {{
        setTimeout(() => location.reload(), 800);
      }}
    }}
  }} catch(e) {{}}
}}

async function submitUTR() {{
  const input = document.getElementById("utrInput");
  const msg = document.getElementById("utrMsg");
  const utr = input.value.trim();
  if (utr.length < 6) {{
    msg.innerHTML = '<span style="color:#ef4444">❌ UTR too short — min 6 characters</span>';
    return;
  }}
  msg.innerHTML = '<span style="color:#facc15">⏳ Verifying...</span>';
  try {{
    const r = await fetch("/api/pay/verify-utr", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ order_id: ORDER_ID, utr: utr, secret: "change_me_random_secret" }})
    }});
    const d = await r.json();
    if (d.success && d.verified) {{
      msg.innerHTML = '<span style="color:#10b981">✅ Verified! Refreshing...</span>';
      setTimeout(() => location.reload(), 1200);
    }} else {{
      msg.innerHTML = '<span style="color:#ef4444">❌ ' + (d.message || "Verification failed") + '</span>';
    }}
  }} catch(e) {{
    msg.innerHTML = '<span style="color:#ef4444">❌ Network error</span>';
  }}
}}

if (isActive && remaining > 0) startTimer();
if (isActive) setInterval(checkStatus, 4000);
</script>
</body></html>
""")


# ============================================================
# ERROR HANDLER
# ============================================================

@app.exception_handler(Exception)
async def err_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        content={"success": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}},
        status_code=500,
    )
