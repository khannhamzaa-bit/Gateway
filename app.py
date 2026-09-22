import os
import hmac
import hashlib
import secrets
import json
import logging
import socket
import urllib.parse
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import Optional

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
# OS GATEWAY — UPI PAYMENT GATEWAY
# Powered by MODX · Developer: @MODX
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OS-GATEWAY")


# ============================================================
# ⚙️ HARDCODED CONFIG — EDIT ONLY THIS BLOCK
# ============================================================

# 🔴 REPLACE npg_USgbsX5pDRO4 WITH YOUR NEW NEON PASSWORD
DATABASE_URL = "postgresql://neondb_owner:npg_USgbsX5pDRO4@ep-bold-art-avpvzvhx-pooler.c-11.us-east-1.aws.neon.tech/neondb"

MERCHANT_NAME   = "HAMZA KHAN"
MERCHANT_UPI_ID = "khannhamzaa@fam"

ADMIN_KEY      = "os-gateway@123"
WEBHOOK_SECRET = "change_me_random_secret"

MAX_PAYMENT_AMOUNT     = 100000.0
PAYMENT_EXPIRY_MINUTES = 15

ALLOWED_ORIGINS = ["*"]

# ============================================================
# END CONFIG
# ============================================================


# ============================================================
# 🛠️ DNS FIX FOR VERCEL (forces IPv4 lookups)
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
        # Strip ALL query params — asyncpg doesn't parse libpq options
        dsn = DATABASE_URL.split("?")[0]

        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=3,
            ssl="require",
            command_timeout=20,
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
                upi_reference VARCHAR(128),
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                webhook_received INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                merchant_webhook_url TEXT,
                UNIQUE(merchant_id, idempotency_key)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_o1 ON orders(order_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_o2 ON orders(transaction_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_o3 ON orders(merchant_id)")


@asynccontextmanager
async def lifespan(app):
    try:
        await init_db()
        logger.info("DB ready")
    except Exception as e:
        logger.warning("DB init skipped: %s", e)
    yield
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ============================================================
# APP
# ============================================================

app = FastAPI(title="OS GATEWAY", version="2.0.0", lifespan=lifespan)

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
        "pa": MERCHANT_UPI_ID,
        "pn": MERCHANT_NAME,
        "tr": order_id,
        "am": f"{amount:.2f}",
        "cu": "INR",
    }
    upi_uri = "upi://pay?" + urllib.parse.urlencode(params)
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8, border=4,
    )
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
            hash_key(api_key),
        )
    return row["merchant_id"] if row else None


# ============================================================
# 1. HEALTH
# ============================================================

@app.get("/api/health")
async def health():
    return {
        "success": True,
        "service": "OS GATEWAY",
        "powered_by": "MODX",
        "developer": "@MODX",
        "status": "online",
    }


# ============================================================
# 1b. DB TEST
# ============================================================

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
            """
            INSERT INTO merchants (merchant_id, api_key_hash, name, webhook_url, created_at)
            VALUES ($1,$2,$3,$4,$5)
            """,
            merchant_id, hash_key(raw_key), name, webhook_url, now(),
        )

    return {
        "success": True,
        "merchant_id": merchant_id,
        "api_key": raw_key,
        "name": name,
        "gateway": "OS GATEWAY",
        "developer": "@MODX",
    }


# ============================================================
# 3. LIST MERCHANTS
# ============================================================

