"""
Verdant Backend API
FastAPI + SQLite + Razorpay + Retell AI + Kimi + Resend
"""

import os
import uuid
import json
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, EmailStr
import httpx
import aiosqlite
import resend

# ─── Configuration ───
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RETELL_API_KEY = os.getenv("RETELL_API_KEY", "")
RETELL_AGENT_ID = os.getenv("RETELL_AGENT_ID", "")
RETELL_FROM_NUMBER = os.getenv("RETELL_FROM_NUMBER", "")
KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://verdantai.xyz")
DB_PATH = os.getenv("DB_PATH", "verdant.db")

resend.api_key = RESEND_API_KEY
RETELL_BASE = "https://api.retell.ai/v2"

# ─── Pydantic Models ───
class UserRegister(BaseModel):
    email: EmailStr
    phone: str
    name: str

class PaymentInitiate(BaseModel):
    email: EmailStr

class PaymentVerify(BaseModel):
    email: EmailStr
    razorpay_payment_id: str
    razorpay_order_id: str
    razorpay_signature: str

class ScheduleCall(BaseModel):
    email: EmailStr
    preferred_time: str

# ─── Database ───
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                phone TEXT,
                name TEXT,
                payment_status TEXT DEFAULT 'pending',
                razorpay_order_id TEXT,
                razorpay_payment_id TEXT,
                call_status TEXT DEFAULT 'not_scheduled',
                call_id TEXT,
                transcript TEXT,
                report_status TEXT DEFAULT 'pending',
                report_content TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        await db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="Verdant API", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "https://www.verdantai.xyz", "https://verdant.pages.dev", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Health Check ───
@app.get("/health")
async def health():
    return {"status": "ok", "service": "verdant-api"}

# ─── User Registration ───
@app.post("/api/auth/register")
async def register(user: UserRegister):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, email FROM users WHERE email = ?", (user.email,))
        existing = await cursor.fetchone()
        if existing:
            return {"message": "Welcome back!", "user_id": existing[0], "existing": True}
        
        user_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO users (id, email, phone, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, user.email, user.phone, user.name, now, now)
        )
        await db.commit()
        return {"message": "Registered successfully", "user_id": user_id, "existing": False}

# ─── Razorpay: Create Order ───
@app.post("/api/payments/create-order")
async def create_order(req: PaymentInitiate):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(status_code=500, detail="Razorpay not configured")
    
    import base64
    credentials = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.razorpay.com/v1/orders",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json"
            },
            json={
                "amount": 1999,  # $19.99 in cents (actually paise for INR)
                "currency": "INR",
                "receipt": f"receipt_{uuid.uuid4().hex[:8]}",
                "notes": {"email": req.email}
            }
        )
        result = response.json()
    
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"]["description"])
    
    # Store order ID
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET razorpay_order_id = ?, updated_at = ? WHERE email = ?",
            (result["id"], datetime.utcnow().isoformat(), req.email)
        )
        await db.commit()
    
    return {
        "order_id": result["id"],
        "amount": result["amount"],
        "currency": result["currency"],
        "key_id": RAZORPAY_KEY_ID
    }

