from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import sqlite3
import time
import random
import requests
import traceback
from contextlib import closing
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="TG Clicker API")

BUILD = os.getenv("BUILD") or os.getenv("RENDER_GIT_COMMIT") or "local"

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

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()
TRON_RECEIVE_ADDRESS = os.getenv("TRON_RECEIVE_ADDRESS", "").strip()
TRC20_USDT_CONTRACT = os.getenv(
    "TRC20_USDT_CONTRACT",
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
).strip()
TRONGRID_BASE = "https://api.trongrid.io"

PAYMENT_TIME_SLOP_SEC = int(os.getenv("PAYMENT_TIME_SLOP_SEC", "120"))
MAX_OVERPAY = float(os.getenv("MAX_OVERPAY", "1000"))

WELCOME_TAPS = int(os.getenv("WELCOME_TAPS", "10000"))
WELCOME_REWARD = float(os.getenv("WELCOME_REWARD", "0.0001"))
WELCOME_CAP = float(os.getenv("WELCOME_CAP", "1.0"))

REFERRAL_BONUS = float(os.getenv("REFERRAL_BONUS", "0.1"))

# ---------------- PACKAGES ----------------
PACKAGES = {
    1: {"name": "Новичок", "price": 10.0, "taps": 100_000, "reward": 0.0002, "cap": 20},
    2: {"name": "Профи",   "price": 50.0, "taps": 100_000, "reward": 0.001,  "cap": 100},
    3: {"name": "VIP",     "price": 100.0, "taps": 100_000, "reward": 0.002, "cap": 200},
}

# ================== DB ==================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # чуть стабильнее при параллельных запросах
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def table_cols(cur: sqlite3.Cursor, table: str):
    cur.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    return [r["name"] for r in rows]

def ensure_column(cur: sqlite3.Cursor, table: str, col: str, col_def: str):
    cols = table_cols(cur, table)
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

def init_db():
    with closing(db()) as conn:
        cur = conn.cursor()

        cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            bonus_given INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS taps (
            tg_id INTEGER PRIMARY KEY,
            taps_available INTEGER DEFAULT 0,
            tap_reward REAL DEFAULT 0,
            earn_cap_remaining REAL DEFAULT 0,
            taps_total INTEGER DEFAULT 0,
            balance_usdt REAL DEFAULT 0.0,
            FOREIGN KEY(tg_id) REFERENCES users(tg_id)
        );

        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id INTEGER,
            referred_id INTEGER PRIMARY KEY,
            bonus_paid INTEGER DEFAULT 0,
            FOREIGN KEY(referrer_id) REFERENCES users(tg_id)
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

        # мягкие миграции
        if table_exists(cur, "taps"):
            ensure_column(cur, "taps", "taps_total", "INTEGER DEFAULT 0")
            ensure_column(cur, "taps", "balance_usdt", "REAL DEFAULT 0.0")

        conn.commit()

init_db()

# ================== MODELS ==================
class CreateInvoiceIn(BaseModel):
    tg_id: int
    package_id: int

class CheckInvoiceIn(BaseModel):
    tg_id: int
    invoice_id: int

# ================== TRON ==================
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

        cur.execute("SELECT 1 FROM processed_tx WHERE txid=? LIMIT 1", (txid,))
        if cur.fetchone():
            continue

        return True, txid, float(val), int(ts)

    return False, None, None, None

# ================== CORE HELPERS ==================
def ensure_user_and_bonus(conn: sqlite3.Connection, tg_id: int):
    """
    Гарантирует наличие строк в users/taps.
    Выдаёт welcome бонус атомарно (ровно 1 раз) и НЕ перетирает купленные значения.
    """
    cur = conn.cursor()

    # создаём строки (без бонусов)
    cur.execute("INSERT OR IGNORE INTO users (tg_id, bonus_given) VALUES (?, 0)", (tg_id,))
    cur.execute("""
        INSERT OR IGNORE INTO taps
        (tg_id, taps_available, tap_reward, earn_cap_remaining, taps_total, balance_usdt)
        VALUES (?, 0, 0, 0, 0, 0.0)
    """, (tg_id,))
    conn.commit()

    # атомарно: кто первый перевёл bonus_given 0->1, тот начисляет
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("UPDATE users SET bonus_given=1 WHERE tg_id=? AND bonus_given=0", (tg_id,))
    first_time = (cur.rowcount == 1)

    if first_time:
        # добавляем бонус, не затираем
        cur.execute("""
            UPDATE taps SET
              taps_available = taps_available + ?,
              tap_reward = CASE WHEN tap_reward > 0 THEN tap_reward ELSE ? END,
              earn_cap_remaining = earn_cap_remaining + ?
            WHERE tg_id=?
        """, (WELCOME_TAPS, WELCOME_REWARD, WELCOME_CAP, tg_id))

    conn.commit()

