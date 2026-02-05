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
    """
    Аккуратно пересоздаёт таблицу:
      old_name -> old_name_old
      создаёт новую по new_ddl
      копирует данные по copy_sql (если задан)
      удаляет old
    """
    cur.execute(f"ALTER TABLE {old_name} RENAME TO {old_name}_old")
    cur.executescript(new_ddl)
    if copy_sql:
        cur.execute(copy_sql)
    cur.execute(f"DROP TABLE {old_name}_old")

def init_db():
    with closing(db()) as conn:
        cur = conn.cursor()

        # 1) если таблиц нет — создаём новую схему
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

        # 2) миграции users (если старая таблица без tg_id / с id / другая)
        if table_exists(cur, "users"):
            cols = table_cols(cur, "users")
            if "tg_id" not in cols:
                # пытаемся перенести из 'id' если было
                new_ddl = """
                CREATE TABLE users (
                    tg_id INTEGER PRIMARY KEY,
                    bonus_given INTEGER DEFAULT 0
                );
                """
                copy_sql = None
                if "id" in cols:
                    copy_sql = "INSERT OR IGNORE INTO users(tg_id, bonus_given) SELECT id, 0 FROM users_old WHERE id IS NOT NULL"
                recreate_table(cur, "users", new_ddl, copy_sql)
            else:
                ensure_column(cur, "users", "bonus_given", "INTEGER DEFAULT 0")

        # 3) миграции taps (часто ломается именно taps)
        if table_exists(cur, "taps"):
            cols = table_cols(cur, "taps")
            if "tg_id" not in cols:
                # если вдруг было user_id
                new_ddl = """
                CREATE TABLE taps (
                    tg_id INTEGER PRIMARY KEY,
                    taps_available INTEGER DEFAULT 0,
                    tap_reward REAL DEFAULT 0,
                    earn_cap_remaining REAL DEFAULT 0,
                    taps_total INTEGER DEFAULT 0,
                    balance_usdt REAL DEFAULT 0.0
                );
                """
                copy_sql = None
                if "user_id" in cols:
                    copy_sql = """
                    INSERT OR IGNORE INTO taps(tg_id, taps_available, tap_reward, earn_cap_remaining, taps_total, balance_usdt)
                    SELECT user_id,
                           COALESCE(taps_available,0),
                           COALESCE(tap_reward,0),
                           COALESCE(earn_cap_remaining,0),
                           COALESCE(taps_total,0),
                           COALESCE(balance_usdt,0.0)
                    FROM taps_old
                    """
                recreate_table(cur, "taps", new_ddl, copy_sql)
            else:
                ensure_column(cur, "taps", "taps_available", "INTEGER DEFAULT 0")
                ensure_column(cur, "taps", "tap_reward", "REAL DEFAULT 0")
                ensure_column(cur, "taps", "earn_cap_remaining", "REAL DEFAULT 0")
                ensure_column(cur, "taps", "taps_total", "INTEGER DEFAULT 0")
                ensure_column(cur, "taps", "balance_usdt", "REAL DEFAULT 0.0")

        # 4) invoices поля
        if table_exists(cur, "invoices"):
            ensure_column(cur, "invoices", "tg_id", "INTEGER")
            ensure_column(cur, "invoices", "package_id", "INTEGER")
            ensure_column(cur, "invoices", "base_price", "REAL")
            ensure_column(cur, "invoices", "unique_amount", "REAL")
            ensure_column(cur, "invoices", "status", "TEXT")
            ensure_column(cur, "invoices", "txid", "TEXT")
            ensure_column(cur, "invoices", "created_at", "INTEGER")
            ensure_column(cur, "invoices", "paid_at", "INTEGER")

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

    # ensure rows exist
    cur.execute("INSERT OR IGNORE INTO users (tg_id, bonus_given) VALUES (?, 0)", (tg_id,))
    cur.execute("""
        INSERT OR IGNORE INTO taps (tg_id, taps_available, tap_reward, earn_cap_remaining, taps_total, balance_usdt)
        VALUES (?, ?, ?, ?, 0, 0.0)
    """, (tg_id, WELCOME_TAPS, WELCOME_REWARD, WELCOME_CAP))

    # bonus one-time
    cur.execute("SELECT bonus_given FROM users WHERE tg_id=? LIMIT 1", (tg_id,))
    u = cur.fetchone()
    bonus_given = int(u["bonus_given"]) if u and u["bonus_given"] is not None else 0

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
                return {"status": "ok", "userId": int(tg_id), "taps_available": 0, "tap_reward": 0.0, "earn_cap_remaining": 0.0, "bonus_given": 0, "balance": {"balance_usdt": 0.0}, "taps": {}}

            return {
                "status": "ok",
                "userId": int(row["userId"]),
                "taps_available": int(row["taps_available"]),
                "tap_reward": float(row["tap_reward"]),
                "earn_cap_remaining": float(row["earn_cap_remaining"]),
                "taps_total": int(row["taps_total"]),
                "balance": {"balance_usdt": float(row["balance_usdt"])},
                "bonus_given": int(row["bonus_given"]),
            }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": str(e),
                "trace": traceback.format_exc()[:4000]
            }
        )

