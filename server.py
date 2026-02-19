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

# Timezone Italia (CET = UTC+1)
TZ_ITALY = timezone(timedelta(hours=1))

def now_italy():
    return datetime.now(TZ_ITALY)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ====== TELEGRAM IN-MEMORY STATE ======
telegram_state = {
    "bot_token": None,
    "chat_id": None,
    "history": [],
    "threshold": 6,
    "last_sync": None,
    "rendered_templates": {}
}

BACKEND_URL = os.environ.get('BACKEND_URL', 'https://fas-streak-alert.preview.emergentagent.com')

# Lista delle 12 squadre monitorate
TEAMS = ['SAM', 'ROM', 'UDI', 'NAP', 'INT', 'GEN', 'VER', 'ATA', 'JUV', 'LAZ', 'MIL', 'FIO']

# Models
class MatchResult(BaseModel):
    number: int
    homeTeam: str
    awayTeam: str
    matchName: str
    result: str  # "Goal" or "No Goal"

class GiornataImport(BaseModel):
    timestamp: str
    date: str
    giornata: str
    ora: str
    matches: List[MatchResult]
    totalMatches: int

class GiornataRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    date: str
    giornata: int
    ora: str
    matches: List[dict]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatsResponse(BaseModel):
    total_giornate: int
    total_matches: int
    goal_count: int
    no_goal_count: int
    goal_percentage: float
    per_position_stats: dict

# Routes
@api_router.get("/")
async def root():
    return {"message": "FAS League Monitor API"}

@api_router.get("/ping")
async def ping():
    """Endpoint per keep-alive esterno - mantiene il container sveglio"""
    total = await db.fas_historical.count_documents({})
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_records": total
    }

@api_router.get("/health")
async def health():
    """Health check dettagliato"""
    try:
        # Verifica connessione MongoDB
        await db.fas_historical.find_one({}, {"_id": 1})
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return {
        "status": "healthy",
        "database": db_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "telegram_configured": telegram_state["bot_token"] is not None
    }

@api_router.post("/matches/import")
async def import_matches(data: GiornataImport):
    """Importa dati dalla Chrome Extension"""
    try:
        # Converti giornata in int
        giornata_num = int(data.giornata) if data.giornata.isdigit() else 0
        
        # Prepara documento
        doc = {
            "id": str(uuid.uuid4()),
            "date": data.date,
            "giornata": giornata_num,
            "ora": data.ora,
            "matches": [m.model_dump() for m in data.matches],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "import_timestamp": data.timestamp
        }
        
        # Upsert basato su date + giornata + ora (evita duplicati)
        result = await db.fas_giornate.update_one(
            {"date": data.date, "giornata": giornata_num, "ora": data.ora},
            {"$set": doc},
            upsert=True
        )
        
        logger.info(f"Importata Giornata {giornata_num} - {data.ora} con {len(data.matches)} partite")
        
        return {
            "status": "success",
            "message": f"Importata Giornata {giornata_num}",
            "matches_count": len(data.matches)
        }
        
    except Exception as e:
        logger.error(f"Errore import: {e}")
        return {"status": "error", "message": str(e)}

@api_router.get("/matches")
async def get_matches(date: Optional[str] = None, limit: int = 50):
    """Ottieni tutte le giornate"""
    query = {}
    if date:
        query["date"] = date
    
    cursor = db.fas_giornate.find(query, {"_id": 0}).sort([("date", -1), ("giornata", -1)]).limit(limit)
    giornate = await cursor.to_list(limit)
    
    # Calcola statistiche rapide
    total = await db.fas_giornate.count_documents({})
    
    return {
        "giornate": giornate,
        "total_giornate": total
    }

@api_router.get("/matches/latest")
async def get_latest():
    """Ottieni l'ultima giornata importata"""
    latest = await db.fas_giornate.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
    return latest or {"message": "Nessun dato"}

