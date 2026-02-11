🚀 PASSO 3b: Aggiungi server.py
Torna alla pagina principale del repository
Clicca "Add file" → "Create new file"
Nel campo nome file scrivi: server.py
Copia e incolla TUTTO questo contenuto:
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import httpx
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

telegram_state = {
    "bot_token": None,
    "chat_id": None,
    "history": [],
    "threshold": 6,
    "last_sync": None,
    "rendered_templates": {}
}

BACKEND_URL = os.environ.get('BACKEND_URL', '')

class MatchResult(BaseModel):
    number: int
    homeTeam: str
    awayTeam: str
    matchName: str
    result: str

class GiornataImport(BaseModel):
    timestamp: str
    date: str
    giornata: str
    ora: str
    matches: List[MatchResult]
    totalMatches: int

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
    return {"status": "healthy", "database": db_status, "timestamp": datetime.now(timezone.utc).isoformat(), "telegram_configured": telegram_state["bot_token"] is not None}

def pad_giornata(g):
    try:
        n = int(g)
        return f"{n:02d}"
    except (ValueError, TypeError):
        return str(g)

def _sort_records(records):
    def sort_key(r):
        d = r.get("data_sisal", r.get("data", "00/00/0000"))
        parts = d.split("/")
        date_str = f"{parts[2]}{parts[1]}{parts[0]}" if len(parts) == 3 else d
        try:
            g = int(r.get("giornata", 0))
        except (ValueError, TypeError):
            g = 0
        ora = r.get("ora", "00:00")
        return (date_str, ora, g)
    records.sort(key=sort_key)
    return records

async def _load_all_historical():
    cursor = db.fas_historical.find({}, {"_id": 0})
    records = await cursor.to_list(length=50000)
    return _sort_records(records)

