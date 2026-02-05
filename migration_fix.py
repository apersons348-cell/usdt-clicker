import sqlite3
import os
import sys

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
print(f"Используем базу данных: {DB_PATH}")

def migrate_database():
    print("=== Исправленная миграция базы данных ===")
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            # 1. Переименовываем user_id в telegram_id
            print("1. Переименовываем user_id в telegram_id...")
            
            # Создаем новую таблицу с правильными колонками
            cur.execute("""
                CREATE TABLE users_new (
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    lang TEXT DEFAULT 'ru',
                    balance REAL DEFAULT 0.0,
                    free_taps_left INTEGER DEFAULT 10000,
                    paid_taps_left INTEGER DEFAULT 0,
                    tap_value REAL DEFAULT 0.0001,
                    withdraw_address TEXT,
                    created_at INTEGER DEFAULT (strftime('%s','now'))
                )
            """)
            
            # Копируем данные
            cur.execute("""
                INSERT INTO users_new 
                SELECT user_id, username, first_name, lang, balance, 
                       free_taps_left, paid_taps_left, tap_value, 
                       withdraw_address, created_at
                FROM users
            """)
            
            # Удаляем старую таблицу и переименовываем
            cur.execute("DROP TABLE users")
            cur.execute("ALTER TABLE users_new RENAME TO users")
            
            print("   ✓ user_id переименован в telegram_id")
            
            conn.commit()
            print("\n✅ Миграция завершена успешно!")
            
    except Exception as e:
        print(f"❌ Ошибка при миграции: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    migrate_database()
