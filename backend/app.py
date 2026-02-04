from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os, sqlite3, time, random, requests
from contextlib import closing
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TG Clicker API")
BUILD = "9df0383"
@app.get("/api/version")
def version():
    return {"ok": True, "build": BUILD}

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
TRON_RECEIVE_ADDRESS = os.getenv("TRON_RECEIVE_ADDRESS", "").strip()
TRC20_USDT_CONTRACT = os.getenv("TRC20_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").strip()
TRONGRID_BASE = "https://api.trongrid.io"

# анти-ложные совпадения: берем только платежи после создания инвойса (с небольшим запасом)
PAYMENT_TIME_SLOP_SEC = int(os.getenv("PAYMENT_TIME_SLOP_SEC", "120"))  # 120 сек
# допуск по переплате (можешь поставить 0 если хочешь "любой >= price")
MAX_OVERPAY = float(os.getenv("MAX_OVERPAY", "1000"))  # по умолчанию огромный — переплата не мешает

# ---------------- PACKAGES ----------------
PACKAGES = {
    1: {"name": "Новичок", "price": 10.0,  "taps": 100_000, "reward": 0.0002, "cap": 20},
    2: {"name": "Профи",   "price": 50.0,  "taps": 100_000, "reward": 0.001,  "cap": 100},
    3: {"name": "VIP",     "price": 100.0, "taps": 100_000, "reward": 0.002,  "cap": 200},
}

# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(cur, table: str, col: str, col_def: str):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r["name"] for r in cur.fetchall()]
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