async def generate_stats_message_from_db(threshold):
    history = await _load_all_historical()
    if not history:
        return "📊 <b>STATISTICHE FAS</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    last_record = history[-1]
    data_corrente = last_record.get("data_sisal", last_record.get("data", now_italy().strftime("%d/%m/%Y")))
    last_giornata = pad_giornata(last_record.get("giornata", "?"))
    text = f"📊 <b>STATISTICHE FAS</b>\n📅 <b>{data_corrente}</b> — Giornata {last_giornata}\n━━━━━━━━━━━━━━━━━\n\n"
    has_alert = False
    for pos in range(6):
        consecutive_ng = 0
        consecutive_g = 0
        total_g = 0
        total_ng = 0
        streak_started_at = None
        for i in range(len(history) - 1, -1, -1):
            matches = history[i].get("matches", [])
            if pos < len(matches):
                result = matches[pos].get("result", "")
                if result == "NG":
                    if consecutive_g == 0:
                        consecutive_ng += 1
                        streak_started_at = pad_giornata(history[i].get("giornata", "?"))
                    total_ng += 1
                else:
                    if consecutive_ng == 0:
                        consecutive_g += 1
                    total_g += 1
        if consecutive_ng >= threshold:
            icon = "🚨"
            has_alert = True
        elif consecutive_ng >= 4:
            icon = "⚠️"
        elif consecutive_ng > 0:
            icon = "🔴"
        else:
            icon = "🟢"
        text += f"{icon} <b>Serie {pos + 1}</b>: "
        if consecutive_ng > 0:
            text += f"<b>{consecutive_ng} NG di fila</b>"
            if streak_started_at:
                text += f" (da G{streak_started_at})"
        else:
            text += f"{consecutive_g}x G"
        text += f"  [G:{total_g} NG:{total_ng}]\n"
    text += f"\n📊 Giornate analizzate: <b>{len(history)}</b>"
    text += f"\n🔔 Soglia allarme: <b>{threshold}</b> NG"
    if has_alert:
        text += "\n\n🚨 <b>ATTENZIONE: Soglia superata!</b>"
    text += f"\n\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_history_message_from_db(raw_data_only=False):
    cursor = db.fas_historical.find({}, {"_id": 0}).sort("order", -1).limit(15)
    records = await cursor.to_list(length=15)
    if not records:
        if raw_data_only:
            return "(nessun dato)"
        return "📋 <b>STORICO FAS</b>\n\nNessun dato. Sincronizza con 📡 Sync."
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
    header_row = "                       1   2   3   4   5   6\n"
    text = f"📋 <b>STORICO FAS</b>\n\n📋 <b>ULTIME {len(records)} GIORNATE</b> (di {total} totali)\n━━━━━━━━━━━━━━━━━\n"
    text += header_row + data_rows
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_info_message_from_db(threshold):
    total = await db.fas_historical.count_documents({})
    dates = await db.fas_historical.distinct("data_sisal")
    text = "ℹ️ <b>FAS MONITOR</b>\n━━━━━━━━━━━━━━━━━\n\n"
    text += "⚽ Monitora Goal/No Goal della FAS League\n\n"
    text += f"🔔 <b>Soglia allarme:</b> {threshold} NG consecutivi\n"
    text += f"📊 <b>Giornate in DB:</b> {total} ({len(dates)} giorni)\n"
    text += "⏱️ <b>Intervallo auto:</b> 5 minuti\n"
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_streak_message_from_db(threshold):
    MIN_STREAK = 5
    all_records = await _load_all_historical()
    if not all_records:
        return "📈 <b>ANALISI STREAK NG</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    all_records.sort(key=lambda r: r.get("order", 0))
    text = f"📈 <b>STREAK TOTALE NG (≥{MIN_STREAK})</b>\n📊 Analisi su <b>{len(all_records)}</b> giornate totali\n━━━━━━━━━━━━━━━━━\n\n"
    def is_consecutive(g1, g2):
        try:
            n1, n2 = int(g1), int(g2)
            return n2 == 1 if n1 == 22 else n2 == n1 + 1
        except:
            return False
    for pos in range(6):
        streak_counts = {}
        current_streak = 0
        max_streak = 0
        current_streak_start = None
        ongoing_streak = False
        last_giornata = None
        for i, record in enumerate(all_records):
            matches = record.get("matches", [])
            if pos < len(matches):
                result = matches[pos].get("result", "")
                giornata = record.get("giornata", "?")
                if last_giornata is not None and current_streak > 0:
                    if not is_consecutive(last_giornata, giornata):
                        if current_streak >= MIN_STREAK:
                            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                        if current_streak > max_streak:
                            max_streak = current_streak
                        current_streak = 0
                        current_streak_start = None
                if result == "NG":
                    if current_streak == 0:
                        current_streak_start = giornata
                    current_streak += 1
                    if i == len(all_records) - 1:
                        ongoing_streak = True
                        if current_streak > max_streak:
                            max_streak = current_streak
                else:
                    if current_streak >= MIN_STREAK:
                        streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                    if current_streak > max_streak:
                        max_streak = current_streak
                    current_streak = 0
                    current_streak_start = None
                last_giornata = giornata
        if current_streak >= MIN_STREAK and ongoing_streak:
            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
        if streak_counts or max_streak >= MIN_STREAK:
            text += f"📍 <b>POSIZIONE {pos + 1}</b>\n   Max streak: <b>{max_streak}</b> NG\n"
            if streak_counts:
                text += f"   Occorrenze (≥{MIN_STREAK}):\n"
                for length in sorted(streak_counts.keys(), reverse=True):
                    count = streak_counts[length]
                    volte = "volta" if count == 1 else "volte"
                    text += f"   • {length} NG → <b>{count}</b> {volte}\n"
            if ongoing_streak and current_streak >= MIN_STREAK:
                text += f"   ⚠️ <b>In corso: {current_streak} NG</b> (da G{current_streak_start})\n"
            text += "\n"
    text += f"🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_streak_daily_message_from_db(threshold):
    MIN_STREAK = 5
    today = now_italy().strftime('%d/%m/%Y')
    cursor = db.fas_historical.find({"data_sisal": today}, {"_id": 0}).sort("order", 1)
    today_records = await cursor.to_list(length=500)
    if not today_records:
        return f"📊 <b>STREAK GIORNALIERO</b>\n\n⚠️ Nessun dato per oggi ({today}).\n\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    text = f"📊 <b>STREAK GIORNALIERO (≥{MIN_STREAK})</b>\n📅 Data: <b>{today}</b>\n📊 Giornate analizzate: <b>{len(today_records)}</b>\n━━━━━━━━━━━━━━━━━\n\n"
    def is_consecutive(g1, g2):
        try:
            n1, n2 = int(g1), int(g2)
            return n2 == 1 if n1 == 22 else n2 == n1 + 1
        except:
            return False
    for pos in range(6):
        streak_counts = {}
        current_streak = 0
        max_streak = 0
        current_streak_start = None
        ongoing_streak = False
        last_giornata = None
        for i, record in enumerate(today_records):
            matches = record.get("matches", [])
            if pos < len(matches):
                result = matches[pos].get("result", "")
                giornata = record.get("giornata", "?")
                if last_giornata is not None and current_streak > 0:
                    if not is_consecutive(last_giornata, giornata):
                        if current_streak >= MIN_STREAK:
                            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                        if current_streak > max_streak:
                            max_streak = current_streak
                        current_streak = 0
                        current_streak_start = None
                if result == "NG":
                    if current_streak == 0:
                        current_streak_start = giornata
                    current_streak += 1
                    if i == len(today_records) - 1:
                        ongoing_streak = True
                        if current_streak > max_streak:
                            max_streak = current_streak
                else:
                    if current_streak >= MIN_STREAK:
                        streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                    if current_streak > max_streak:
                        max_streak = current_streak
                    current_streak = 0
                    current_streak_start = None
                last_giornata = giornata
        if current_streak >= MIN_STREAK and ongoing_streak:
            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
        text += f"📍 <b>POSIZIONE {pos + 1}</b>\n   Max streak: <b>{max_streak}</b> NG\n"
        if streak_counts:
            text += f"   Occorrenze (≥{MIN_STREAK}):\n"
            for length in sorted(streak_counts.keys(), reverse=True):
                count = streak_counts[length]
                volte = "volta" if count == 1 else "volte"
                text += f"   • {length} NG → <b>{count}</b> {volte}\n"
        if ongoing_streak and current_streak >= MIN_STREAK:
            text += f"   ⚠️ <b>In corso: {current_streak} NG</b> (da G{current_streak_start})\n"
        text += "\n"
    text += f"🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def delete_message_after_delay(bot_token, chat_id, message_id, delay_seconds):
    import asyncio
    await asyncio.sleep(delay_seconds)
    async with httpx.AsyncClient() as http:
        try:
            await http.post(f"https://api.telegram.org/bot{bot_token}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
        except:
            pass

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
    telegram_state["bot_token"] = bot_token
    telegram_state["chat_id"] = chat_id
    telegram_state["threshold"] = threshold
    telegram_state["last_sync"] = now
    telegram_state["rendered_templates"] = rendered_templates
    if not is_incremental:
        telegram_state["history"] = history
    else:
        existing_ids = set(str(r.get("id", "")) for r in telegram_state["history"])
        for r in history:
            if str(r.get("id", "")) not in existing_ids:
                telegram_state["history"].append(r)
    await db.fas_telegram_sync.update_one({"_id": "current"}, {"$set": {"bot_token": bot_token, "chat_id": chat_id, "history": telegram_state["history"], "threshold": threshold, "rendered_templates": rendered_templates, "last_sync": now, "total_local": total_local}}, upsert=True)
    new_count = 0
    for idx, record in enumerate(history):
        data_sisal = record.get("dataRicerca") or record.get("data") or "sconosciuta"
        giornata = str(record.get("giornata", "?"))
        ora = record.get("ora", "")
        key = f"{data_sisal}_{giornata}_{ora}"
        result = await db.fas_historical.update_one({"_id": key}, {"$set": {"data_sisal": data_sisal, "data_estrazione": record.get("data", ""), "giornata": giornata, "ora": ora, "matches": record.get("matches", []), "synced_at": now, "order": record.get("id", idx)}}, upsert=True)
        if result.upserted_id:
            new_count += 1
    total_historical = await db.fas_historical.count_documents({})
    logger.info(f"[Telegram] Sync: {len(history)} giornate, {new_count} nuove, storico: {total_historical}")
    return {"status": "ok", "synced_giornate": len(history), "new_records": new_count, "total_historical": total_historical, "incremental": is_incremental}

@api_router.post("/telegram/setup-webhook")
async def setup_webhook(request: Request):
    data = await request.json()
    bot_token = data.get("bot_token") or telegram_state["bot_token"]
    if not bot_token:
        return {"status": "error", "message": "Bot token mancante"}
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
        return {"status": "error", "message": "Bot token o Chat ID mancante"}
    text = "⚽ <b>FAS MONITOR - MENU</b>\n━━━━━━━━━━━━━━━━━\n\nSeleziona un'opzione:"
    async with httpx.AsyncClient() as http:
        resp = await http.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": build_callback_keyboard()})
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
    user_name = callback.get("from", {}).get("first_name", "Utente")
    channel_id = callback.get("message", {}).get("chat", {}).get("id")
    bot_token = telegram_state["bot_token"]
    if not bot_token or not user_id:
        return {"ok": True}
    threshold = telegram_state.get("threshold", 7)
    saved_templates = telegram_state.get("rendered_templates", {})
    user_tpl = saved_templates.get(callback_data)
    use_raw_data = user_tpl and "{data}" in user_tpl
    if callback_data == "streak":
        fresh_data = await generate_streak_message_from_db(threshold)
    elif callback_data == "streak_daily":
        fresh_data = await generate_streak_daily_message_from_db(threshold)
    elif callback_data == "history":
        fresh_data = await generate_history_message_from_db(raw_data_only=use_raw_data)
    elif callback_data == "info":
        fresh_data = await generate_info_message_from_db(threshold)
    elif callback_data == "stats":
        fresh_data = await generate_stats_message_from_db(threshold)
    else:
        fresh_data = "⚠️ Comando non riconosciuto"
    if use_raw_data:
        ts = now_italy().strftime('%d/%m/%Y %H:%M')
        text = user_tpl.replace("{data}", fresh_data).replace("{timestamp}", ts)
    else:
        text = fresh_data
    async with httpx.AsyncClient() as http:
        await http.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": "📨 Messaggio inviato in privato!"})
        response = await http.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": user_id, "text": text, "parse_mode": "HTML", "reply_markup": build_callback_keyboard()})
        result = response.json()
        if not result.get("ok"):
            error_desc = result.get("description", "")
            if "bot can't initiate" in error_desc.lower() or "chat not found" in error_desc.lower() or "forbidden" in error_desc.lower():
                await http.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": "⚠️ Devi prima avviare il bot!", "show_alert": True})
                msg_response = await http.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": channel_id, "text": f"👋 <b>{user_name}</b>, per ricevere le statistiche in privato devi prima avviare il bot!\n\n👉 Clicca qui: @fas_alert_bot\n\nPoi premi <b>AVVIA/START</b> e riprova.", "parse_mode": "HTML"})
                msg_result = msg_response.json()
                if msg_result.get("ok"):
                    message_id = msg_result.get("result", {}).get("message_id")
                    if message_id:
                        import asyncio
                        asyncio.create_task(delete_message_after_delay(bot_token, channel_id, message_id, 10))
    return {"ok": True}

@api_router.get("/telegram/status")
async def telegram_status():
    total_historical = await db.fas_historical.count_documents({})
    dates = await db.fas_historical.distinct("data_sisal")
    return {"configured": telegram_state["bot_token"] is not None, "synced_giornate": len(telegram_state["history"]), "total_historical": total_historical, "historical_days": len(dates), "threshold": telegram_state["threshold"], "last_sync": telegram_state["last_sync"]}

@api_router.get("/historical/render-history")
async def render_history():
    text = await generate_history_message_from_db(raw_data_only=True)
    return {"text": text}

@api_router.get("/historical/render-info")
async def render_info():
    text = await generate_info_message_from_db(telegram_state.get("threshold", 7))
    return {"text": text}

@api_router.get("/historical/render-streak")
async def render_streak():
    text = await generate_streak_message_from_db(telegram_state.get("threshold", 7))
    return {"text": text}

app.include_router(api_router)

app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def load_telegram_state():
    saved = await db.fas_telegram_sync.find_one({"_id": "current"})
    if saved:
        telegram_state["bot_token"] = saved.get("bot_token")
        telegram_state["chat_id"] = saved.get("chat_id")
        telegram_state["history"] = saved.get("history", [])
        telegram_state["threshold"] = saved.get("threshold", 6)
        telegram_state["last_sync"] = saved.get("last_sync")
        telegram_state["rendered_templates"] = saved.get("rendered_templates", {})
        logger.info(f"[Telegram] Caricato da DB: {len(telegram_state['history'])} giornate")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