@app.get("/api/debug/diag/{tg_id}")
def debug_diag(tg_id: int):
    with closing(db()) as conn:
        cur = conn.cursor()
        out = {"ok": True, "tg_id": int(tg_id)}
        for t in ["users", "taps", "invoices", "processed_tx"]:
            if table_exists(cur, t):
                out[f"{t}_cols"] = table_cols(cur, t)
            else:
                out[f"{t}_cols"] = None
        # попробуем ensure_user_and_bonus и вернём что в taps
        try:
            ensure_user_and_bonus(conn, int(tg_id))
            cur.execute("SELECT * FROM users WHERE tg_id=? LIMIT 1", (int(tg_id),))
            out["user_row"] = dict(cur.fetchone() or {})
            cur.execute("SELECT * FROM taps WHERE tg_id=? LIMIT 1", (int(tg_id),))
            out["taps_row"] = dict(cur.fetchone() or {})
        except Exception as e:
            out["ensure_error"] = str(e)
            out["trace"] = traceback.format_exc()[:4000]
        return out

@app.get("/api/debug/taps/{tg_id}")
def debug_taps(tg_id: int):
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM taps WHERE tg_id=? LIMIT 1", (int(tg_id),))
        r = cur.fetchone()
        return {"ok": True, "row": dict(r) if r else None}

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

# Новый роут для тапов
@app.post("/api/tap")
def tap(data: dict):
    tg_id = data.get("tg_id")
    if not tg_id:
        return {"ok": False, "error": "No tg_id"}

    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM taps WHERE tg_id = ?", (tg_id,))
        row = cur.fetchone()

        if not row:
            return {"ok": False, "error": "User not found"}

        taps_available = row["taps_available"]
        if taps_available <= 0:
            return {
                "ok": True,
                "balance_usdt": row["balance_usdt"],
                "taps_available": 0,
                "tap_reward": row["tap_reward"],
                "taps_total": row["taps_total"],
                "earn_cap_remaining": row["earn_cap_remaining"]
            }

        new_balance = row["balance_usdt"] + row["tap_reward"]
        new_taps = taps_available - 1
        new_total = row["taps_total"] + 1
        new_cap = row["earn_cap_remaining"] - row["tap_reward"]

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
            "tap_reward": row["tap_reward"],
            "taps_total": new_total,
            "earn_cap_remaining": new_cap
        }

# Заглушка для рефералов (можно доработать позже)
@app.get("/api/referrals/{tg_id}")
def get_referrals(tg_id: int):
    return {
        "ok": True,
        "referrals": [],
        "invited_count": 0,
        "bonus_total": 0
    }