@api_router.get("/stats")
async def get_stats():
    """Statistiche aggregate Goal/No Goal"""
    
    # Conta totali
    total_giornate = await db.fas_giornate.count_documents({})
    
    if total_giornate == 0:
        return {
            "total_giornate": 0,
            "total_matches": 0,
            "goal_count": 0,
            "no_goal_count": 0,
            "goal_percentage": 0,
            "no_goal_percentage": 0,
            "per_position_stats": {}
        }
    
    # Aggregazione per contare Goal/No Goal
    pipeline = [
        {"$unwind": "$matches"},
        {"$group": {
            "_id": "$matches.result",
            "count": {"$sum": 1}
        }}
    ]
    
    results = await db.fas_giornate.aggregate(pipeline).to_list(10)
    
    goal_count = 0
    no_goal_count = 0
    
    for r in results:
        if r["_id"] == "Goal":
            goal_count = r["count"]
        elif r["_id"] == "No Goal":
            no_goal_count = r["count"]
    
    total_matches = goal_count + no_goal_count
    
    # Statistiche per posizione (1-6)
    position_pipeline = [
        {"$unwind": "$matches"},
        {"$group": {
            "_id": {
                "position": "$matches.number",
                "result": "$matches.result"
            },
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id.position": 1}}
    ]
    
    position_results = await db.fas_giornate.aggregate(position_pipeline).to_list(100)
    
    per_position = {}
    for r in position_results:
        pos = r["_id"]["position"]
        result = r["_id"]["result"]
        if pos not in per_position:
            per_position[pos] = {"Goal": 0, "No Goal": 0, "total": 0}
        per_position[pos][result] = r["count"]
        per_position[pos]["total"] += r["count"]
    
    # Calcola percentuali per posizione
    for pos in per_position:
        total = per_position[pos]["total"]
        if total > 0:
            per_position[pos]["goal_pct"] = round(per_position[pos]["Goal"] / total * 100, 1)
            per_position[pos]["no_goal_pct"] = round(per_position[pos]["No Goal"] / total * 100, 1)
    
    return {
        "total_giornate": total_giornate,
        "total_matches": total_matches,
        "goal_count": goal_count,
        "no_goal_count": no_goal_count,
        "goal_percentage": round(goal_count / total_matches * 100, 1) if total_matches > 0 else 0,
        "no_goal_percentage": round(no_goal_count / total_matches * 100, 1) if total_matches > 0 else 0,
        "per_position_stats": per_position
    }

@api_router.get("/stats/history")
async def get_stats_history(days: int = 7):
    """Storico statistiche per giorno"""
    pipeline = [
        {"$unwind": "$matches"},
        {"$group": {
            "_id": {
                "date": "$date",
                "result": "$matches.result"
            },
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id.date": -1}}
    ]
    
    results = await db.fas_giornate.aggregate(pipeline).to_list(100)
    
    # Raggruppa per data
    by_date = {}
    for r in results:
        date = r["_id"]["date"]
        result = r["_id"]["result"]
        if date not in by_date:
            by_date[date] = {"date": date, "Goal": 0, "No Goal": 0}
        by_date[date][result] = r["count"]
    
    # Converti in lista ordinata
    history = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)[:days]
    
    return {"history": history}

@api_router.get("/sequences")
async def get_sequences():
    """Ottieni sequenze Goal/No Goal per posizione, ordinate per giornata"""
    
    # Recupera tutte le giornate ordinate
    cursor = db.fas_giornate.find({}, {"_id": 0}).sort([("date", 1), ("giornata", 1)])
    giornate = await cursor.to_list(1000)
    
    if not giornate:
        return {"sequences": {}, "streaks": {}, "giornate_count": 0}
    
    # Organizza per posizione (1-6)
    # Ogni posizione avrà la sequenza di risultati in ordine cronologico
    positions = {1: [], 2: [], 3: [], 4: [], 5: [], 6: []}
    
    for g in giornate:
        # Ordina partite per numero
        sorted_matches = sorted(g.get("matches", []), key=lambda x: x.get("number", 0))
        
        for idx, match in enumerate(sorted_matches):
            pos = idx + 1  # Posizione 1-6
            if pos <= 6:
                result = "G" if match.get("result") == "Goal" else "NG"
                positions[pos].append({
                    "giornata": g.get("giornata"),
                    "date": g.get("date"),
                    "ora": g.get("ora"),
                    "match": match.get("matchName", ""),
                    "result": result,
                    "full_result": match.get("result", "")
                })
    
    # Calcola streak (sequenze consecutive) per ogni posizione
    streaks = {}
    for pos, results in positions.items():
        if not results:
            continue
            
        current_streaks = []
        if results:
            current_result = results[0]["result"]
            current_count = 1
            
            for i in range(1, len(results)):
                if results[i]["result"] == current_result:
                    current_count += 1
                else:
                    current_streaks.append({
                        "type": current_result,
                        "count": current_count
                    })
                    current_result = results[i]["result"]
                    current_count = 1
            
            # Aggiungi l'ultima streak
            current_streaks.append({
                "type": current_result,
                "count": current_count
            })
        
        streaks[pos] = {
            "sequence": [r["result"] for r in results],
            "streaks": current_streaks,
            "total": len(results),
            "max_goal_streak": max([s["count"] for s in current_streaks if s["type"] == "G"], default=0),
            "max_nogol_streak": max([s["count"] for s in current_streaks if s["type"] == "NG"], default=0),
            "current_streak": current_streaks[-1] if current_streaks else None
        }
    
    return {
        "sequences": positions,
        "streaks": streaks,
        "giornate_count": len(giornate)
    }

@api_router.get("/timeline")
async def get_timeline():
    """Timeline completa delle giornate con risultati per posizione"""
    
    cursor = db.fas_giornate.find({}, {"_id": 0}).sort([("date", 1), ("giornata", 1)])
    giornate = await cursor.to_list(1000)
    
    timeline = []
    for g in giornate:
        sorted_matches = sorted(g.get("matches", []), key=lambda x: x.get("number", 0))
        
        results_by_position = {}
        for idx, match in enumerate(sorted_matches):
            pos = idx + 1
            if pos <= 6:
                results_by_position[pos] = {
                    "result": "G" if match.get("result") == "Goal" else "NG",
                    "match": match.get("matchName", ""),
                    "number": match.get("number", 0)
                }
        
        timeline.append({
            "giornata": g.get("giornata"),
            "date": g.get("date"),
            "ora": g.get("ora"),
            "positions": results_by_position
        })
    
    return {"timeline": timeline, "total": len(timeline)}

@api_router.delete("/matches/clear")
async def clear_matches():
    """Cancella tutti i dati (per reset)"""
    result = await db.fas_giornate.delete_many({})
    return {"deleted": result.deleted_count}

# ====== TELEGRAM CALLBACK SYSTEM ======

def pad_giornata(g):
    """Formatta giornata con zero iniziale: 1->01, 9->09, 10->10"""
    try:
        n = int(g)
        return f"{n:02d}"
    except (ValueError, TypeError):
        return str(g)

def _sort_records(records):
    """Ordina record per data_sisal + giornata + ora"""
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
    """Carica e ordina tutto lo storico da MongoDB"""
    cursor = db.fas_historical.find({}, {"_id": 0})
    records = await cursor.to_list(length=50000)
    return _sort_records(records)

async def generate_stats_message_from_db(threshold):
    """Genera statistiche SEMPRE fresche da MongoDB"""
    history = await _load_all_historical()
    if not history:
        return "📊 <b>STATISTICHE FAS</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    
    last_record = history[-1]
    data_corrente = last_record.get("data_sisal", last_record.get("data", now_italy().strftime("%d/%m/%Y")))
    last_giornata = pad_giornata(last_record.get("giornata", "?"))
    
    text = f"📊 <b>STATISTICHE FAS</b>\n"
    text += f"📅 <b>{data_corrente}</b> — Giornata {last_giornata}\n"
    text += "━━━━━━━━━━━━━━━━━\n\n"
    
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
    """Genera storico — ultime 15 giornate per ordine di inserimento
    Se raw_data_only=True, restituisce SOLO le righe dati (senza header numeri)
    """
    cursor = db.fas_historical.find({}, {"_id": 0}).sort("order", -1).limit(15)
    records = await cursor.to_list(length=15)
    
    if not records:
        if raw_data_only:
            return "(nessun dato)"
        return "📋 <b>STORICO FAS</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    
    records.reverse()
    total = await db.fas_historical.count_documents({})
    
    # Genera le righe dati
    data_rows = ""
    for r in records:
        g = pad_giornata(r.get("giornata", "?"))
        ora = r.get("ora", "")
        matches = r.get("matches", [])
        results = " ".join("🟢" if m.get("result") == "G" else "🔴" for m in matches)
        data_rows += f"G<b>{g}</b> {ora}  {results}\n"
    
    if raw_data_only:
        # Solo le righe, l'utente mette l'header nel template
        return data_rows.strip()
    
    # Messaggio completo (quando non c'è template personalizzato)
    header_row = "                       1   2   3   4   5   6\n"
    text = f"📋 <b>STORICO FAS</b>\n\n"
    text += f"📋 <b>ULTIME {len(records)} GIORNATE</b> (di {total} totali)\n━━━━━━━━━━━━━━━━━\n"
    text += header_row
    text += data_rows
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_info_message_from_db(threshold):
    """Genera info SEMPRE fresco da MongoDB"""
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
    """Genera analisi streak NG - conta quante volte si sono verificate sequenze di NG consecutive"""
    MIN_STREAK = 5  # Mostra streak >= 5
    
    all_records = await _load_all_historical()
    
    if not all_records:
        return "📈 <b>ANALISI STREAK NG</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    
    # Ordina i record per ordine di inserimento (order) per mantenere la sequenza temporale corretta
    all_records.sort(key=lambda r: r.get("order", 0))
    
    text = f"📈 <b>STREAK TOTALE NG (≥{MIN_STREAK})</b>\n"
    text += f"📊 Analisi su <b>{len(all_records)}</b> giornate totali\n"
    text += "━━━━━━━━━━━━━━━━━\n\n"
    
    # Funzione per verificare se due giornate sono consecutive
    def is_consecutive(g1, g2):
        """Verifica se g2 è la giornata successiva a g1 (considerando il ciclo G22->G1)"""
        try:
            n1 = int(g1)
            n2 = int(g2)
            if n1 == 22:
                return n2 == 1
            return n2 == n1 + 1
        except (ValueError, TypeError):
            return False
    
    # Analizza ogni posizione (1-6) separatamente
    for pos in range(6):
        streak_counts = {}  # {lunghezza_streak: numero_occorrenze}
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
                
                # Controlla se c'è un buco nei dati (giornata non consecutiva)
                if last_giornata is not None and current_streak > 0:
                    if not is_consecutive(last_giornata, giornata):
                        # C'è un buco! Chiudi la streak corrente
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
                    # Controlla se siamo all'ultimo record
                    if i == len(all_records) - 1:
                        ongoing_streak = True
                        if current_streak > max_streak:
                            max_streak = current_streak
                else:
                    # Fine della streak
                    if current_streak >= MIN_STREAK:
                        streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                    if current_streak > max_streak:
                        max_streak = current_streak
                    current_streak = 0
                    current_streak_start = None
                
                last_giornata = giornata
        
        # Se c'è una streak in corso alla fine dei dati
        if current_streak >= MIN_STREAK and ongoing_streak:
            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
        
        # Mostra solo se ci sono streak significative
        if streak_counts or max_streak >= MIN_STREAK:
            text += f"📍 <b>POSIZIONE {pos + 1}</b>\n"
            text += f"   Max streak: <b>{max_streak}</b> NG\n"
            
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
    """Genera analisi streak NG solo per la giornata odierna - stesso formato di streak totale"""
    MIN_STREAK = 5  # Mostra streak >= 5
    today = now_italy().strftime('%d/%m/%Y')
    
    # Cerca i record della giornata odierna - nessun limite, prendi tutti
    cursor = db.fas_historical.find({"data_sisal": today}, {"_id": 0}).sort("order", 1)
    today_records = await cursor.to_list(length=500)
    
    if not today_records:
        return f"📊 <b>STREAK GIORNALIERO</b>\n\n⚠️ Nessun dato per oggi ({today}).\n\nSincronizza con 📡 Sync.\n\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    
    text = f"📊 <b>STREAK GIORNALIERO (≥{MIN_STREAK})</b>\n"
    text += f"📅 Data: <b>{today}</b>\n"
    text += f"📊 Giornate analizzate: <b>{len(today_records)}</b>\n"
    text += "━━━━━━━━━━━━━━━━━\n\n"
    
    # Funzione per verificare se due giornate sono consecutive
    def is_consecutive(g1, g2):
        """Verifica se g2 è la giornata successiva a g1 (considerando il ciclo G22->G1)"""
        try:
            n1 = int(g1)
            n2 = int(g2)
            if n1 == 22:
                return n2 == 1
            return n2 == n1 + 1
        except (ValueError, TypeError):
            return False
    
    # Analizza ogni posizione (1-6) come nello streak totale
    for pos in range(6):
        streak_counts = {}  # {lunghezza_streak: numero_occorrenze}
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
                
                # Controlla se c'è un buco nei dati (giornata non consecutiva)
                if last_giornata is not None and current_streak > 0:
                    if not is_consecutive(last_giornata, giornata):
                        # C'è un buco! Chiudi la streak corrente
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
                    # Controlla se siamo all'ultimo record
                    if i == len(today_records) - 1:
                        ongoing_streak = True
                        if current_streak > max_streak:
                            max_streak = current_streak
                else:
                    # Fine della streak
                    if current_streak >= MIN_STREAK:
                        streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                    if current_streak > max_streak:
                        max_streak = current_streak
                    current_streak = 0
                    current_streak_start = None
                
                last_giornata = giornata
        
        # Se c'è una streak in corso alla fine dei dati
        if current_streak >= MIN_STREAK and ongoing_streak:
            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
        
        # Mostra solo se ci sono streak significative o max >= MIN_STREAK
        if streak_counts or max_streak >= MIN_STREAK:
            text += f"📍 <b>POSIZIONE {pos + 1}</b>\n"
            text += f"   Max streak: <b>{max_streak}</b> NG\n"
            
            if streak_counts:
                text += f"   Occorrenze (≥{MIN_STREAK}):\n"
                for length in sorted(streak_counts.keys(), reverse=True):
                    count = streak_counts[length]
                    volte = "volta" if count == 1 else "volte"
                    text += f"   • {length} NG → <b>{count}</b> {volte}\n"
            
            if ongoing_streak and current_streak >= MIN_STREAK:
                text += f"   ⚠️ <b>In corso: {current_streak} NG</b> (da G{current_streak_start})\n"
            
            text += "\n"
        else:
            # Mostra anche posizioni senza streak significative
            text += f"📍 <b>POSIZIONE {pos + 1}</b>\n"
            text += f"   Max streak: <b>{max_streak}</b> NG\n\n"
    
    text += f"🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

# ====== FUNZIONI PER STATISTICHE PER SQUADRA ======

def build_team_history_from_records(records):
    """Costruisce la mappa dei risultati per squadra dai record"""
    team_results = {team: [] for team in TEAMS}
    
    for record in records:
        matches = record.get("matches", [])
        for match in matches:
            teams_str = match.get("teams", "")
            if not teams_str:
                continue
            
            parts = teams_str.split("-")
            if len(parts) == 2:
                home_team = parts[0].strip().upper()
                away_team = parts[1].strip().upper()
                result = match.get("result", "")
                
                if home_team in TEAMS:
                    team_results[home_team].append({
                        "result": result,
                        "giornata": record.get("giornata", "?"),
                        "ora": record.get("ora", ""),
                        "opponent": away_team
                    })
                if away_team in TEAMS:
                    team_results[away_team].append({
                        "result": result,
                        "giornata": record.get("giornata", "?"),
                        "ora": record.get("ora", ""),
                        "opponent": home_team
                    })
    
    return team_results

async def generate_history_message_by_team(raw_data_only=False):
    """Genera storico per squadra - ultimi risultati di ogni squadra"""
    all_records = await _load_all_historical()
    
    if not all_records:
        if raw_data_only:
            return "(nessun dato)"
        return "📋 <b>STORICO PER SQUADRA</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    
    all_records.sort(key=lambda r: r.get("order", 0))
    team_history = build_team_history_from_records(all_records)
    
    data_rows = ""
    for team in TEAMS:
        results = team_history.get(team, [])
        if not results:
            continue
        
        # Prendi ultimi 10 risultati
        last_results = results[-10:]
        icons = "".join("🟢" if r["result"] == "G" else "🔴" for r in last_results)
        data_rows += f"<b>{team}</b>: {icons}\n"
    
    if raw_data_only:
        return data_rows.strip()
    
    text = f"📋 <b>STORICO PER SQUADRA</b>\n━━━━━━━━━━━━━━━━━\n\n"
    text += data_rows
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_info_message_by_team(threshold):
    """Genera info per vista squadra"""
    all_records = await _load_all_historical()
    team_history = build_team_history_from_records(all_records)
    
    text = "🏆 <b>INFO PER SQUADRA</b>\n━━━━━━━━━━━━━━━━━\n\n"
    
    for team in TEAMS:
        results = team_history.get(team, [])
        if not results:
            continue
        
        # Conta streak corrente
        consecutive_ng = 0
        consecutive_g = 0
        total_g = 0
        total_ng = 0
        
        for i in range(len(results) - 1, -1, -1):
            if results[i]["result"] == "NG":
                if consecutive_g == 0:
                    consecutive_ng += 1
                total_ng += 1
            else:
                if consecutive_ng == 0:
                    consecutive_g += 1
                total_g += 1
        
        if consecutive_ng >= threshold:
            icon = "🚨"
        elif consecutive_ng >= 4:
            icon = "⚠️"
        elif consecutive_ng > 0:
            icon = "🔴"
        else:
            icon = "🟢"
        
        text += f"{icon} <b>{team}</b>: "
        if consecutive_ng > 0:
            text += f"<b>{consecutive_ng} NG di fila</b>"
        else:
            text += f"{consecutive_g}x G"
        text += f"  [G:{total_g} NG:{total_ng}]\n"
    
    text += f"\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_streak_message_by_team(threshold):
    """Genera analisi streak NG per squadra"""
    MIN_STREAK = 5
    
    all_records = await _load_all_historical()
    
    if not all_records:
        return "🏆 <b>STREAK PER SQUADRA</b>\n\nNessun dato. Sincronizza con 📡 Sync."
    
    all_records.sort(key=lambda r: r.get("order", 0))
    team_history = build_team_history_from_records(all_records)
    
    text = f"🏆 <b>STREAK SQUADRE (≥{MIN_STREAK})</b>\n"
    text += f"📊 Analisi su <b>{len(all_records)}</b> giornate totali\n"
    text += "━━━━━━━━━━━━━━━━━\n\n"
    
    for team in TEAMS:
        results = team_history.get(team, [])
        if not results:
            continue
        
        streak_counts = {}
        current_streak = 0
        max_streak = 0
        ongoing_streak = False
        
        for i, r in enumerate(results):
            if r["result"] == "NG":
                current_streak += 1
                if i == len(results) - 1:
                    ongoing_streak = True
                    if current_streak > max_streak:
                        max_streak = current_streak
            else:
                if current_streak >= MIN_STREAK:
                    streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
                if current_streak > max_streak:
                    max_streak = current_streak
                current_streak = 0
        
        if current_streak >= MIN_STREAK and ongoing_streak:
            streak_counts[current_streak] = streak_counts.get(current_streak, 0) + 1
        
        if streak_counts or max_streak >= MIN_STREAK:
            text += f"🏆 <b>{team}</b>\n"
            text += f"   Max streak: <b>{max_streak}</b> NG\n"
            
            if streak_counts:
                text += f"   Occorrenze (≥{MIN_STREAK}):\n"
                for length in sorted(streak_counts.keys(), reverse=True):
                    count = streak_counts[length]
                    volte = "volta" if count == 1 else "volte"
                    text += f"   • {length} NG → <b>{count}</b> {volte}\n"
            
            if ongoing_streak and current_streak >= MIN_STREAK:
                text += f"   ⚠️ <b>In corso: {current_streak} NG</b>\n"
            
            text += "\n"
    
    text += f"🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def generate_streak_daily_message_by_team(threshold):
    """Genera analisi streak NG giornaliera per squadra"""
    MIN_STREAK = 5
    today = now_italy().strftime('%d/%m/%Y')
    
    cursor = db.fas_historical.find({"data_sisal": today}, {"_id": 0}).sort("order", 1)
    today_records = await cursor.to_list(length=500)
    
    if not today_records:
        return f"🏆 <b>STREAK GIORNALIERO SQUADRE</b>\n\n⚠️ Nessun dato per oggi ({today}).\n\n🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    
    team_history = build_team_history_from_records(today_records)
    
    text = f"🏆 <b>STREAK GIORNALIERO SQUADRE (≥{MIN_STREAK})</b>\n"
    text += f"📅 Data: <b>{today}</b>\n"
    text += f"📊 Giornate: <b>{len(today_records)}</b>\n"
    text += "━━━━━━━━━━━━━━━━━\n\n"
    
    for team in TEAMS:
        results = team_history.get(team, [])
        if not results:
            continue
        
        current_streak = 0
        max_streak = 0
        ongoing_streak = False
        
        for i, r in enumerate(results):
            if r["result"] == "NG":
                current_streak += 1
                if i == len(results) - 1:
                    ongoing_streak = True
                    if current_streak > max_streak:
                        max_streak = current_streak
            else:
                if current_streak > max_streak:
                    max_streak = current_streak
                current_streak = 0
        
        if max_streak >= MIN_STREAK or (ongoing_streak and current_streak >= MIN_STREAK):
            text += f"🏆 <b>{team}</b>\n"
            text += f"   Max streak: <b>{max_streak}</b> NG\n"
            if ongoing_streak and current_streak >= MIN_STREAK:
                text += f"   ⚠️ <b>In corso: {current_streak} NG</b>\n"
            text += "\n"
    
    text += f"🕐 {now_italy().strftime('%d/%m/%Y %H:%M')}"
    return text

async def delete_message_after_delay(bot_token, chat_id, message_id, delay_seconds):
    """Cancella un messaggio dopo un certo numero di secondi"""
    import asyncio
    await asyncio.sleep(delay_seconds)
    async with httpx.AsyncClient() as http:
        try:
            await http.post(
                f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id}
            )
            logger.info(f"[Telegram] Messaggio {message_id} cancellato dopo {delay_seconds}s")
        except Exception as e:
            logger.error(f"[Telegram] Errore cancellazione messaggio: {e}")

def build_callback_keyboard():
    """Costruisce inline keyboard con callback_data"""
    return {
        "inline_keyboard": [
            [
                {"text": "📈 Streak Totale", "callback_data": "streak"},
                {"text": "📊 Streak Giornaliero", "callback_data": "streak_daily"}
            ],
            [
                {"text": "📋 Storico Ultime 15", "callback_data": "history"},
                {"text": "ℹ️ Info", "callback_data": "info"}
            ]
        ]
    }

@api_router.post("/telegram/sync")
async def telegram_sync(request: Request):
    """Riceve dati sincronizzati dall'estensione e salva su MongoDB - supporta sync incrementale"""
    data = await request.json()
    
    bot_token = data.get("bot_token")
    chat_id = data.get("chat_id")
    history = data.get("history", [])
    threshold = data.get("threshold", 6)
    rendered_templates = data.get("rendered_templates", {})
    is_incremental = data.get("is_incremental", False)
    total_local = data.get("total_local", len(history))
    now = datetime.now(timezone.utc).isoformat()
    
    # Aggiorna stato sessione corrente
    telegram_state["bot_token"] = bot_token
    telegram_state["chat_id"] = chat_id
    telegram_state["threshold"] = threshold
    telegram_state["last_sync"] = now
    telegram_state["rendered_templates"] = rendered_templates
    
    # Per sync incrementale, mantieni la history esistente e aggiungi solo i nuovi
    if not is_incremental:
        telegram_state["history"] = history
    else:
        # Aggiungi i nuovi record alla history esistente (evita duplicati)
        existing_ids = set(str(r.get("id", "")) for r in telegram_state["history"])
        for r in history:
            if str(r.get("id", "")) not in existing_ids:
                telegram_state["history"].append(r)
    
    # Salva sessione corrente
    await db.fas_telegram_sync.update_one(
        {"_id": "current"},
        {"$set": {
            "bot_token": bot_token,
            "chat_id": chat_id,
            "history": telegram_state["history"],
            "threshold": threshold,
            "rendered_templates": rendered_templates,
            "last_sync": now,
            "total_local": total_local
        }},
        upsert=True
    )
    
    # Accumula nello storico permanente
    # Chiave: data + giornata + ora = no duplicati (stessa giornata a ore diverse = record diversi)
    new_count = 0
    for idx, record in enumerate(history):
        data_sisal = record.get("dataRicerca") or record.get("data") or "sconosciuta"
        giornata = str(record.get("giornata", "?"))
        ora = record.get("ora", "")
        key = f"{data_sisal}_{giornata}_{ora}"
        
        result = await db.fas_historical.update_one(
            {"_id": key},
            {"$set": {
                "data_sisal": data_sisal,
                "data_estrazione": record.get("data", ""),
                "giornata": giornata,
                "ora": ora,
                "matches": record.get("matches", []),
                "synced_at": now,
                "order": record.get("id", idx)
            }},
            upsert=True
        )
        if result.upserted_id:
            new_count += 1
    
    total_historical = await db.fas_historical.count_documents({})
    logger.info(f"[Telegram] Sync{'(incr)' if is_incremental else ''}: {len(history)} giornate, {new_count} nuove, storico: {total_historical}, local: {total_local}")
    return {"status": "ok", "synced_giornate": len(history), "new_records": new_count, "total_historical": total_historical, "incremental": is_incremental}

@api_router.post("/telegram/setup-webhook")
async def setup_webhook(request: Request):
    """Registra il webhook con Telegram"""
    data = await request.json()
    bot_token = data.get("bot_token") or telegram_state["bot_token"]
    
    if not bot_token:
        return {"status": "error", "message": "Bot token mancante"}
    
    webhook_url = f"{BACKEND_URL}/api/telegram/webhook"
    
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["callback_query"]}
        )
        result = resp.json()
    
    logger.info(f"[Telegram] Webhook setup: {result}")
    return result