# ================== ROUTES ==================
@app.get("/")
def home():
    return FileResponse(INDEX_PATH) if os.path.exists(INDEX_PATH) else {"ok": True}

@app.get("/api/health")
def health():
    return {"ok": True, "db": DB_PATH, "tron_ready": bool(TRON_RECEIVE_ADDRESS)}

@app.get("/api/version")
def version():
    return {"ok": True, "build": BUILD, "ts": int(time.time())}

@app.get("/api/packages")
def packages():
    return {
        "ok": True,
        "packages": PACKAGES,
        "address": TRON_RECEIVE_ADDRESS,
        "network": "TRON (TRC20 USDT)",
        "rules": {
            "min_amount": ">= package price",
            "time_slop_sec": PAYMENT_TIME_SLOP_SEC,
            "max_overpay": MAX_OVERPAY,
        }
    }

@app.get("/api/user/{tg_id}")
def get_user(tg_id: int):
    try:
        with closing(db()) as conn:
            ensure_user_and_bonus(conn, int(tg_id))
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    u.tg_id as userId,
                    COALESCE(t.taps_available, 0) as taps_available,
                    COALESCE(t.tap_reward, 0) as tap_reward,
                    COALESCE(t.earn_cap_remaining, 0) as earn_cap_remaining,
                    COALESCE(t.taps_total, 0) as taps_total,
                    COALESCE(t.balance_usdt, 0.0) as balance_usdt,
                    COALESCE(u.bonus_given, 0) as bonus_given
                FROM users u
                LEFT JOIN taps t ON t.tg_id = u.tg_id
                WHERE u.tg_id = ?
                LIMIT 1
            """, (int(tg_id),))
            row = cur.fetchone()

            if not row:
                return {
                    "status": "ok",
                    "userId": int(tg_id),
                    "taps_available": 0,
                    "tap_reward": 0.0,
                    "earn_cap_remaining": 0.0,
                    "taps_total": 0,
                    "balance_usdt": 0.0,
                    "bonus_given": 0
                }

            return {
                "status": "ok",
                "userId": int(row["userId"]),
                "taps_available": int(row["taps_available"]),
                "tap_reward": float(row["tap_reward"]),
                "earn_cap_remaining": float(row["earn_cap_remaining"]),
                "taps_total": int(row["taps_total"]),
                "balance_usdt": float(row["balance_usdt"]),
                "bonus_given": int(row["bonus_given"]),
            }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:4000]}
        )

@app.post("/api/tap")
def tap(data: dict):
    tg_id = data.get("tg_id")
    if not tg_id:
        return {"ok": False, "error": "No tg_id"}

    try:
        with closing(db()) as conn:
            ensure_user_and_bonus(conn, int(tg_id))
            cur = conn.cursor()

            # блокируем БД на запись во время одного тапа, чтобы два устройства не пересчитали одновременно
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("""
                SELECT taps_available, tap_reward, earn_cap_remaining, taps_total, balance_usdt
                FROM taps WHERE tg_id = ?
            """, (int(tg_id),))
            row = cur.fetchone()

            if not row:
                conn.commit()
                return {"ok": False, "error": "User not found"}

            taps_available = int(row["taps_available"])
            reward = float(row["tap_reward"])
            cap = float(row["earn_cap_remaining"])
            balance = float(row["balance_usdt"])
            total = int(row["taps_total"])

            # если нет тапок или кап исчерпан или reward=0 — просто возвращаем текущее состояние
            if taps_available <= 0 or reward <= 0 or cap <= 0:
                conn.commit()
                return {
                    "ok": True,
                    "balance_usdt": balance,
                    "taps_available": max(0, taps_available),
                    "tap_reward": reward,
                    "taps_total": total,
                    "earn_cap_remaining": max(0.0, cap),
                }

            new_balance = balance + reward
            new_taps = taps_available - 1
            new_total = total + 1
            new_cap = cap - reward
            if new_cap < 0:
                new_cap = 0.0

            cur.execute("""
                UPDATE taps SET
                    balance_usdt = ?,
                    taps_available = ?,
                    taps_total = ?,
                    earn_cap_remaining = ?
                WHERE tg_id = ?
            """, (new_balance, new_taps, new_total, new_cap, int(tg_id)))

            conn.commit()

            return {
                "ok": True,
                "balance_usdt": new_balance,
                "taps_available": new_taps,
                "tap_reward": reward,
                "taps_total": new_total,
                "earn_cap_remaining": new_cap
            }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:4000]}
        )

@app.get("/api/referrals/{tg_id}")
def get_referrals(tg_id: int):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.referred_id, r.bonus_paid
            FROM referrals r
            WHERE r.referrer_id = ?
        """, (int(tg_id),))
        referrals = cur.fetchall()
        invited_count = len(referrals)
        bonus_total = sum(REFERRAL_BONUS for r in referrals if int(r["bonus_paid"]) == 0)
        return {
            "ok": True,
            "referrals": [dict(r) for r in referrals],
            "invited_count": invited_count,
            "bonus_total": bonus_total
        }

