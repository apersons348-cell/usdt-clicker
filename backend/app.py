# ================== AUTOMATIC MIGRATION ==================
import sys
import os

# Добавляем путь к корню проекта для импорта миграции
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

# Запускаем миграцию перед стартом сервера
try:
    print("=== Запуск миграции базы данных ===")
    from migration import migrate_database
    migrate_database()
except ImportError:
    print("⚠️  Модуль миграции не найден, продолжаем без миграции...")
except Exception as e:
    print(f"⚠️  Миграция не удалась: {e}")
    print("Продолжаем работу без миграции...")

# ================== IMPORTS ==================
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sqlite3
import time
import random
import requests
import traceback
from contextlib import closing
from dotenv import load_dotenv
from typing import Optional, Dict
from datetime import datetime, timedelta

load_dotenv()

app = FastAPI(title="TG Clicker API", version="3.0")

BUILD = os.getenv("BUILD") or os.getenv("RENDER_GIT_COMMIT") or "local"

# ---------------- CORS ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- PATHS ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")
INDEX_PATH = os.path.join(WEBAPP_DIR, "index.html")
if os.path.exists(WEBAPP_DIR):
    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

# ---------------- ENV ----------------
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "data.db"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()
TRON_RECEIVE_ADDRESS = os.getenv("TRON_RECEIVE_ADDRESS", "").strip()
TRC20_USDT_CONTRACT = os.getenv("TRC20_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").strip()
TRONGRID_BASE = "https://api.trongrid.io"

PAYMENT_TIME_SLOP_SEC = int(os.getenv("PAYMENT_TIME_SLOP_SEC", "300"))
MAX_OVERPAY = float(os.getenv("MAX_OVERPAY", "1000"))

# ---------------- PACKAGES ----------------
PACKAGES = {
    1: {"name": "Новичок", "price": 10.0, "taps": 100000, "reward": 0.0002, "cap": 20.0},
    2: {"name": "Профи", "price": 50.0, "taps": 500000, "reward": 0.00025, "cap": 125.0},
    3: {"name": "VIP", "price": 100.0, "taps": 1000000, "reward": 0.0003, "cap": 300.0},
}

# ================== DB ==================
def get_db():
    """Получение соединения с БД"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    """Инициализация базы данных"""
    with closing(get_db()) as conn:
        cur = conn.cursor()
        
        # Таблица пользователей
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            welcome_given BOOLEAN DEFAULT 0
        );
        """)
        
        # Таблица баланса и кликов
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0.0,
            free_taps INTEGER DEFAULT 10000,
            total_taps INTEGER DEFAULT 0,
            package_taps_remaining INTEGER DEFAULT 0,
            tap_reward REAL DEFAULT 0.0001,
            package_type TEXT,
            package_expires TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
        
        # Таблица платежей
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            unique_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            tx_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
        
        # Таблица обработанных транзакций
        cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_transactions (
            tx_hash TEXT PRIMARY KEY,
            payment_id INTEGER,
            amount REAL NOT NULL,
            timestamp INTEGER NOT NULL
        );
        """)
        
        # Индексы
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at);")
        
        conn.commit()

# Инициализируем БД
init_db()

# ================== MODELS ==================
class TapRequest(BaseModel):
    telegram_id: int

class CreateInvoiceRequest(BaseModel):
    telegram_id: int
    package_id: int

class CheckInvoiceRequest(BaseModel):
    telegram_id: int
    invoice_id: int

# ================== USER MANAGEMENT ==================
def get_or_create_user(conn, telegram_id: int) -> int:
    """Получить или создать пользователя, возвращает user_id"""
    cur = conn.cursor()
    
    # Проверяем существующего пользователя
    cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    
    if row:
        return row['id']
    
    # Создаем нового пользователя
    cur.execute("""
        INSERT INTO users (telegram_id, welcome_given)
        VALUES (?, 0)
    """, (telegram_id,))
    
    user_id = cur.lastrowid
    
    # Создаем запись в статистике с приветственным бонусом
    cur.execute("""
        INSERT INTO user_stats (user_id, free_taps, tap_reward, balance)
        VALUES (?, 10000, 0.0001, 1.0)
    """, (user_id,))
    
    # Отмечаем, что бонус выдан
    cur.execute("UPDATE users SET welcome_given = 1 WHERE id = ?", (user_id,))
    
    conn.commit()
    return user_id

def get_user_stats(conn, user_id: int) -> Dict:
    """Получить статистику пользователя"""
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            us.balance,
            us.free_taps,
            us.total_taps,
            us.package_taps_remaining,
            us.tap_reward,
            us.package_type,
            us.package_expires,
            u.welcome_given
        FROM user_stats us
        JOIN users u ON u.id = us.user_id
        WHERE us.user_id = ?
    """, (user_id,))
    
    row = cur.fetchone()
    if not row:
        return {
            "balance": 0.0,
            "free_taps": 10000,
            "total_taps": 0,
            "package_taps": 0,
            "tap_reward": 0.0001,
            "package_type": None,
            "has_package": False,
            "welcome_given": False
        }
    
    has_package = bool(row['package_taps_remaining'] > 0 and 
                      (not row['package_expires'] or 
                       datetime.fromisoformat(row['package_expires']) > datetime.now()))
    
    return {
        "balance": float(row['balance'] or 0),
        "free_taps": int(row['free_taps'] or 10000),
        "total_taps": int(row['total_taps'] or 0),
        "package_taps": int(row['package_taps_remaining'] or 0),
        "tap_reward": float(row['tap_reward'] or 0.0001),
        "package_type": row['package_type'],
        "has_package": has_package,
        "welcome_given": bool(row['welcome_given'])
    }