@api_router.post("/telegram/remove-webhook")
async def remove_webhook(request: Request):
    """Rimuove il webhook"""
    data = await request.json()
    bot_token = data.get("bot_token") or telegram_state["bot_token"]
    
    if not bot_token:
        return {"status": "error", "message": "Bot token mancante"}
    
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
        )
        return resp.json()

@api_router.post("/telegram/send-menu")
async def send_menu(request: Request):
    """Invia il messaggio menu con bottoni callback al canale"""
    data = await request.json()
    bot_token = data.get("bot_token") or telegram_state["bot_token"]
    chat_id = data.get("chat_id") or telegram_state["chat_id"]
    
    if not bot_token or not chat_id:
        return {"status": "error", "message": "Bot token o Chat ID mancante"}
    
    text = "⚽ <b>FAS MONITOR - MENU</b>\n━━━━━━━━━━━━━━━━━\n\nSeleziona un'opzione:"
    
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": build_callback_keyboard()
            }
        )
        result = resp.json()
    
    logger.info(f"[Telegram] Menu inviato: {result.get('ok')}")
    return result

@api_router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Gestisce le callback_query da Telegram - invia risposte in PRIVATO all'utente"""
    update = await request.json()
    
    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}
    
    callback_id = callback["id"]
    callback_data = callback.get("data", "")
    
    # ID dell'utente che ha premuto il bottone (per messaggi privati)
    user_id = callback.get("from", {}).get("id")
    user_name = callback.get("from", {}).get("first_name", "Utente")
    
    # Chat ID del canale (per log)
    channel_id = callback.get("message", {}).get("chat", {}).get("id")
    
    bot_token = telegram_state["bot_token"]
    if not bot_token:
        logger.warning("[Telegram] Webhook: bot_token non disponibile")
        return {"ok": True}
    
    if not user_id:
        logger.warning("[Telegram] Webhook: user_id non disponibile")
        return {"ok": True}
    
    threshold = telegram_state.get("threshold", 7)
    saved_templates = telegram_state.get("rendered_templates", {})
    
    # Controlla se l'utente ha un template personalizzato con {data}
    user_tpl = saved_templates.get(callback_data)
    use_raw_data = user_tpl and "{data}" in user_tpl
    
    # Genera i dati freschi dal DB
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
    
    # Se l'utente ha un template personalizzato con {data}, sostituisci
    if use_raw_data:
        ts = now_italy().strftime('%d/%m/%Y %H:%M')
        text = user_tpl.replace("{data}", fresh_data).replace("{timestamp}", ts)
    else:
        text = fresh_data
    
    async with httpx.AsyncClient() as http:
        # 1. Rispondi alla callback (rimuove l'icona di caricamento)
        await http.post(
            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": "📨 Messaggio inviato in privato!"}
        )
        
        # 2. Invia il messaggio IN PRIVATO all'utente (non al canale!)
        response = await http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": user_id,  # Invia all'utente, non al canale
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": build_callback_keyboard()
            }
        )
        
        result = response.json()
        if not result.get("ok"):
            # Se l'utente non ha mai avviato il bot, non può ricevere messaggi privati
            error_desc = result.get("description", "")
            if "bot can't initiate" in error_desc.lower() or "chat not found" in error_desc.lower() or "forbidden" in error_desc.lower():
                # Risposta popup
                await http.post(
                    f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                    json={
                        "callback_query_id": callback_id, 
                        "text": "⚠️ Devi prima avviare il bot! Clicca il link nel canale.",
                        "show_alert": True
                    }
                )
                
                # Invia un messaggio nel CANALE con il link cliccabile
                msg_response = await http.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": channel_id,
                        "text": f"👋 <b>{user_name}</b>, per ricevere le statistiche in privato devi prima avviare il bot!\n\n👉 Clicca qui: @fas_alert_bot\n\nPoi premi <b>AVVIA/START</b> e riprova.",
                        "parse_mode": "HTML"
                    }
                )
                
                # Cancella il messaggio dopo 10 secondi
                msg_result = msg_response.json()
                if msg_result.get("ok"):
                    message_id = msg_result.get("result", {}).get("message_id")
                    if message_id:
                        # Aspetta 10 secondi e poi cancella
                        import asyncio
                        asyncio.create_task(delete_message_after_delay(bot_token, channel_id, message_id, 10))
                
                logger.warning(f"[Telegram] Utente {user_id} ({user_name}) non ha avviato il bot - inviato messaggio nel canale")
            else:
                logger.error(f"[Telegram] Errore invio messaggio: {error_desc}")
    
    logger.info(f"[Telegram] Callback '{callback_data}' inviata in privato a {user_name} ({user_id})")
    return {"ok": True}

