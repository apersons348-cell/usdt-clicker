from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os, sqlite3, time, traceback
from contextlib import closing
from dotenv import load_dotenv
from typing import Dict, Any
from datetime import datetime

load_dotenv()

app = FastAPI(title="TG Clicker", version="4.0")

BUILD = os.getenv("BUILD") or os.getenv("RENDER_GIT_COMMIT") or "clean"

# ---------------- CORS ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- PATHS ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(os.getcwd(), "webapp")
INDEX_PATH = os.path.join(WEBAPP_DIR, "index.html")
if os.path.exists(WEBAPP_DIR):
    app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")

# ---------------- ENV ----------------
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "data.db"))

# ---------------- ПАКЕТЫ ----------------
PACKAGES = {
    "basic": {"name": "Новичок", "price": 10.0, "taps": 10000, "reward": 0.0002},
    "pro": {"name": "Профи", "price": 50.0, "taps": 50000, "reward": 0.00025},
    "max": {"name": "VIP", "price": 100.0, "taps": 100000, "reward": 0.0003},
}

# ================== БАЗА ДАННЫХ ==================
def get_db():
    """Получение соединения с БД"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    """Инициализация базы данных при запуске"""
    with closing(get_db()) as conn:
        cur = conn.cursor()
        
        # Создаем таблицу users если её нет
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                lang TEXT DEFAULT 'ru',
                balance REAL DEFAULT 0.0,
                free_taps_left INTEGER DEFAULT 10000,
                paid_taps_left INTEGER DEFAULT 0,
                tap_value REAL DEFAULT 0.0001,
                withdraw_address TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                total_taps INTEGER DEFAULT 0,
                last_active TEXT,
                package_expires TEXT,
                package_type TEXT,
                daily_taps INTEGER DEFAULT 0,
                welcome_given BOOLEAN DEFAULT 0
            )
        """)
        
        # Создаем таблицу payments если её нет
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                package_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        
        # Индексы для быстрого поиска
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_telegram ON payments(telegram_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
        
        conn.commit()
        print("✅ База данных инициализирована")

# Инициализируем базу при старте
init_db()

# ================== МОДЕЛИ ==================
class TapRequest(BaseModel):
    telegram_id: int

class BuyPackageRequest(BaseModel):
    telegram_id: int
    package_type: str = "basic"

class SaveProgressRequest(BaseModel):
    telegram_id: int
    balance: float = 0.0
    free_taps_left: int = 0
    paid_taps_left: int = 0
    total_taps: int = 0

# ================== ХЕЛПЕРЫ ==================
def get_or_create_user(conn, telegram_id: int):
    """Получить или создать пользователя"""
    cur = conn.cursor()
    
    # Проверяем существующего пользователя
    cur.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    
    if row:
        return telegram_id
    
    # Создаем нового пользователя с приветственным бонусом
    cur.execute("""
        INSERT INTO users (
            telegram_id, balance, free_taps_left, tap_value, welcome_given, last_active
        ) VALUES (?, 1.0, 10000, 0.0001, 1, datetime('now'))
    """, (telegram_id,))
    
    conn.commit()
    return telegram_id

def get_user_stats(conn, telegram_id: int) -> Dict:
    """Получить статистику пользователя"""
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            balance,
            free_taps_left as free_taps,
            total_taps,
            paid_taps_left as package_taps,
            tap_value as tap_reward,
            package_type,
            CASE 
                WHEN package_expires IS NOT NULL AND datetime(package_expires) > datetime('now') 
                THEN 1 
                ELSE 0 
            END as has_package,
            welcome_given
        FROM users 
        WHERE telegram_id = ?
    """, (telegram_id,))
    
    row = cur.fetchone()
    if not row:
        # Возвращаем дефолтные значения если пользователь не найден
        return {
            "balance": 1.0,
            "free_taps": 10000,
            "total_taps": 0,
            "package_taps": 0,
            "tap_reward": 0.0001,
            "package_type": None,
            "has_package": False,
            "welcome_given": True
        }
    
    return dict(row)

# ================== ROUTES ==================
@app.get("/")
async def root():
    """Корневой endpoint"""
    return {"app": "TG Clicker", "status": "running", "version": "4.0"}

@app.get("/api/health")
async def health_check():
    """Проверка здоровья сервиса"""
    try:
        with closing(get_db()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            db_ok = cur.fetchone() is not None
        
        return {
            "ok": True,
            "db": db_ok,
            "timestamp": int(time.time())
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

@app.get("/api/version")
async def version():
    """Версия приложения"""
    return {
        "ok": True,
        "build": BUILD,
        "ts": int(time.time())
    }

@app.get("/api/user/{telegram_id}")
async def get_user(telegram_id: int):
    """Получить данные пользователя"""
    try:
        with closing(get_db()) as conn:
            # Получаем или создаем пользователя
            get_or_create_user(conn, telegram_id)
            
            # Получаем статистику
            stats = get_user_stats(conn, telegram_id)
            
            return {
                "ok": True,
                "user_id": 1,  # Для совместимости
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
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            
            # Получаем текущие данные пользователя
            cur.execute("""
                SELECT balance, free_taps_left, paid_taps_left, tap_value, total_taps
                FROM users 
                WHERE telegram_id = ?
            """, (request.telegram_id,))
            
            user_row = cur.fetchone()
            if not user_row:
                conn.rollback()
                return {"ok": False, "error": "User not found"}
            
            balance = float(user_row['balance'] or 0)
            free_taps = int(user_row['free_taps_left'] or 10000)
            paid_taps = int(user_row['paid_taps_left'] or 0)
            tap_reward = float(user_row['tap_value'] or 0.0001)
            total_taps = int(user_row['total_taps'] or 0)
            
            earned = 0.0
            new_free_taps = free_taps
            new_paid_taps = paid_taps
            
            # Определяем тип клика и начисляем
            if free_taps > 0:
                # Бесплатные клики
                earned = 0.0001
                new_free_taps = free_taps - 1
            elif paid_taps > 0:
                # Клики из пакета
                earned = tap_reward
                new_paid_taps = paid_taps - 1
            else:
                # Клики после окончания пакета
                earned = 0.0001
            
            new_balance = balance + earned
            new_total_taps = total_taps + 1
            
            # Обновляем данные пользователя
            cur.execute("""
                UPDATE users 
                SET balance = ?,
                    free_taps_left = ?,
                    paid_taps_left = ?,
                    total_taps = ?,
                    last_active = datetime('now')
                WHERE telegram_id = ?
            """, (new_balance, new_free_taps, new_paid_taps, new_total_taps, request.telegram_id))
            
            conn.commit()
            
            return {
                "ok": True,
                "earned": earned,
                "balance": new_balance,
                "free_taps": new_free_taps,
                "package_taps": new_paid_taps,
                "total_taps": new_total_taps,
                "tap_reward": tap_reward
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

@app.post("/api/buy-package")
async def buy_package(request: BuyPackageRequest):
    """Покупка пакета тапов"""
    try:
        with closing(get_db()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            
            # Проверяем существование пользователя
            get_or_create_user(conn, request.telegram_id)
            
            # Проверяем тип пакета
            if request.package_type not in PACKAGES:
                conn.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "Invalid package type"}
                )
            
            package = PACKAGES[request.package_type]
            
            # Создаем запись о платеже
            cur.execute("""
                INSERT INTO payments (telegram_id, amount, package_type, status, completed_at)
                VALUES (?, ?, ?, 'completed', datetime('now'))
            """, (request.telegram_id, package["price"], request.package_type))
            
            # Обновляем данные пользователя
            cur.execute("""
                UPDATE users 
                SET paid_taps_left = paid_taps_left + ?,
                    tap_value = ?,
                    package_type = ?,
                    package_expires = datetime('now', '+30 days'),
                    last_active = datetime('now')
                WHERE telegram_id = ?
            """, (package["taps"], package["reward"], request.package_type, request.telegram_id))
            
            conn.commit()
            
            return {
                "ok": True,
                "message": f"Пакет '{package['name']}' успешно активирован!",
                "package": request.package_type,
                "taps_added": package["taps"],
                "new_reward": package["reward"],
                "expires_in": "30 дней"
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

@app.post("/api/save-progress")
async def save_progress(request: SaveProgressRequest):
    """Сохранение прогресса пользователя"""
    try:
        with closing(get_db()) as conn:
            cur = conn.cursor()
            
            # Обновляем данные пользователя
            cur.execute("""
                UPDATE users 
                SET balance = ?,
                    free_taps_left = ?,
                    paid_taps_left = ?,
                    total_taps = ?,
                    last_active = datetime('now')
                WHERE telegram_id = ?
            """, (
                request.balance,
                request.free_taps_left,
                request.paid_taps_left,
                request.total_taps,
                request.telegram_id
            ))
            
            if cur.rowcount == 0:
                # Если пользователь не найден, создаем его
                cur.execute("""
                    INSERT INTO users (
                        telegram_id, balance, free_taps_left, paid_taps_left, 
                        total_taps, last_active, welcome_given
                    ) VALUES (?, ?, ?, ?, ?, datetime('now'), 1)
                """, (
                    request.telegram_id,
                    request.balance,
                    request.free_taps_left,
                    request.paid_taps_left,
                    request.total_taps
                ))
            
            conn.commit()
            
            return {
                "ok": True,
                "message": "Прогресс сохранен",
                "timestamp": datetime.now().isoformat()
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

@app.get("/api/payments/create")
async def create_payment(telegram_id: int, amount: float, package_type: str = "basic"):
    """Создание платежа (для совместимости)"""
    try:
        with closing(get_db()) as conn:
            cur = conn.cursor()
            
            cur.execute("""
                INSERT INTO payments (telegram_id, amount, package_type, status)
                VALUES (?, ?, ?, 'pending')
            """, (telegram_id, amount, package_type))
            
            payment_id = cur.lastrowid
            
            conn.commit()
            
            return {
                "ok": True,
                "payment_id": payment_id,
                "status": "pending"
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

@app.get("/api/payments/check")
async def check_payment(payment_id: int):
    """Проверка статуса платежа (для совместимости)"""
    try:
        with closing(get_db()) as conn:
            cur = conn.cursor()
            
            cur.execute("""
                SELECT telegram_id, amount, package_type, status, created_at
                FROM payments 
                WHERE id = ?
            """, (payment_id,))
            
            payment = cur.fetchone()
            if not payment:
                return {"ok": False, "error": "Payment not found"}
            
            return {
                "ok": True,
                "payment": dict(payment)
            }
            
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )

# ================== STATIC FILES ==================
@app.get("/{full_path:path}")
async def serve_static(full_path: str):
    """Обслуживание статических файлов"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    
    if os.path.exists(INDEX_PATH):
        return FileResponse(INDEX_PATH)
    
    return {"error": "Frontend not found"}

if __name__ == "__main__":
    import uvicorn
    print("🚀 Запуск TG Clicker версии 4.0")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