# ================== PAYMENT HELPERS ==================
def tron_headers():
    headers = {"accept": "application/json"}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return headers

def check_tron_transaction(amount: float, created_at: int) -> Optional[Dict]:
    """Проверить транзакцию в сети TRON"""
    if not TRON_RECEIVE_ADDRESS:
        return None
    
    try:
        url = f"{TRONGRID_BASE}/v1/accounts/{TRON_RECEIVE_ADDRESS}/transactions/trc20"
        params = {
            "only_confirmed": "true",
            "limit": 20,
            "contract_address": TRC20_USDT_CONTRACT,
            "order_by": "block_timestamp,desc"
        }
        
        response = requests.get(url, headers=tron_headers(), params=params, timeout=15)
        response.raise_for_status()
        
        transactions = response.json().get("data", [])
        min_time = created_at - PAYMENT_TIME_SLOP_SEC
        
        for tx in transactions:
            try:
                tx_hash = tx.get("transaction_id")
                tx_amount = int(tx.get("value", 0)) / 1_000_000
                tx_time = tx.get("block_timestamp", 0) // 1000
                
                if tx_time < min_time:
                    continue
                
                if tx_amount < amount or tx_amount > amount + MAX_OVERPAY:
                    continue
                
                return {
                    "tx_hash": tx_hash,
                    "amount": tx_amount,
                    "timestamp": tx_time
                }
                
            except (KeyError, ValueError, TypeError):
                continue
                
    except Exception as e:
        print(f"Error checking TRON transaction: {e}")
    
    return None

# ================== ROUTES ==================
@app.get("/")
async def home():
    if os.path.exists(INDEX_PATH):
        return FileResponse(INDEX_PATH)
    return {"app": "TG Clicker", "status": "running", "version": "3.0"}

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "db": os.path.exists(DB_PATH),
        "tron_configured": bool(TRON_RECEIVE_ADDRESS),
        "timestamp": int(time.time())
    }

@app.get("/api/version")
async def version():
    return {"ok": True, "build": BUILD, "ts": int(time.time())}