@api_router.get("/telegram/status")
async def telegram_status():
    """Stato corrente del sistema Telegram"""
    total_historical = await db.fas_historical.count_documents({})
    dates = await db.fas_historical.distinct("data_sisal")
    
    return {
        "configured": telegram_state["bot_token"] is not None,
        "synced_giornate": len(telegram_state["history"]),
        "total_historical": total_historical,
        "historical_days": len(dates),
        "threshold": telegram_state["threshold"],
        "last_sync": telegram_state["last_sync"]
    }

# ====== HISTORICAL DATA ENDPOINTS ======

@api_router.get("/historical/list")
async def historical_list(date: str = None, page: int = 1, limit: int = 50):
    """Lista record storici. Filtro opzionale per data Sisal (es. ?date=05/02/2026)"""
    query = {}
    if date:
        query["data_sisal"] = date
    
    total = await db.fas_historical.count_documents(query)
    skip = (page - 1) * limit
    
    cursor = db.fas_historical.find(query, {"_id": 0}).sort([("data_sisal", 1), ("giornata", 1)]).skip(skip).limit(limit)
    records = await cursor.to_list(length=limit)
    
    # Converti giornata a int per ordinamento corretto
    def sort_key(r):
        d = r.get("data_sisal", "00/00/0000")
        parts = d.split("/")
        ds = f"{parts[2]}{parts[1]}{parts[0]}" if len(parts) == 3 else d
        try:
            g = int(r.get("giornata", 0))
        except (ValueError, TypeError):
            g = 0
        return (ds, g)
    
    records.sort(key=sort_key)
    
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit if limit > 0 else 0,
        "records": records
    }

