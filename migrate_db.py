#!/usr/bin/env python3
import sqlite3
import os

DB_PATH = 'data.db'

def migrate():
    print("=== Миграция базы данных ===")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    # 1. Проверяем текущую схему users
    cur.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cur.fetchall()]
    print(f"Текущие колонки users: {columns}")
    
    # 2. Если нет колонки id, нужно пересоздать таблицу
    if 'id' not in columns:
        print("❌ Обнаружена старая схема таблицы users")
        
        # Сохраняем данные из старой таблицы
        cur.execute("SELECT * FROM users")
        old_data = cur.fetchall()
        print(f"Найдено {len(old_data)} записей для миграции")
        
        # Переименовываем старую таблицу
        cur.execute("ALTER TABLE users RENAME TO users_old")
        print("✅ Таблица users переименована в users_old")
        
        # Создаем новую таблицу с правильной схемой
        cur.execute('''
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                welcome_given BOOLEAN DEFAULT 0
            )
        ''')
        print("✅ Создана новая таблица users")
        
        # Переносим данные
        for row in old_data:
            telegram_id = row['telegram_id']
            username = row['username']
            first_name = row['first_name']
            
            cur.execute('''
                INSERT INTO users (telegram_id, username, first_name, welcome_given)
                VALUES (?, ?, ?, 1)
            ''', (telegram_id, username, first_name))
        
        print(f"✅ Перенесено {len(old_data)} записей")
    
    # 3. Проверяем user_stats
    cur.execute("SELECT COUNT(*) FROM user_stats")
    stats_count = cur.fetchone()[0]
    
    if stats_count == 0:
        print("❌ Таблица user_stats пуста, создаем записи...")
        
        # Создаем записи для всех пользователей
        cur.execute("SELECT id, telegram_id FROM users")
        users = cur.fetchall()
        
        for user in users:
            user_id = user['id']
            telegram_id = user['telegram_id']
            
            # Получаем данные из users_old если есть
            cur.execute("SELECT balance, free_taps_left, paid_taps_left FROM users_old WHERE telegram_id = ?", (telegram_id,))
            old_user = cur.fetchone()
            
            if old_user:
                balance = old_user['balance']
                free_taps = old_user['free_taps_left']
                paid_taps = old_user['paid_taps_left']
            else:
                balance = 1.0
                free_taps = 10000
                paid_taps = 0
            
            cur.execute('''
                INSERT OR REPLACE INTO user_stats 
                (user_id, balance, free_taps, total_taps, package_taps_remaining, tap_reward)
                VALUES (?, ?, ?, 0, ?, 0.0001)
            ''', (user_id, balance, free_taps, paid_taps))
            
        print(f"✅ Создано {len(users)} записей в user_stats")
    
    conn.commit()
    conn.close()
    print("\n✅ Миграция завершена!")

if __name__ == "__main__":
    migrate()