@app.get("/api/packages")
async def get_packages():
    return {
        "ok": True,
        "packages": PACKAGES,
        "address": TRON_RECEIVE_ADDRESS,
        "network": "TRON (TRC20 USDT)",
        "currency": "USDT"
    }

@app.get("/api/user/{telegram_id}")
async def get_user(telegram_id: int):
    try:
        with closing(get_db()) as conn:
            # Получаем или создаем пользователя
            user_id = get_or_create_user(conn, telegram_id)
            
            # Получаем статистику
            stats = get_user_stats(conn, user_id)
            
            return {
                "ok": True,
                "user_id": user_id,
                "telegram_id": telegram_id,
                "stats": stats
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:2000]}
        )

@app.post("/api/tap")
async def process_tap(request: TapRequest):
    """Обработка клика"""
    try:
        with closing(get_db()) as conn:
            # Начинаем транзакцию
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            
            # Получаем user_id
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (request.telegram_id,))
            user_row = cur.fetchone()
            
            if not user_row:
                conn.rollback()
                return {"ok": False, "error": "User not found"}
            
            user_id = user_row['id']
            
            # Получаем текущую статистику
            cur.execute("""
                SELECT balance, free_taps, package_taps_remaining, tap_reward, total_taps
                FROM user_stats 
                WHERE user_id = ?
            """, (user_id,))
            
            stats_row = cur.fetchone()
            if not stats_row:
                conn.rollback()
                return {"ok": False, "error": "Stats not found"}
            
            balance = float(stats_row['balance'] or 0)
            free_taps = int(stats_row['free_taps'] or 10000)
            package_taps = int(stats_row['package_taps_remaining'] or 0)
            tap_reward = float(stats_row['tap_reward'] or 0.0001)
            total_taps = int(stats_row['total_taps'] or 0)
            
            earned = 0.0
            new_free_taps = free_taps
            new_package_taps = package_taps
            
            # Определяем тип клика и начисляем
            if free_taps > 0:
                # Бесплатные клики
                earned = 0.0001
                new_free_taps = free_taps - 1
            elif package_taps > 0:
                # Клики из пакета
                earned = tap_reward
                new_package_taps = package_taps - 1
            else:
                # Клики после окончания пакета
                earned = 0.0001
            
            new_balance = balance + earned
            new_total_taps = total_taps + 1
            
            # Обновляем статистику
            cur.execute("""
                UPDATE user_stats 
                SET balance = ?,
                    free_taps = ?,
                    package_taps_remaining = ?,
                    total_taps = ?
                WHERE user_id = ?
            """, (new_balance, new_free_taps, new_package_taps, new_total_taps, user_id))
            
            conn.commit()
            
            return {
                "ok": True,
                "earned": earned,
                "balance": new_balance,
                "free_taps": new_free_taps,
                "package_taps": new_package_taps,
                "total_taps": new_total_taps,
                "tap_reward": tap_reward if package_taps > 0 else 0.0001
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:2000]}
        )

