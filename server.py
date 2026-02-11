from fastapi import FastAPI, APIRouter, Request
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import httpx
from pydantic import BaseModel
from typing import List
from datetime import datetime, timezone, timedelta

TZ_ITALY = timezone(timedelta(hours=1))

def now_italy():
    return datetime.now(TZ_ITALY)

load_dotenv()

mongo_url = os.environ.get('MONGO_URL')
db_name = os.environ.get('DB_NAME', 'fas_monitor')
client = AsyncIOMotorClient(mongo_url)
db = client[db_name]

app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

telegram_state = {"bot_token": None, "chat_id": None, "history": [], "threshold": 6, "last_sync": None, "rendered_templates": {}}

BACKEND_URL = os.environ.get('BACKEND_URL', '')

@api_router.get("/")
async def root():
    return {"message": "FAS League Monitor API"}

@api_router.get("/ping")
async def ping():
    total = await db.fas_historical.count_documents({})
    return {"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat(), "total_records": total}

@api_router.get("/health")
async def health():
    try:
        await db.fas_historical.find_one({}, {"_id": 1})
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    return {"status": "healthy", "database": db_status, "timestamp": datetime.now(timezone.utc).isoformat()}

def pad_giornata(g):
    try:
        return f"{int(g):02d}"
    except:
        return str(g)

def _sort_records(records):
    def sort_key(r):
        d = r.get("data_sisal", r.get("data", "00/00/0000"))
        parts = d.split("/")
        date_str = f"{parts[2]}{parts[1]}{parts[0]}" if len(parts) == 3 else d
        try:
            g = int(r.get("giornata", 0))
        except:
            g = 0
        return (date_str, r.get("ora", "00:00"), g)
    records.sort(key=sort_key)
    return records

async def _load_all_historical():
    cursor = db.fas_historical.find({}, {"_id": 0})
    records = await cursor.to_list(length=50000)
    return _sort_records(records)

async def generate_stats_message_from_db(threshold):
    history = await _load_all_historical()
    if not history:
        return "Nessun dato."
    last_record = history[-1]
    data_corrente = last_record.get("data_sisal", now_italy().strftime("%d/%m/%Y"))
    last_giornata = pad_giornata(last_record.get("giornata", "?"))
    text = f"📊 <b>STATISTICHE FAS</b>\n📅 <b>{data_corrente}</b> - Giornata {last_giornata}\n\n"
    for pos in range(6):
        consecutive_ng = 0
        for i in range(len(history) - 1, -1, -1):
            matches = history[i].get("matches", [])
            if pos < len(matches):
                if matches[pos].get("result") == "NG":
                    consecutive_ng += 1
                else:
                    break
        icon = "🚨" if consecutive_ng >= threshold else "🔴" if consecutive_ng > 0 else "🟢"
        text += f"{icon} Serie {pos+1}: {consecutive_ng} NG\n"
    text += f"\n📊 Totale: {len(history)} giornate\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_history_message_from_db(raw_data_only=False):
    cursor = db.fas_historical.find({}, {"_id": 0}).sort("order", -1).limit(15)
    records = await cursor.to_list(length=15)
    if not records:
        return "Nessun dato."
    records.reverse()
    total = await db.fas_historical.count_documents({})
    data_rows = ""
    for r in records:
        g = pad_giornata(r.get("giornata", "?"))
        ora = r.get("ora", "")
        matches = r.get("matches", [])
        results = " ".join("🟢" if m.get("result") == "G" else "🔴" for m in matches)
        data_rows += f"G<b>{g}</b> {ora}  {results}\n"
    if raw_data_only:
        return data_rows.strip()
    text = f"📋 <b>STORICO FAS - ULTIME {len(records)}</b> (di {total})\n\n{data_rows}\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_info_message_from_db(threshold):
    total = await db.fas_historical.count_documents({})
    return f"ℹ️ <b>FAS MONITOR</b>\n\n📊 Giornate: {total}\n🔔 Soglia: {threshold} NG\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"

async def generate_streak_message_from_db(threshold):
    MIN_STREAK = 5
    all_records = await _load_all_historical()
    if not all_records:
        return "Nessun dato."
    text = f"📈 <b>STREAK TOTALE NG (≥{MIN_STREAK})</b>\n📊 {len(all_records)} giornate\n\n"
    for pos in range(6):
        max_streak = 0
        current = 0
        for record in all_records:
            matches = record.get("matches", [])
            if pos < len(matches) and matches[pos].get("result") == "NG":
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        text += f"📍 Pos {pos+1}: max <b>{max_streak}</b> NG\n"
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_streak_daily_message_from_db(threshold):
    today = now_italy().strftime('%d/%m/%Y')
    cursor = db.fas_historical.find({"data_sisal": today}, {"_id": 0})
    records = await cursor.to_list(length=500)
    if not records:
        return f"📊 <b>STREAK GIORNALIERO</b>\n\nNessun dato per {today}"
    text = f"📊 <b>STREAK GIORNALIERO</b>\n📅 {today} ({len(records)} giornate)\n\n"
    for pos in range(6):
        max_streak = 0
        current = 0
        for record in records:
            matches = record.get("matches", [])
            if pos < len(matches) and matches[pos].get("result") == "NG":
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        text += f"📍 Pos {pos+1}: max <b>{max_streak}</b> NG\n"
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

def build_callback_keyboard():
    return {"inline_keyboard": [[{"text": "📈 Streak Totale", "callback_data": "streak"}, {"text": "📊 Streak Giornaliero", "callback_data": "streak_daily"}], [{"text": "📋 Storico Ultime 15", "callback_data": "history"}, {"text": "ℹ️ Info", "callback_data": "info"}]]}

@api_router.post("/telegram/sync")
async def telegram_sync(request: Request):
    data = await request.json()
    bot_token = data.get("bot_token")
    chat_id = data.get("chat_id")
    history = data.get("history", [])
    threshold = data.get("threshold", 6)
    rendered_templates = data.get("rendered_templates", {})
    is_incremental = data.get("is_incremental", False)
    total_local = data.get("total_local", len(history))
    now = datetime.now(timezone.utc).isoformat()
    telegram_state.update({"bot_token": bot_token, "chat_id": chat_id, "threshold": threshold, "last_sync": now, "rendered_templates": rendered_templates})
    if not is_incremental:
        telegram_state["history"] = history
    await db.fas_telegram_sync.update_one({"_id": "current"}, {"$set": {"bot_token": bot_token, "chat_id": chat_id, "history": telegram_state["history"], "threshold": threshold, "last_sync": now}}, upsert=True)
    new_count = 0
    for idx, record in enumerate(history):
        data_sisal = record.get("dataRicerca") or record.get("data") or "sconosciuta"
        giornata = str(record.get("giornata", "?"))
        ora = record.get("ora", "")
        key = f"{data_sisal}_{giornata}_{ora}"
        result = await db.fas_historical.update_one({"_id": key}, {"$set": {"data_sisal": data_sisal, "giornata": giornata, "ora": ora, "matches": record.get("matches", []), "synced_at": now, "order": record.get("id", idx)}}, upsert=True)
        if result.upserted_id:
            new_count += 1
    total = await db.fas_historical.count_documents({})
    logger.info(f"Sync: {len(history)} giornate, {new_count} nuove, totale: {total}")
    return {"status": "ok", "synced_giornate": len(history), "new_records": new_count, "total_historical": total}

@api_router.post("/telegram/setup-webhook")
async def setup_webhook(request: Request):
    data = await request.json()
    bot_token = data.get("bot_token") or telegram_state["bot_token"]
    if not bot_token:
        return {"error": "Bot token mancante"}
    webhook_url = f"{BACKEND_URL}/api/telegram/webhook"
    async with httpx.AsyncClient() as http:
        resp = await http.post(f"https://api.telegram.org/bot{bot_token}/setWebhook", json={"url": webhook_url, "allowed_updates": ["callback_query"]})
        return resp.json()

@api_router.post("/telegram/send-menu")
async def send_menu(request: Request):
    data = await request.json()
    bot_token = data.get("bot_token") or telegram_state["bot_token"]
    chat_id = data.get("chat_id") or telegram_state["chat_id"]
    if not bot_token or not chat_id:
        return {"error": "Configurazione mancante"}
    async with httpx.AsyncClient() as http:
        resp = await http.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": "⚽ <b>FAS MONITOR</b>\n\nSeleziona:", "parse_mode": "HTML", "reply_markup": build_callback_keyboard()})
        return resp.json()

@api_router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}
    callback_id = callback["id"]
    callback_data = callback.get("data", "")
    user_id = callback.get("from", {}).get("id")
    bot_token = telegram_state["bot_token"]
    if not bot_token or not user_id:
        return {"ok": True}
    threshold = telegram_state.get("threshold", 6)
    if callback_data == "streak":
        text = await generate_streak_message_from_db(threshold)
    elif callback_data == "streak_daily":
        text = await generate_streak_daily_message_from_db(threshold)
    elif callback_data == "history":
        text = await generate_history_message_from_db()
    elif callback_data == "info":
        text = await generate_info_message_from_db(threshold)
    else:
        text = "Comando non riconosciuto"
    async with httpx.AsyncClient() as http:
        await http.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery", json={"callback_query_id": callback_id})
        await http.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": user_id, "text": text, "parse_mode": "HTML", "reply_markup": build_callback_keyboard()})
    return {"ok": True}

@api_router.get("/telegram/status")
async def telegram_status():
    total = await db.fas_historical.count_documents({})
    return {"configured": telegram_state["bot_token"] is not None, "total_historical": total, "last_sync": telegram_state["last_sync"]}

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def load_state():
    saved = await db.fas_telegram_sync.find_one({"_id": "current"})
    if saved:
        telegram_state.update({"bot_token": saved.get("bot_token"), "chat_id": saved.get("chat_id"), "history": saved.get("history", []), "threshold": saved.get("threshold", 6), "last_sync": saved.get("last_sync")})
        logger.info(f"Caricato: {len(telegram_state['history'])} giornate")
