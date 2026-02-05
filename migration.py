import sqlite3
import os
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
print(f"Используем базу данных: {DB_PATH}")

def migrate_database():
    print("=== Начинаем миграцию базы данных на сервере ===")
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            # 1. Проверяем текущую структуру
            print("\n1. Проверяем текущие таблицы...")
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cur.fetchall()]
            print(f"   Найдены таблицы: {tables}")
            
            # 2. Если есть старая таблица users с tg_id, переименовываем
            if 'users' in tables:
                cur.execute("PRAGMA table_info(users)")
                columns = [(col[1], col[2]) for col in cur.fetchall()]
                print(f"   Столбцы users: {columns}")
                
                # Проверяем есть ли tg_id
                has_tg_id = any(col[0] == 'tg_id' for col in columns)
                has_telegram_id = any(col[0] == 'telegram_id' for col in columns)
                
                if has_tg_id and not has_telegram_id:
                    print("   ⚠️  Найден старый формат (tg_id), нужно мигрировать...")
                    
                    # Создаем новую таблицу с правильной схемой
                    print("   Создаем новую таблицу users_new...")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS users_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            telegram_id INTEGER UNIQUE NOT NULL,
                            username TEXT,
                            first_name TEXT,
                            last_name TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            welcome_given BOOLEAN DEFAULT 0
                        )
                    """)
                    
                    # Копируем данные
                    print("   Копируем данные из старой таблицы...")
                    cur.execute("""
                        INSERT OR IGNORE INTO users_new (id, telegram_id, welcome_given)
                        SELECT id, tg_id, bonus_given FROM users
                    """)
                    
                    # Переименовываем таблицы
                    print("   Переименовываем таблицы...")
                    cur.execute("ALTER TABLE users RENAME TO users_backup")
                    cur.execute("ALTER TABLE users_new RENAME TO users")
                    
                    print("   ✓ Таблица users мигрирована")
            
            # 3. Создаем недостающие таблицы
            print("\n2. Создаем недостающие таблицы...")
            
            # Таблица user_stats если её нет
            if 'user_stats' not in tables:
                print("   Создаем user_stats...")
                cur.execute("""
                    CREATE TABLE user_stats (
                        user_id INTEGER PRIMARY KEY,
                        balance REAL DEFAULT 0.0,
                        free_taps INTEGER DEFAULT 10000,
                        total_taps INTEGER DEFAULT 0,
                        package_taps_remaining INTEGER DEFAULT 0,
                        tap_reward REAL DEFAULT 0.0001,
                        package_type TEXT,
                        package_expires TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                    )
                """)
                print("   ✓ Таблица user_stats создана")
            
            # Таблица payments если её нет
            if 'payments' not in tables:
                print("   Создаем payments...")
                cur.execute("""
                    CREATE TABLE payments (
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
                    )
                """)
                print("   ✓ Таблица payments создана")
            
            # 4. Создаем индексы
            print("\n3. Создаем индексы...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")
            print("   ✓ Индексы созданы")
            
            conn.commit()
            
            # 5. Показываем результат
            print("\n=== Результат миграции ===")
            cur.execute("SELECT COUNT(*) as cnt FROM users")
            print(f"Всего пользователей: {cur.fetchone()['cnt']}")
            
            cur.execute("SELECT COUNT(*) as cnt FROM user_stats")
            print(f"Записей статистики: {cur.fetchone()['cnt']}")
            
            print("\n✅ Миграция завершена успешно!")
            
    except Exception as e:
        print(f"❌ Ошибка при миграции: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    migrate_database()
