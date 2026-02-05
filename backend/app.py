from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os, sqlite3, time, random, requests, traceback
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
TRC20_USDT_CONTRACT = os.getenv("TRC20_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").strip()
TRONGRID_BASE = "https://api.trongrid.io"

PAYMENT_TIME_SLOP_SEC = int(os.getenv("PAYMENT_TIME_SLOP_SEC", "120"))
MAX_OVERPAY = float(os.getenv("MAX_OVERPAY", "1000"))

WELCOME_TAPS = int(os.getenv("WELCOME_TAPS", "10000"))
WELCOME_REWARD = float(os.getenv("WELCOME_REWARD", "0.0001"))
WELCOME_CAP = float(os.getenv("WELCOME_CAP", "1.0"))

# ---------------- PACKAGES ----------------
PACKAGES = {
    1: {"name": "Новичок", "price": 10.0,  "taps": 100_000, "reward": 0.0002, "cap": 20},
    2: {"name": "Профи",   "price": 50.0,  "taps": 100_000, "reward": 0.001,  "cap": 100},
    3: {"name": "Кит",     "price": 100.0, "taps": 100_000, "reward": 0.002,  "cap": 200},
}

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def table_exists(cur, name: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None

def table_cols(cur, table: str):
    cur.execute(f"PRAGMA table_info({table})")
    rows = cur.fetchall()
    return [r["name"] for r in rows]

def ensure_column(cur, table: str, col: str, col_def: str):
    cols = table_cols(cur, table)
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

def recreate_table(cur, old_name: str, new_ddl: str, copy_sql: str | None = None):
    cur.execute(f"ALTER TABLE {old_name} RENAME TO {old_name}_old")
    cur.executescript(new_ddl)
    if copy_sql:
        cur.execute(copy_sql)
    cur.execute(f"DROP TABLE {old_name}_old")

def init_db():
    with closing(db()) as conn:
        cur = conn.cursor()

        # Создаём таблицы, если их нет
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
            balance_usdt REAL DEFAULT 0.0
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

        # Миграция: добавляем новые поля в taps, если их нет
        ensure_column(cur, "taps", "taps_total", "INTEGER DEFAULT 0")
        ensure_column(cur, "taps", "balance_usdt", "REAL DEFAULT 0.0")

        # Если нужно перенести старые данные — делаем аккуратно
        cols = table_cols(cur, "taps")
        if "balance_usdt" not in cols:
            cur.execute("ALTER TABLE taps ADD COLUMN balance_usdt REAL DEFAULT 0.0")

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
        params={"only_confirmed": "true", "limit": limit, "contract_address": TRC20_USDT_CONTRACT},
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
    cur = conn.cursor()

    cur.execute("INSERT OR IGNORE INTO users (tg_id, bonus_given) VALUES (?, 0)", (tg_id,))
    cur.execute("""
        INSERT OR IGNORE INTO taps (
            tg_id, taps_available, tap_reward, earn_cap_remaining, taps_total, balance_usdt
        ) VALUES (?, ?, ?, ?, 0, 0.0)
    """, (tg_id, WELCOME_TAPS, WELCOME_REWARD, WELCOME_CAP))

    cur.execute("SELECT bonus_given FROM users WHERE tg_id=? LIMIT 1", (tg_id,))
    u = cur.fetchone()
    bonus_given = int(u["bonus_given"]) if u else 0

    if bonus_given == 0:
        cur.execute("""
            UPDATE taps SET
              taps_available = taps_available + ?,
              tap_reward = CASE WHEN tap_reward > 0 THEN tap_reward ELSE ? END,
              earn_cap_remaining = earn_cap_remaining + ?
            WHERE tg_id=?
        """, (WELCOME_TAPS, WELCOME_REWARD, WELCOME_CAP, tg_id))
        cur.execute("UPDATE users SET bonus_given=1 WHERE tg_id=?", (tg_id,))

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
            ensure_user_and_bonus(conn, tg_id)
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    u.tg_id,
                    u.bonus_given,
                    t.taps_available,
                    t.tap_reward,
                    t.earn_cap_remaining,
                    t.taps_total,
                    t.balance_usdt
                FROM users u
                LEFT JOIN taps t ON t.tg_id = u.tg_id
                WHERE u.tg_id = ?
            """, (tg_id,))
            row = cur.fetchone()

            if not row:
                return {"ok": False, "error": "User not found"}

            return {
                "ok": True,
                "balance": {"balance_usdt": float(row["balance_usdt"] or 0.0)},
                "taps": {
                    "taps_available": int(row["taps_available"] or 0),
                    "taps_total": int(row["taps_total"] or 0),
                    "tap_reward": float(row["tap_reward"] or 0.0),
                    "earn_cap_remaining": float(row["earn_cap_remaining"] or 0.0)
                },
                "bonus_given": int(row["bonus_given"] or 0)
            }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:2000]}
        )

@app.post("/api/tap")
async def tap(data: dict):
    tg_id = data.get("tg_id")
    if not tg_id:
        return {"ok": False, "error": "No tg_id"}

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT taps_available, tap_reward, earn_cap_remaining, taps_total, balance_usdt
            FROM taps WHERE tg_id = ?
        """, (tg_id,))
        row = cur.fetchone()

        if not row:
            return {"ok": False, "error": "User not found"}

        taps_available = int(row["taps_available"])
        if taps_available <= 0:
            return {
                "ok": True,
                "balance_usdt": float(row["balance_usdt"] or 0.0),
                "taps_available": 0,
                "tap_reward": float(row["tap_reward"]),
                "taps_total": int(row["taps_total"] or 0),
                "earn_cap_remaining": float(row["earn_cap_remaining"])
            }

        reward = float(row["tap_reward"])
        new_balance = float(row["balance_usdt"] or 0.0) + reward
        new_taps = taps_available - 1
        new_total = int(row["taps_total"] or 0) + 1
        new_cap = float(row["earn_cap_remaining"]) - reward if float(row["earn_cap_remaining"]) > 0 else 0.0

        cur.execute("""
            UPDATE taps SET
                balance_usdt = ?,
                taps_available = ?,
                taps_total = ?,
                earn_cap_remaining = ?
            WHERE tg_id = ?
        """, (new_balance, new_taps, new_total, new_cap, tg_id))

        conn.commit()

        return {
            "ok": True,
            "balance_usdt": new_balance,
            "taps_available": new_taps,
            "tap_reward": reward,
            "taps_total": new_total,
            "earn_cap_remaining": new_cap
        }

@app.get("/api/referrals/{tg_id}")
async def get_referrals(tg_id: int):
    # Пока заглушка (добавь позже реальную таблицу рефералов)
    return {
        "ok": True,
        "referrals": [],
        "invited_count": 0,
        "bonus_total": 0.0
    }

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