@api_router.get("/historical/dates")
async def historical_dates():
    """Lista tutte le date Sisal disponibili con conteggio giornate"""
    pipeline = [
        {"$group": {"_id": "$data_sisal", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}}
    ]
    results = await db.fas_historical.aggregate(pipeline).to_list(length=1000)
    
    dates = []
    for r in results:
        dates.append({"date": r["_id"], "giornate": r["count"]})
    
    return {"total_dates": len(dates), "dates": dates}

@api_router.get("/historical/streak-analysis")
async def historical_streak_analysis():
    """Analisi streak NG completa da tutto lo storico"""
    text = await generate_streak_message_from_db(telegram_state.get("threshold", 7))
    return {"analysis": text}

@api_router.get("/historical/render-history")
async def render_history(view: str = "serie"):
    """Genera storico fresco dal DB per l'editor dell'estensione (solo dati grezzi)
    view: 'serie' per posizioni 1-6, 'squadra' per le 12 squadre
    """
    if view == "squadra":
        text = await generate_history_message_by_team(raw_data_only=True)
    else:
        text = await generate_history_message_from_db(raw_data_only=True)
    return {"text": text}

@api_router.get("/historical/render-info")
async def render_info(view: str = "serie"):
    """Genera info fresco dal DB per l'editor dell'estensione
    view: 'serie' per posizioni 1-6, 'squadra' per le 12 squadre
    """
    threshold = telegram_state.get("threshold", 7)
    if view == "squadra":
        text = await generate_info_message_by_team(threshold)
    else:
        text = await generate_info_message_from_db(threshold)
    return {"text": text}