@app.post("/api/payments/create")
async def create_payment(request: CreateInvoiceRequest):
    """Создание счета на оплату"""
    try:
        # Проверяем пакет
        if request.package_id not in PACKAGES:
            return {"ok": False, "error": "Invalid package"}
        
        package = PACKAGES[request.package_id]
        
        with closing(get_db()) as conn:
            # Проверяем/создаем пользователя
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (request.telegram_id,))
            user_row = cur.fetchone()
            
            if not user_row:
                user_id = get_or_create_user(conn, request.telegram_id)
            else:
                user_id = user_row['id']
            
            # Создаем уникальную сумму
            unique_amount = round(package['price'] + random.randint(1, 999) / 10000, 6)
            
            # Создаем запись о платеже
            cur.execute("""
                INSERT INTO payments (user_id, package_id, amount, unique_amount, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (user_id, request.package_id, package['price'], unique_amount))
            
            payment_id = cur.lastrowid
            conn.commit()
            
            return {
                "ok": True,
                "payment_id": payment_id,
                "package": package,
                "amount": package['price'],
                "unique_amount": unique_amount,
                "address": TRON_RECEIVE_ADDRESS,
                "instructions": f"Send exactly {unique_amount:.6f} USDT (TRC20)"
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:2000]}
        )

@app.post("/api/payments/check")
async def check_payment(request: CheckInvoiceRequest):
    """Проверка статуса оплаты"""
    try:
        with closing(get_db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            
            # Находим платеж
            cur.execute("""
                SELECT p.*, u.telegram_id 
                FROM payments p
                JOIN users u ON u.id = p.user_id
                WHERE p.id = ? AND u.telegram_id = ?
            """, (request.invoice_id, request.telegram_id))
            
            payment = cur.fetchone()
            
            if not payment:
                conn.rollback()
                return {"ok": False, "error": "Payment not found"}
            
            # Если уже оплачен
            if payment['status'] == 'paid':
                conn.commit()
                return {
                    "ok": True,
                    "paid": True,
                    "tx_hash": payment['tx_hash'],
                    "package": PACKAGES.get(payment['package_id'])
                }
            
            # Проверяем время создания платежа
            if payment['created_at']:
                try:
                    # Пытаемся преобразовать время создания
                    created_dt = datetime.fromisoformat(payment['created_at'].replace('Z', '+00:00'))
                    created_at = int(created_dt.timestamp())
                except:
                    created_at = int(time.time()) - 60
            else:
                created_at = int(time.time())
            
            # Проверяем транзакцию
            tx_info = check_tron_transaction(payment['unique_amount'], created_at)
            
            if tx_info:
                # Помечаем как оплаченный
                cur.execute("""
                    UPDATE payments 
                    SET status = 'paid', 
                        tx_hash = ?,
                        paid_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (tx_info['tx_hash'], payment['id']))
                
                # Регистрируем транзакцию
                cur.execute("""
                    INSERT OR IGNORE INTO processed_transactions (tx_hash, payment_id, amount, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (tx_info['tx_hash'], payment['id'], tx_info['amount'], tx_info['timestamp']))
                
                # Начисляем пакет
                package = PACKAGES[payment['package_id']]
                user_id = payment['user_id']
                
                # Обновляем статистику
                expires_at = datetime.now() + timedelta(days=30)
                cur.execute("""
                    UPDATE user_stats 
                    SET package_taps_remaining = package_taps_remaining + ?,
                        tap_reward = ?,
                        package_type = ?,
                        package_expires = ?
                    WHERE user_id = ?
                """, (package['taps'], package['reward'], 
                      package['name'], expires_at.isoformat(), user_id))
                
                conn.commit()
                
                return {
                    "ok": True,
                    "paid": True,
                    "tx_hash": tx_info['tx_hash'],
                    "amount": tx_info['amount'],
                    "package": package,
                    "message": "Package activated!"
                }
            
            conn.commit()
            return {
                "ok": True,
                "paid": False,
                "status": "waiting",
                "message": "Payment not received yet"
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "trace": traceback.format_exc()[:2000]}
        )

@app.get("/api/payments/history/{telegram_id}")
async def payment_history(telegram_id: int):
    """История платежей пользователя"""
    try:
        with closing(get_db()) as conn:
            cur = conn.cursor()
            
            # Находим user_id
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            user_row = cur.fetchone()
            
            if not user_row:
                return {"ok": True, "payments": []}
            
            user_id = user_row['id']
            
            # Получаем историю платежей
            cur.execute("""
                SELECT p.* 
                FROM payments p
                WHERE p.user_id = ?
                ORDER BY p.created_at DESC
                LIMIT 20
            """, (user_id,))
            
            payments = cur.fetchall()
            
            return {
                "ok": True,
                "payments": [
                    {
                        "id": p['id'],
                        "package_id": p['package_id'],
                        "amount": p['amount'],
                        "status": p['status'],
                        "created_at": p['created_at'],
                        "paid_at": p['paid_at']
                    }
                    for p in payments
                ]
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")