def init_db():
    with closing(db()) as conn:
        cur = conn.cursor()

        # базовые таблицы
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

        CREATE TABLE IF NOT EXISTS processed_tx (
            txid TEXT PRIMARY KEY,
            invoice_id INTEGER,
            tg_id INTEGER,
            amount REAL,
            ts INTEGER
        );
        """)

        # миграции (если у тебя старый users без tg_id)
        # если tg_id нет — проще пересоздать users, но SQLite не умеет DROP COLUMN.
        # Поэтому: проверяем, если users есть, но tg_id нет — создаем новую таблицу users_new и меняем.
        cur.execute("PRAGMA table_info(users)")
        users_cols = [r[1] for r in cur.fetchall()]  # pragma via tuple
        if "tg_id" not in users_cols:
            cur.executescript("""
            ALTER TABLE users RENAME TO users_old;
            CREATE TABLE users (tg_id INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO users(tg_id)
            SELECT id FROM users_old WHERE id IS NOT NULL;
            DROP TABLE users_old;
            """)

        # убедимся, что invoices имеет нужные поля (на случай старых версий)
        ensure_column(cur, "invoices", "base_price", "REAL")
        ensure_column(cur, "invoices", "unique_amount", "REAL")
        ensure_column(cur, "invoices", "status", "TEXT")
        ensure_column(cur, "invoices", "txid", "TEXT")
        ensure_column(cur, "invoices", "created_at", "INTEGER")
        ensure_column(cur, "invoices", "paid_at", "INTEGER")

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

def get_recent_trc20_transfers(limit: int = 50):
    if not TRON_RECEIVE_ADDRESS:
        raise RuntimeError("TRON_RECEIVE_ADDRESS is empty")

    url = f"{TRONGRID_BASE}/v1/accounts/{TRON_RECEIVE_ADDRESS}/transactions/trc20"
    r = requests.get(
        url,
        headers=tron_headers(),
        params={
            "only_confirmed": "true",
            "limit": limit,
            "contract_address": TRC20_USDT_CONTRACT
        },
        timeout=20
    )
    r.raise_for_status()
    return r.json().get("data", [])

def find_payment_for_invoice(base_price: float, created_at: int, conn: sqlite3.Connection):
    """
    Ищем первый платеж:
      - val >= base_price
      - ts >= created_at - PAYMENT_TIME_SLOP_SEC
      - txid еще не встречался в processed_tx
      - (опционально) val <= base_price + MAX_OVERPAY
    """
    after_ts = int(created_at) - PAYMENT_TIME_SLOP_SEC
    cur = conn.cursor()

    txs = get_recent_trc20_transfers(limit=50)

    for tx in txs:
        try:
            txid = tx["transaction_id"]
            val = int(tx["value"]) / 1_000_000
            ts = tx["block_timestamp"] // 1000
        except Exception:
            continue

        if ts < after_ts:
            continue

        if val < float(base_price):
            continue

        if val > float(base_price) + float(MAX_OVERPAY):
            continue

        # уже использовали этот txid?
        cur.execute("SELECT 1 FROM processed_tx WHERE txid=? LIMIT 1", (txid,))
        if cur.fetchone():
            continue

        return True, txid, val, ts

    return False, None, None, None

# ---------------- ROUTES ----------------
@app.get("/")
def home():
    return FileResponse(INDEX_PATH) if os.path.exists(INDEX_PATH) else {"ok": True}

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": DB_PATH,
        "tron_ready": bool(TRON_RECEIVE_ADDRESS),
    }
BUILD = "v1"

@app.get("/api/version")
def version():
    return {"ok": True, "build": BUILD}

@app.get("/api/packages")
def packages():
    return {
        "ok": True,
        "packages": PACKAGES,
        "address": TRON_RECEIVE_ADDRESS,
        "network": "TRON (TRC20 USDT)",
        "rules": {
            "min_amount": ">= package price",
            "time_slop_sec": PAYMENT_TIME_SLOP_SEC
        }
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
        cur.execute("INSERT OR IGNORE INTO taps (tg_id) VALUES (?)", (data.tg_id,))

        # для совместимости со старым фронтом оставляем unique_amount,
        # но логика проверки больше НЕ требует точного совпадения
        unique = round(float(pkg["price"]) + random.randint(1, 9999) / 1_000_000, 6)

        cur.execute("""
            INSERT INTO invoices
            (tg_id, package_id, base_price, unique_amount, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (data.tg_id, data.package_id, float(pkg["price"]), unique, now))
        conn.commit()

        invoice_id = cur.lastrowid

        return {
            "ok": True,
            "invoice": {
                "id": invoice_id,
                "amount_usdt": unique,   # показываем как "рекомендованную сумму"
                "min_amount_usdt": float(pkg["price"]),  # вот это важно для UI
                "address": TRON_RECEIVE_ADDRESS
            }
        }

@app.post("/api/payments/check")
def check_payment(data: CheckInvoiceIn):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE id=? AND tg_id=?", (data.invoice_id, data.tg_id))
        inv = cur.fetchone()
        if not inv:
            return JSONResponse({"error": "invoice not found"}, 404)

        if inv["status"] == "paid":
            return {"ok": True, "paid": True, "txid": inv["txid"]}

        base_price = float(inv["base_price"] or 0.0)
        if base_price <= 0:
            # fallback если база не записалась
            pkg = PACKAGES.get(int(inv["package_id"]))
            base_price = float(pkg["price"]) if pkg else float(inv["unique_amount"] or 0.0)

        found, txid, val, ts = find_payment_for_invoice(base_price, int(inv["created_at"]), conn)
        if not found:
            return {"ok": True, "paid": False}

        # фиксируем tx как использованный
        cur.execute(
            "INSERT OR IGNORE INTO processed_tx (txid, invoice_id, tg_id, amount, ts) VALUES (?, ?, ?, ?, ?)",
            (txid, int(inv["id"]), int(inv["tg_id"]), float(val), int(ts))
        )

        # отмечаем invoice как paid
        cur.execute(
            "UPDATE invoices SET status='paid', txid=?, paid_at=? WHERE id=?",
            (txid, int(time.time()), int(inv["id"]))
        )

        # начисляем пакет
        pkg = PACKAGES[int(inv["package_id"])]
        cur.execute("""
            UPDATE taps SET
              taps_available = taps_available + ?,
              tap_reward = ?,
              earn_cap_remaining = earn_cap_remaining + ?
            WHERE tg_id=?
        """, (int(pkg["taps"]), float(pkg["reward"]), float(pkg["cap"]), int(data.tg_id)))

        conn.commit()
        return {"ok": True, "paid": True, "txid": txid, "amount": val, "package": pkg}