# ─── Razorpay: Verify Payment ───
@app.post("/api/payments/verify")
async def verify_payment(payment: PaymentVerify):
    # Verify signature
    message = f"{payment.razorpay_order_id}|{payment.razorpay_payment_id}"
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(expected_signature, payment.razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid payment signature")
    
    # Update user
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE users SET payment_status = 'confirmed', 
                razorpay_payment_id = ?, razorpay_order_id = ?, updated_at = ? 
                WHERE email = ?""",
            (payment.razorpay_payment_id, payment.razorpay_order_id, now, payment.email)
        )
        await db.commit()
    
    return {"message": "Payment verified successfully", "status": "confirmed"}

# ─── Get User ───
@app.get("/api/users/{email}")
async def get_user(email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, email, name, phone, payment_status, call_status, report_status, created_at FROM users WHERE email = ?",
            (email,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return dict(row)

# ─── Schedule Retell Call ───
@app.post("/api/calls/schedule")
async def schedule_call(schedule: ScheduleCall):
    if not RETELL_API_KEY or not RETELL_AGENT_ID or not RETELL_FROM_NUMBER:
        raise HTTPException(status_code=500, detail="Retell not configured")
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT name, phone, payment_status FROM users WHERE email = ?",
            (schedule.email,)
        )
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["payment_status"] != "confirmed":
            raise HTTPException(status_code=400, detail="Payment required before scheduling")
    
    headers = {
        "Authorization": f"Bearer {RETELL_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "from_number": RETELL_FROM_NUMBER,
        "to_number": user["phone"],
        "override_agent_id": RETELL_AGENT_ID,
        "retell_llm_dynamic_variables": {
            "full_name": user["name"],
            "preferred_time": schedule.preferred_time
        }
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{RETELL_BASE}/create-phone-call",
            headers=headers,
            json=payload
        )
        result = response.json()
    
    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Retell error: {result}")
    
    call_id = result.get("call_id", result.get("id", "unknown"))
    
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET call_status = 'scheduled', call_id = ?, updated_at = ? WHERE email = ?",
            (call_id, now, schedule.email)
        )
        await db.commit()
    
    return {"message": "Call scheduled successfully", "call_id": call_id}

# ─── Retell Webhook: Call Completed ───
@app.post("/api/webhooks/retell")
async def retell_webhook(payload: dict):
    event = payload.get("event", "")
    call_data = payload.get("call", {})
    
    if event in ("call_ended", "call_completed"):
        call_id = call_data.get("call_id", call_data.get("id", ""))
        transcript = call_data.get("transcript", "")
        if not transcript and "call_analysis" in call_data:
            transcript = call_data["call_analysis"].get("custom_analysis_data", "")
        
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT email, name FROM users WHERE call_id = ?", (call_id,))
            user = await cursor.fetchone()
            
            if user:
                now = datetime.utcnow().isoformat()
                await db.execute(
                    "UPDATE users SET call_status = 'completed', transcript = ?, updated_at = ? WHERE call_id = ?",
                    (json.dumps(transcript) if isinstance(transcript, dict) else str(transcript), now, call_id)
                )
                await db.commit()
                
                # Trigger report generation
                await generate_report(user["email"], user["name"], str(transcript))
    
    return {"status": "ok"}

# ─── Generate Report via Kimi ───
async def generate_report(email: str, name: str, transcript: str):
    if not KIMI_API_KEY:
        return
    
    prompt = f"""You are a senior McKinsey-style productivity consultant. Based on the following interview transcript with {name}, write a comprehensive 10-page AI productivity report.

INTERVIEW TRANSCRIPT:
{transcript[:8000]}

Write a professional report with these sections:
1. Executive Summary - key findings, total savings potential, top 3 recommendations
2. Your Workflow - analysis of how they spend their time
3. Pain Point #1 - deep dive on biggest time sink
4. Pain Point #2 - second biggest issue
5. Pain Point #3 - third issue
6. Pain Point #4 - fourth issue
7. Tool Stack: Analysis & Writing - specific tool recommendations
8. Tool Stack: Communication & Voice - more tool recommendations
9. Implementation Roadmap - 30/60/90 day plan with quick wins
10. Expected Outcomes - ROI calculation, time savings projection

Include specific tools with pricing, setup time, and hours saved. Use tables where helpful. Write in a professional, consultant tone. No jargon."""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{KIMI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"},
                json={"model": "kimi-latest", "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 8000},
                timeout=120.0
            )
            result = response.json()
            report_content = result["choices"][0]["message"]["content"]
    except Exception as e:
        report_content = f"Error generating report: {str(e)}. Please contact support."
    
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET report_status = 'ready', report_content = ?, updated_at = ? WHERE email = ?",
            (report_content, now, email)
        )
        await db.commit()
    
    await send_report_email(email, name, report_content)

# ─── Send Report via Email ───
async def send_report_email(email: str, name: str, report: str):
    if not RESEND_API_KEY:
        return
    try:
        resend.Emails.send({
            "from": "Verdant <reports@verdantai.xyz>",
            "to": email,
            "subject": f"Your Personal AI Productivity Report - {name}",
            "html": f"""
            <div style="font-family: Georgia, serif; max-width: 600px; margin: 0 auto; color: #1a1a1a;">
                <div style="background: #080F0C; padding: 40px 30px; text-align: center;">
                    <p style="color: #2ECC71; font-size: 12px; letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 20px;">Verdant</p>
                    <h1 style="color: #F0F4F1; font-size: 24px; font-weight: 400; margin: 0;">Your Personal AI Productivity Report</h1>
                </div>
                <div style="padding: 30px;">
                    <p style="font-size: 16px; line-height: 1.6;">Hi {name},</p>
                    <p style="font-size: 15px; line-height: 1.6; color: #555;">
                        Thank you for completing your assessment call. Your personalised report is attached below.
                    </p>
                    <pre style="white-space: pre-wrap; font-family: Georgia, serif; font-size: 13px; line-height: 1.7; color: #333; background: #f8f8f8; padding: 20px; border-radius: 8px; overflow-wrap: break-word;">{report}</pre>
                </div>
                <div style="padding: 20px 30px; text-align: center; border-top: 1px solid #e0e0e0; color: #888; font-size: 12px;">
                    Verdant - hello@verdantai.xyz - verdantai.xyz
                </div>
            </div>"""
        })
    except Exception as e:
        print(f"Email send failed: {e}")

# ─── Get Report ───
@app.get("/api/reports/{email}")
async def get_report(email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT report_status, report_content, updated_at FROM users WHERE email = ?", (email,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return {"status": row["report_status"], "content": row["report_content"] or "Report being generated. Check back in a few hours.", "generated_at": row["updated_at"]}

# ─── Admin: List Users ───
@app.get("/api/admin/users")
async def list_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, email, name, phone, payment_status, call_status, report_status, created_at FROM users ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

# ─── Razorpay Webhook ───
@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    
    if RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    
    payload = json.loads(body)
    event = payload.get("event", "")
    
    if event == "payment.captured":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment_entity.get("order_id")
        payment_id = payment_entity.get("id")
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET payment_status = 'confirmed', razorpay_payment_id = ?, updated_at = ? WHERE razorpay_order_id = ?",
                (payment_id, datetime.utcnow().isoformat(), order_id)
            )
            await db.commit()
    
    return {"status": "ok"}

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "Verdant API is running. See /docs for API documentation."

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