# ---------------- PAYMENTS (оставляем как есть по логике) ----------------
@app.post("/api/payments/create")
def create_payment(data: CreateInvoiceIn):
    if data.package_id not in PACKAGES:
        return JSONResponse({"error": "bad package"}, 400)

    pkg = PACKAGES[data.package_id]
    now = int(time.time())

    with closing(db()) as conn:
        ensure_user_and_bonus(conn, int(data.tg_id))
        cur = conn.cursor()

        unique = round(float(pkg["price"]) + random.randint(1, 9999) / 1_000_000, 6)

        cur.execute("""
            INSERT INTO invoices (tg_id, package_id, base_price, unique_amount, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (int(data.tg_id), int(data.package_id), float(pkg["price"]), unique, now))
        conn.commit()

        invoice_id = cur.lastrowid
        return {
            "ok": True,
            "invoice": {
                "id": int(invoice_id),
                "amount_usdt": unique,
                "min_amount_usdt": float(pkg["price"]),
                "address": TRON_RECEIVE_ADDRESS
            }
        }

@app.post("/api/payments/check")
def check_payment(data: CheckInvoiceIn):
    with closing(db()) as conn:
        ensure_user_and_bonus(conn, int(data.tg_id))
        cur = conn.cursor()

        cur.execute("SELECT * FROM invoices WHERE id=? AND tg_id=?", (int(data.invoice_id), int(data.tg_id)))
        inv = cur.fetchone()
        if not inv:
            return JSONResponse({"error": "invoice not found"}, 404)

        if (inv["status"] or "") == "paid":
            return {"ok": True, "paid": True, "txid": inv["txid"]}

        base_price = float(inv["base_price"]) if inv["base_price"] is not None else 0.0
        if base_price <= 0:
            pkg = PACKAGES.get(int(inv["package_id"]))
            base_price = float(pkg["price"]) if pkg else float(inv["unique_amount"] or 0.0)

        found, txid, val, ts = find_payment_for_invoice(base_price, int(inv["created_at"]), conn)
        if not found:
            return {"ok": True, "paid": False}

        cur.execute(
            "INSERT OR IGNORE INTO processed_tx (txid, invoice_id, tg_id, amount, ts) VALUES (?, ?, ?, ?, ?)",
            (txid, int(inv["id"]), int(inv["tg_id"]), float(val), int(ts))
        )

        cur.execute(
            "UPDATE invoices SET status='paid', txid=?, paid_at=? WHERE id=?",
            (txid, int(time.time()), int(inv["id"]))
        )

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
