# database.py
import sqlite3
from datetime import datetime
import os


def get_db_path():
    """Путь к SQLite. DATABASE_PATH задаёт свой файл (например с Railway Volume). Иначе — pregnancy_bot.db в корне приложения."""
    custom_path = os.getenv("DATABASE_PATH")
    if custom_path:
        return custom_path

    # В Vercel файловая система для записи доступна только в /tmp.
    if os.getenv("VERCEL") == "1":
        return "/tmp/pregnancy_bot.db"

    return "pregnancy_bot.db"

def get_connection():
    """Создает соединение с БД и гарантирует наличие таблицы"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    # ВСЕГДА создаем таблицу, если её нет
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            week INTEGER,
            due_date TEXT,
            last_period_date TEXT,
            registered_date TEXT,
            username TEXT,
            height_cm INTEGER,
            weight_kg REAL,
            notifications_enabled INTEGER DEFAULT 1,
            last_notification_week INTEGER DEFAULT 0
        )
    ''')
    for query in (
        "ALTER TABLE users ADD COLUMN username TEXT",
        "ALTER TABLE users ADD COLUMN height_cm INTEGER",
        "ALTER TABLE users ADD COLUMN weight_kg REAL",
    ):
        try:
            cursor.execute(query)
        except sqlite3.OperationalError:
            pass
    
    conn.commit()
    return conn

def init_db():
    """Создает таблицы в базе данных при первом запуске"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    # Таблица пользователей (уже есть)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            week INTEGER,
            due_date TEXT,
            last_period_date TEXT,
            registered_date TEXT,
            username TEXT,
            height_cm INTEGER,
            weight_kg REAL,
            notifications_enabled INTEGER DEFAULT 1,
            last_notification_week INTEGER DEFAULT 0
        )
    ''')
    for query in (
        "ALTER TABLE users ADD COLUMN username TEXT",
        "ALTER TABLE users ADD COLUMN height_cm INTEGER",
        "ALTER TABLE users ADD COLUMN weight_kg REAL",
    ):
        try:
            cursor.execute(query)
        except sqlite3.OperationalError:
            # Колонка уже существует в рабочей БД.
            pass
    
    # 📊 НОВАЯ ТАБЛИЦА ДЛЯ ПОДСЧЕТА ШЕВЕЛЕНИЙ
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS kick_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            count INTEGER DEFAULT 0,
            start_time TEXT,
            last_kick_time TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')

    # Логи всех входящих сообщений
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            chat_id INTEGER,
            message_text TEXT,
            created_at TEXT
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

# 📊 НОВЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С ПОДСЧЕТОМ ШЕВЕЛЕНИЙ

def start_kick_count(user_id):
    """Начинает новый подсчет шевелений"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Проверяем, есть ли уже подсчет за сегодня
    cursor.execute('''
        SELECT * FROM kick_counts 
        WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    
    existing = cursor.fetchone()
    
    if not existing:
        # Создаем новый подсчет
        cursor.execute('''
            INSERT INTO kick_counts (user_id, date, count, start_time, last_kick_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, today, 0, now, now))
        
        conn.commit()
        conn.close()
        return True
    else:
        conn.close()
        return False

def add_kick(user_id):
    """Добавляет +1 к шевелениям"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute('''
        UPDATE kick_counts 
        SET count = count + 1, last_kick_time = ?
        WHERE user_id = ? AND date = ?
    ''', (now, user_id, today))
    
    conn.commit()
    
    # Получаем обновленное значение
    cursor.execute('''
        SELECT count FROM kick_counts 
        WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    
    result = cursor.fetchone()
    conn.close()
    
    return result[0] if result else 0

def get_today_kicks(user_id):
    """Получает количество шевелений за сегодня"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute('''
        SELECT count FROM kick_counts 
        WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    
    result = cursor.fetchone()
    conn.close()
    
    return result[0] if result else 0

def get_kick_history(user_id, days=7):
    """Получает историю шевелений за последние N дней"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT date, count FROM kick_counts 
        WHERE user_id = ? 
        ORDER BY date DESC LIMIT ?
    ''', (user_id, days))
    
    results = cursor.fetchall()
    conn.close()
    
    return results

def add_user(user_id, week, due_date=None, last_period_date=None):
    """Добавляет пользователя"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        exists = cursor.fetchone()
        if exists:
            cursor.execute(
                '''
                UPDATE users
                SET week = ?,
                    due_date = ?,
                    last_period_date = ?,
                    registered_date = ?,
                    last_notification_week = ?
                WHERE user_id = ?
                ''',
                (week, due_date, last_period_date, now, week, user_id),
            )
        else:
            cursor.execute(
                '''
                INSERT INTO users
                (user_id, week, due_date, last_period_date, registered_date, last_notification_week)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (user_id, week, due_date, last_period_date, now, week),
            )
        
        conn.commit()
        print(f"✅ Пользователь {user_id} добавлен (неделя {week})")
        
    except Exception as e:
        print(f"❌ Ошибка добавления пользователя: {e}")
    finally:
        if conn:
            conn.close()

