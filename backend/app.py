from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os, sqlite3, json, time, random, requests
from contextlib import closing
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TG Clicker API")

# ---------------- CORS ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- PATHS ----------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")
INDEX_PATH = os.path.join(WEBAPP_DIR, "index.html")

app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

# ---------------- ENV ----------------
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "data.db"))
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "")
TRON_RECEIVE_ADDRESS = os.getenv("TRON_RECEIVE_ADDRESS", "")
TRC20_USDT_CONTRACT = os.getenv(
    "TRC20_USDT_CONTRACT",
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
)

TRONGRID_BASE = "https://api.trongrid.io"

# ---------------- PACKAGES ----------------
PACKAGES = {
    1: {"name": "Новичок", "price": 10.0, "taps": 100_000, "reward": 0.0002, "cap": 20},
    2: {"name": "Профи",   "price": 50.0, "taps": 100_000, "reward": 0.001,  "cap": 100},
    3: {"name": "VIP",     "price": 100.0,"taps": 100_000, "reward": 0.002,  "cap": 200},
}

# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db()) as conn:
        cur = conn.cursor()

        cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS taps (
            tg_id INTEGER PRIMARY KEY,
            taps_available INTEGER DEFAULT 0,
            tap_reward REAL DEFAULT 0,
            earn_cap_remaining REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            package_id INTEGER,
            base_price REAL,
            unique_amount REAL,
            status TEXT,
            txid TEXT,
            created_at INTEGER,
            paid_at INTEGER
        );
        """)

        conn.commit()

init_db()

# ---------------- MODELS ----------------
class CreateInvoiceIn(BaseModel):
    tg_id: int
    package_id: int

class CheckInvoiceIn(BaseModel):
    tg_id: int
    invoice_id: int

# ---------------- TRON ----------------
def tron_headers():
    h = {"accept": "application/json"}
    if TRONGRID_API_KEY:
        h["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return h

def check_tron_payment(amount: float, after_ts: int):
    url = f"{TRONGRID_BASE}/v1/accounts/{TRON_RECEIVE_ADDRESS}/transactions/trc20"
    r = requests.get(
        url,
        headers=tron_headers(),
        params={
            "only_confirmed": "true",
            "limit": 50,
            "contract_address": TRC20_USDT_CONTRACT
        },
        timeout=15
    )
    r.raise_for_status()
    for tx in r.json().get("data", []):
        val = int(tx["value"]) / 1_000_000
        ts = tx["block_timestamp"] // 1000
        if abs(val - amount) < 0.000001 and ts >= after_ts:
            return True, tx["transaction_id"]
    return False, None

# ---------------- ROUTES ----------------
@app.get("/")
def home():
    return FileResponse(INDEX_PATH) if os.path.exists(INDEX_PATH) else {"ok": True}

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": DB_PATH,
        "tron_ready": bool(TRON_RECEIVE_ADDRESS)
    }

@app.get("/api/packages")
def packages():
    return {
        "ok": True,
        "packages": PACKAGES,
        "address": TRON_RECEIVE_ADDRESS,
        "network": "TRON (TRC20 USDT)"
    }

@app.post("/api/payments/create")
def create_payment(data: CreateInvoiceIn):
    if data.package_id not in PACKAGES:
        return JSONResponse({"error": "bad package"}, 400)

    pkg = PACKAGES[data.package_id]
    now = int(time.time())

    with closing(db()) as conn:
        cur = conn.cursor()

        cur.execute("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (data.tg_id,))
        conn.commit()

        cur.execute("""
            INSERT INTO invoices
            (tg_id, package_id, base_price, unique_amount, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (data.tg_id, data.package_id, pkg["price"], pkg["price"], now))
        conn.commit()

        invoice_id = cur.lastrowid
        unique = round(pkg["price"] + random.randint(1, 9999)/1_000_000, 6)

        cur.execute("UPDATE invoices SET unique_amount=? WHERE id=?",
                    (unique, invoice_id))
        conn.commit()

        return {
            "ok": True,
            "invoice": {
                "id": invoice_id,
                "amount_usdt": unique,
                "address": TRON_RECEIVE_ADDRESS
            }
        }

@app.post("/api/payments/check")
def check_payment(data: CheckInvoiceIn):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE id=? AND tg_id=?",
                    (data.invoice_id, data.tg_id))
        inv = cur.fetchone()
        if not inv:
            return JSONResponse({"error": "invoice not found"}, 404)

        if inv["status"] == "paid":
            return {"ok": True, "paid": True}

        paid, txid = check_tron_payment(inv["unique_amount"], inv["created_at"])
        if not paid:
            return {"ok": True, "paid": False}

        cur.execute("""
            UPDATE invoices
            SET status='paid', txid=?, paid_at=?
            WHERE id=?
        """, (txid, int(time.time()), inv["id"]))

        pkg = PACKAGES[inv["package_id"]]
        cur.execute("""
            INSERT OR IGNORE INTO taps (tg_id) VALUES (?)
        """, (data.tg_id,))

        cur.execute("""
            UPDATE taps SET
            taps_available = taps_available + ?,
            tap_reward = ?,
            earn_cap_remaining = earn_cap_remaining + ?
            WHERE tg_id=?
        """, (pkg["taps"], pkg["reward"], pkg["cap"], data.tg_id))

        conn.commit()

        return {"ok": True, "paid": True, "package": pkg}
