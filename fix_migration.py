#!/usr/bin/env python3
import sqlite3
import os

DB_PATH = 'data.db'

def migrate_database():
    print("=== Исправление структуры базы данных ===")
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            
            # 1. Проверяем текущие колонки в users
            cur.execute("PRAGMA table_info(users)")
            columns = [col[1] for col in cur.fetchall()]
            print(f"Существующие колонки: {columns}")
            
            # 2. Добавляем недостающие колонки для сохранения прогресса
            needed_columns = [
                ('total_taps', 'INTEGER DEFAULT 0'),
                ('last_active', 'TEXT'),
                ('package_expires', 'TEXT'),
                ('package_type', 'TEXT'),
                ('daily_taps', 'INTEGER DEFAULT 0')
            ]
            
            for col_name, col_type in needed_columns:
                if col_name not in columns:
                    try:
                        cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                        print(f"✓ Добавлена колонка: {col_name}")
                    except sqlite3.OperationalError as e:
                        print(f"⚠️  Не удалось добавить {col_name}: {e}")
            
            # 3. Обновляем существующие записи
            cur.execute("SELECT telegram_id FROM users")
            users = cur.fetchall()
            
            for (telegram_id,) in users:
                # Обновляем last_active если пусто
                cur.execute("""
                    UPDATE users 
                    SET last_active = datetime('now')
                    WHERE telegram_id = ? AND (last_active IS NULL OR last_active = '')
                """, (telegram_id,))
                
                # Устанавливаем package_type если есть paid_taps_left
                cur.execute("""
                    UPDATE users 
                    SET package_type = 'basic'
                    WHERE telegram_id = ? AND paid_taps_left > 0 AND (package_type IS NULL OR package_type = '')
                """, (telegram_id,))
            
            # 4. Создаем таблицу для сессий если нет
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    session_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    session_end TIMESTAMP,
                    taps_count INTEGER DEFAULT 0,
                    balance_change REAL DEFAULT 0
                )
            """)
            print("✓ Таблица user_sessions создана/проверена")
            
            # 5. Создаем индексы
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(telegram_id)")
            
            conn.commit()
            print("\n✅ Миграция завершена успешно!")
            
    except Exception as e:
        print(f"❌ Ошибка при миграции: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    migrate_database()
