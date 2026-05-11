# database.py
import sqlite3
from datetime import datetime, date, timedelta
import os

from pregnancy_math import (
    from_conception,
    from_due_date,
    from_lmp,
    parse_dd_mm_yyyy,
)

# Индексы строки SELECT * FROM users (после всех миграций)
U_ID = 0
U_WEEK = 1
U_DUE = 2
U_LMP = 3
U_REG = 4
U_USERNAME = 5
U_HEIGHT = 6
U_WEIGHT = 7
U_NOTIF = 8
U_LAST_NOTIF_W = 9
U_PDAY = 10
U_SOURCE = 11
U_DATE_IN = 12


def _normalize_user_row(row):
    if row is None:
        return None
    r = list(row)
    while len(r) < 13:
        r.append(None)
    return tuple(r)


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
            last_notification_week INTEGER DEFAULT 0,
            pregnancy_day INTEGER DEFAULT 0,
            source TEXT,
            date_input TEXT
        )
    ''')
    for query in (
        "ALTER TABLE users ADD COLUMN username TEXT",
        "ALTER TABLE users ADD COLUMN height_cm INTEGER",
        "ALTER TABLE users ADD COLUMN weight_kg REAL",
        "ALTER TABLE users ADD COLUMN pregnancy_day INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN source TEXT",
        "ALTER TABLE users ADD COLUMN date_input TEXT",
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
            last_notification_week INTEGER DEFAULT 0,
            pregnancy_day INTEGER DEFAULT 0,
            source TEXT,
            date_input TEXT
        )
    ''')
    for query in (
        "ALTER TABLE users ADD COLUMN username TEXT",
        "ALTER TABLE users ADD COLUMN height_cm INTEGER",
        "ALTER TABLE users ADD COLUMN weight_kg REAL",
        "ALTER TABLE users ADD COLUMN pregnancy_day INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN source TEXT",
        "ALTER TABLE users ADD COLUMN date_input TEXT",
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
    
    try:
        cursor.execute("UPDATE users SET pregnancy_day = 0 WHERE pregnancy_day IS NULL")
    except sqlite3.OperationalError:
        pass

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
        
        return _normalize_user_row(user)
        
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


def user_has_complete_onboarding(row) -> bool:
    """Достаточно ли данных, чтобы не показывать онбординг снова."""
    row = _normalize_user_row(row)
    if not row:
        return False
    week = row[U_WEEK]
    src = row[U_SOURCE]
    date_in = row[U_DATE_IN]
    pday = row[U_PDAY]
    if src in ("lmp", "conception", "due_date"):
        return bool(date_in)
    if src == "manual":
        return week is not None and pday is not None
    # Старые записи без source: достаточно недели
    if src is None and week is not None:
        return True
    return False


def refresh_computed_pregnancy(user_id: int) -> None:
    """Пересчитывает week и pregnancy_day по date_input для датовых источников."""
    row = get_user(user_id)
    row = _normalize_user_row(row)
    if not row or row[U_SOURCE] not in ("lmp", "conception", "due_date"):
        return
    raw = row[U_DATE_IN]
    if not raw:
        return
    d: date | None
    if "." in str(raw):
        d = parse_dd_mm_yyyy(str(raw))
    else:
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            return
    if d is None:
        return
    today = datetime.now().date()
    if row[U_SOURCE] == "lmp":
        res = from_lmp(d, today)
        due_iso = (d + timedelta(days=280)).strftime("%Y-%m-%d")
        lmp_iso = d.strftime("%Y-%m-%d")
    elif row[U_SOURCE] == "conception":
        res = from_conception(d, today)
        due_iso = (d + timedelta(days=266)).strftime("%Y-%m-%d")
        lmp_iso = None
    else:
        res = from_due_date(d, today)
        due_iso = d.strftime("%Y-%m-%d")
        lmp_iso = None
    if res.error:
        return
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        lp = lmp_iso if row[U_SOURCE] == "lmp" else None
        c.execute(
            """
            UPDATE users SET week = ?, pregnancy_day = ?, due_date = ?,
                last_period_date = ?,
                last_notification_week = ?
            WHERE user_id = ?
            """,
            (res.week, res.day, due_iso, lp, res.week, user_id),
        )
        conn.commit()
    finally:
        if conn:
            conn.close()


def apply_pregnancy_save(
    user_id: int,
    week: int,
    pregnancy_day: int,
    source: str,
    date_input: str | None = None,
    due_date: str | None = None,
    last_period_date: str | None = None,
) -> None:
    """Сохраняет срок после онбординга (UPDATE существующей строки пользователя)."""
    ensure_user_exists(user_id)
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            """
            UPDATE users SET
                week = ?,
                pregnancy_day = ?,
                source = ?,
                date_input = ?,
                due_date = ?,
                last_period_date = ?,
                last_notification_week = ?
            WHERE user_id = ?
            """,
            (
                week,
                pregnancy_day,
                source,
                date_input,
                due_date,
                last_period_date,
                week,
                user_id,
            ),
        )
        conn.commit()
    finally:
        if conn:
            conn.close()


def get_users_for_notification():
    """Получает всех пользователей, которым нужно отправить уведомление"""
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM users WHERE source IN ('lmp', 'conception', 'due_date')"
        )
        for (uid,) in cursor.fetchall():
            refresh_computed_pregnancy(uid)

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
    allowed_fields = {"username", "week", "height_cm", "weight_kg", "pregnancy_day", "source", "date_input"}
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