@api_router.get("/historical/render-streak")
async def render_streak(view: str = "serie"):
    """Genera streak fresco dal DB per l'editor dell'estensione
    view: 'serie' per posizioni 1-6, 'squadra' per le 12 squadre
    """
    threshold = telegram_state.get("threshold", 7)
    if view == "squadra":
        text = await generate_streak_message_by_team(threshold)
    else:
        text = await generate_streak_message_from_db(threshold)
    return {"text": text}

@api_router.get("/historical/render-streak-daily")
async def render_streak_daily(view: str = "serie"):
    """Genera streak giornaliero fresco dal DB
    view: 'serie' per posizioni 1-6, 'squadra' per le 12 squadre
    """
    threshold = telegram_state.get("threshold", 7)
    if view == "squadra":
        text = await generate_streak_daily_message_by_team(threshold)
    else:
        text = await generate_streak_daily_message_from_db(threshold)
    return {"text": text}

@api_router.delete("/historical/clear")
async def historical_clear(date: str = None):
    """Cancella record storici. Se date specificata, cancella solo quella data"""
    if date:
        result = await db.fas_historical.delete_many({"data_sisal": date})
        return {"deleted": result.deleted_count, "date": date}
    else:
        result = await db.fas_historical.delete_many({})
        return {"deleted": result.deleted_count, "message": "Tutto lo storico cancellato"}

