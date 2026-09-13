"""
Работа с базой данных (SQLite).

Хранит:
- пользователей (chat_id, время напоминания)
- пары линз (название, дата начала использования, интервал замены в днях)
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "lens_bot.db"

# Готовые интервалы замены линз в днях
INTERVAL_PRESETS = {
    "daily": 1,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 90,
}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            reminder_time TEXT NOT NULL DEFAULT '09:00'
        );

        CREATE TABLE IF NOT EXISTS lenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            interval_days INTEGER NOT NULL,
            last_notified_date TEXT,
            FOREIGN KEY (chat_id) REFERENCES users (chat_id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()


def ensure_user(chat_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,)
    )
    conn.commit()
    conn.close()


def set_reminder_time(chat_id: int, time_str: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE users SET reminder_time = ? WHERE chat_id = ?",
        (time_str, chat_id),
    )
    conn.commit()
    conn.close()


def get_user_reminder_time(chat_id: int) -> str:
    conn = get_connection()
    row = conn.execute(
        "SELECT reminder_time FROM users WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    conn.close()
    return row["reminder_time"] if row else "09:00"


def add_lens(chat_id: int, name: str, start_date: date, interval_days: int) -> None:
    ensure_user(chat_id)
    conn = get_connection()
    conn.execute(
        "INSERT INTO lenses (chat_id, name, start_date, interval_days) "
        "VALUES (?, ?, ?, ?)",
        (chat_id, name, start_date.isoformat(), interval_days),
    )
    conn.commit()
    conn.close()


def list_lenses(chat_id: int) -> list[sqlite3.Row]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM lenses WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    conn.close()
    return rows


def delete_lens(chat_id: int, lens_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM lenses WHERE id = ? AND chat_id = ?", (lens_id, chat_id)
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def next_change_date(start_date_str: str, interval_days: int) -> date:
    """Ближайшая дата замены линз, начиная от start_date, повторяющаяся каждые interval_days."""
    start = date.fromisoformat(start_date_str)
    today = date.today()
    if today <= start:
        return start
    days_passed = (today - start).days
    cycles_done = days_passed // interval_days
    candidate = start + timedelta(days=cycles_done * interval_days)
    if candidate < today:
        candidate += timedelta(days=interval_days)
    return candidate


def get_all_users_with_reminder_time(reminder_time: str) -> list[int]:
    """Все chat_id, у кого выставлено данное время напоминания (для планировщика)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT chat_id FROM users WHERE reminder_time = ?", (reminder_time,)
    ).fetchall()
    conn.close()
    return [row["chat_id"] for row in rows]


def mark_notified(lens_id: int, notified_date: date) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE lenses SET last_notified_date = ? WHERE id = ?",
        (notified_date.isoformat(), lens_id),
    )
    conn.commit()
    conn.close()


def get_all_distinct_reminder_times() -> list[str]:
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT reminder_time FROM users").fetchall()
    conn.close()
    return [row["reminder_time"] for row in rows]