@app.get("/api/admin/merchants")
async def list_merchants(admin_key: str = Query(...)):
    if admin_key != ADMIN_KEY:
        raise HTTPException(401, {"code": "INVALID_ADMIN_KEY", "message": "Wrong admin key"})

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT merchant_id, name, webhook_url, created_at FROM merchants ORDER BY id DESC"
        )

    return {
        "success": True,
        "count": len(rows),
        "merchants": [
            {
                "merchant_id": r["merchant_id"],
                "name": r["name"],
                "webhook_url": r["webhook_url"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# ============================================================
# 4. CREATE PAYMENT — GET
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


# ============================================================
# 5. CREATE PAYMENT — POST
# ============================================================

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
                merchant_id, idempotency_key,
            )
            if existing:
                qr, upi = generate_upi_qr(float(existing["amount"]), existing["order_id"])
                return {
                    "success": True,
                    "order_id": existing["order_id"],
                    "amount": float(existing["amount"]),
                    "currency": "INR",
                    "status": existing["status"],
                    "payment_url": f"{base_url}/pay/{existing['order_id']}",
                    "upi_uri": upi,
                    "qr_code": qr,
                }

        merchant = await conn.fetchrow(
            "SELECT webhook_url FROM merchants WHERE merchant_id=$1", merchant_id
        )
        if not merchant:
            raise HTTPException(404, {"code": "MERCHANT_NOT_FOUND", "message": "Merchant not found"})

        order_id = generate_order_id()
        created = now()
        expires = created + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

        await conn.execute(
            """
            INSERT INTO orders
            (order_id, merchant_id, idempotency_key, amount, currency, status,
             created_at, updated_at, expires_at, merchant_webhook_url)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            order_id, merchant_id, idempotency_key, amount, "INR", "PENDING",
            created, created, expires, merchant["webhook_url"],
        )

    qr, upi = generate_upi_qr(amount, order_id)

    return {
        "success": True,
        "order_id": order_id,
        "amount": amount,
        "currency": "INR",
        "status": "PENDING",
        "payment_url": f"{base_url}/pay/{order_id}",
        "upi_uri": upi,
        "qr_code": qr,
        "gateway": "OS GATEWAY",
        "developer": "@MODX",
    }


# ============================================================
# 6. STATUS
# ============================================================

@app.get("/api/pay/status")
async def payment_status(order_id: str = Query(...)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", order_id)
        if not order:
            raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": "Order not found"})

        st = order["status"]
        if st == "PENDING" and now() > order["expires_at"]:
            st = "EXPIRED"
            await conn.execute(
                "UPDATE orders SET status=$1, updated_at=$2 WHERE order_id=$3",
                "EXPIRED", now(), order_id,
            )

    return {
        "success": True,
        "order_id": order_id,
        "amount": float(order["amount"]),
        "currency": order["currency"],
        "status": st,
        "transaction_id": order["transaction_id"],
        "upi_reference": order["upi_reference"],
        "verified": True if st == "SUCCESS" else bool(order["verified"]),
    }


# ============================================================
# 7. PROVIDER WEBHOOK
# ============================================================

class WebhookBody(BaseModel):
    order_id: str
    transaction_id: str
    amount: float
    status: str
    upi_reference: Optional[str] = None
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
            """
            UPDATE orders
            SET status=$1, transaction_id=$2, upi_reference=$3,
                webhook_received=1, verified=$4, updated_at=$5
            WHERE order_id=$6
            """,
            final, body.transaction_id, body.upi_reference, verified, now(), body.order_id,
        )
        merchant_webhook = order["merchant_webhook_url"]

    if merchant_webhook and final == "SUCCESS":
        payload = {
            "event": "payment.success",
            "order_id": body.order_id,
            "amount": float(order["amount"]),
            "currency": "INR",
            "transaction_id": body.transaction_id,
            "upi_reference": body.upi_reference,
            "status": "SUCCESS",
            "timestamp": now_iso(),
            "gateway": "OS GATEWAY",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(merchant_webhook, json=payload)
        except Exception as e:
            logger.warning("Merchant webhook failed: %s", e)

    return {"success": True, "message": "Webhook processed"}


# ============================================================
# 8. PAYMENT PAGE
# ============================================================

@app.get("/pay/{order_id}", response_class=HTMLResponse)
async def payment_page(order_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", order_id)

    if not order:
        return HTMLResponse("<h2>Payment order not found</h2>", status_code=404)

    qr, upi_uri = generate_upi_qr(float(order["amount"]), order_id)

    return HTMLResponse(f"""
<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OS GATEWAY — UPI Payment</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:linear-gradient(135deg,#0f172a,#1e293b);
font-family:'Segoe UI',Arial,sans-serif;color:#e2e8f0;
display:flex;justify-content:center;align-items:center;min-height:100vh;padding:16px}}
.card{{width:100%;max-width:420px;background:#111827;padding:28px;border-radius:20px;
text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.6);border:1px solid #1f2937}}
.brand{{font-size:13px;letter-spacing:2px;color:#64748b;margin-bottom:6px}}
.brand b{{color:#a855f7}}
h2{{margin:6px 0 14px}}
.amount{{font-size:38px;font-weight:800;color:#10b981;margin:16px 0}}
.qr{{width:230px;max-width:85%;border-radius:12px;background:#fff;padding:10px}}
.pay{{display:block;margin:22px 0;padding:15px;background:linear-gradient(90deg,#a855f7,#ec4899);
color:#fff;text-decoration:none;border-radius:12px;font-weight:700}}
.status{{margin-top:10px;font-size:15px}}
.status span{{color:#facc15;font-weight:700}}
.footer{{margin-top:26px;font-size:12px;color:#64748b}}
</style></head><body>
<div class="card">
  <div class="brand">OS <b>GATEWAY</b></div>
  <h2>{MERCHANT_NAME}</h2>
  <div class="amount">₹{float(order["amount"]):.2f}</div>
  <p style="color:#94a3b8;font-size:13px">Order: {order_id}</p>
  <img class="qr" src="{qr}">
  <a class="pay" href="{upi_uri}">Pay with UPI App</a>
  <div class="status">Status: <span id="status">{order["status"]}</span></div>
  <div class="footer">OS GATEWAY · Powered by MODX</div>
</div>
<script>
async function check(){{
  try{{
    const r=await fetch("/api/pay/status?order_id={order_id}");
    const d=await r.json();
    if(d.success){{
      const el=document.getElementById("status");
      el.innerText=d.status;
      if(d.status==="SUCCESS")el.style.color="#10b981";
      else if(d.status==="FAILED"||d.status==="EXPIRED")el.style.color="#ef4444";
    }}
  }}catch(e){{}}
}}
setInterval(check,4000);
</script></body></html>
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