def get_user(user_id):
    """Получает пользователя"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        
        return user
        
    except Exception as e:
        print(f"❌ Ошибка получения пользователя: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_notifications(user_id, enabled):
    """Обновляет статус уведомлений"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE users SET notifications_enabled = ? WHERE user_id = ?', (enabled, user_id))
        
        conn.commit()
        print(f"✅ Уведомления для {user_id} изменены на {enabled}")
        
    except Exception as e:
        print(f"❌ Ошибка обновления уведомлений: {e}")
    finally:
        if conn:
            conn.close()

def get_users_for_notification():
    """Получает всех пользователей, которым нужно отправить уведомление"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT user_id, week, last_notification_week 
            FROM users 
            WHERE notifications_enabled = 1 AND week > last_notification_week
        ''')
        
        users = cursor.fetchall()
        return users
        
    except Exception as e:
        print(f"❌ Ошибка получения пользователей для уведомлений: {e}")
        return []
    finally:
        if conn:
            conn.close()

def update_last_notification(user_id, week):
    """Обновляет неделю последнего уведомления"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE users SET last_notification_week = ? WHERE user_id = ?', (week, user_id))
        
        conn.commit()
        
    except Exception as e:
        print(f"❌ Ошибка обновления последнего уведомления: {e}")
    finally:
        if conn:
            conn.close()

def count_users():
    """Возвращает количество пользователей в базе"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        result = cursor.fetchone()
        return result[0] if result else 0
    except Exception as e:
        print(f"❌ Ошибка подсчета пользователей: {e}")
        return 0
    finally:
        if conn:
            conn.close()

def log_message(user_id, username, full_name, chat_id, message_text):
    """Сохраняет входящее сообщение пользователя в БД"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            '''
            INSERT INTO message_logs
            (user_id, username, full_name, chat_id, message_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (user_id, username, full_name, chat_id, message_text, created_at)
        )
        conn.commit()
    except Exception as e:
        print(f"❌ Ошибка логирования сообщения: {e}")
    finally:
        if conn:
            conn.close()


def ensure_user_exists(user_id):
    """Создает минимальную запись пользователя, если ее еще нет."""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT OR IGNORE INTO users (user_id, registered_date)
            VALUES (?, ?)
            ''',
            (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        if conn:
            conn.close()


def update_profile_field(user_id, field_name, value):
    """Обновляет одно поле профиля для пользователя."""
    allowed_fields = {"username", "week", "height_cm", "weight_kg"}
    if field_name not in allowed_fields:
        raise ValueError("Недопустимое поле профиля")

    ensure_user_exists(user_id)
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE users SET {field_name} = ? WHERE user_id = ?",
            (value, user_id),
        )
        conn.commit()
    finally:
        if conn:
            conn.close()