# ====== ENDPOINT PER DOWNLOAD ESTENSIONE ======
PUBLIC_DIR = ROOT_DIR.parent / "public"

@api_router.get("/download/crx")
async def download_crx():
    """Scarica il file CRX dell'estensione"""
    crx_path = PUBLIC_DIR / "fas-monitor.crx"
    if not crx_path.exists():
        return {"error": "File CRX non trovato"}
    return FileResponse(
        path=str(crx_path),
        filename="fas-monitor-v2.3.crx",
        media_type="application/x-chrome-extension",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@api_router.get("/download/zip")
async def download_zip():
    """Scarica il file ZIP dell'estensione"""
    zip_path = PUBLIC_DIR / "chrome-extension.zip"
    if not zip_path.exists():
        return {"error": "File ZIP non trovato"}
    return FileResponse(
        path=str(zip_path),
        filename="fas-monitor-v4.4.zip",
        media_type="application/zip",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@api_router.get("/download/complete")
async def download_complete():
    """Scarica il pacchetto completo (backend + estensione + guida)"""
    zip_path = PUBLIC_DIR / "fas-complete-package.zip"
    if not zip_path.exists():
        return {"error": "File non trovato"}
    return FileResponse(
        path=str(zip_path),
        filename="fas-monitor-complete-v4.4.zip",
        media_type="application/zip",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@api_router.get("/download/server-koyeb")
async def download_server_koyeb():
    """Scarica il file server.py per Koyeb"""
    file_path = PUBLIC_DIR / "server_koyeb.py"
    if not file_path.exists():
        return {"error": "File non trovato"}
    return FileResponse(
        path=str(file_path),
        filename="server.py",
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@api_router.get("/extension/version")
async def extension_version():
    """Restituisce la versione corrente dell'estensione"""
    return {"version": "4.0", "format": "crx3", "features": ["incremental_sync", "extended_timeout", "visual_indicator"]}

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],  # Permetti tutte le origini per l'extension
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def load_telegram_state():
    """Carica stato Telegram da MongoDB all'avvio"""
    saved = await db.fas_telegram_sync.find_one({"_id": "current"})
    if saved:
        telegram_state["bot_token"] = saved.get("bot_token")
        telegram_state["chat_id"] = saved.get("chat_id")
        telegram_state["history"] = saved.get("history", [])
        telegram_state["threshold"] = saved.get("threshold", 6)
        telegram_state["last_sync"] = saved.get("last_sync")
        telegram_state["rendered_templates"] = saved.get("rendered_templates", {})
        logger.info(f"[Telegram] Caricato da DB: {len(telegram_state['history'])} giornate, templates={list(telegram_state['rendered_templates'].keys())}")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

