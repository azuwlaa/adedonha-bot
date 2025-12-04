# db.py — sqlite helpers (synchronous connection used with asyncio Lock)
import sqlite3
import asyncio
import logging
from typing import Optional
from config import DB_FILE

logger = logging.getLogger(__name__)

_conn: Optional[sqlite3.Connection] = None
_lock: Optional[asyncio.Lock] = None

def init_db():
    global _conn, _lock
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY,
            games_played INTEGER DEFAULT 0,
            total_validated_words INTEGER DEFAULT 0,
            total_wordlists_sent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    _conn = conn
    _lock = asyncio.Lock()

async def ensure_user(uid: str):
    global _conn, _lock
    if _conn is None:
        raise RuntimeError("DB not initialized")
    async with _lock:
        c = _conn.cursor()
        c.execute("INSERT OR IGNORE INTO stats (user_id) VALUES (?)", (uid,))
        _conn.commit()

async def update_after_round(uid: str, validated_words: int, submitted_any: bool):
    global _conn, _lock
    if _conn is None:
        raise RuntimeError("DB not initialized")
    async with _lock:
        c = _conn.cursor()
        c.execute("INSERT OR IGNORE INTO stats (user_id) VALUES (?)", (uid,))
        if submitted_any:
            c.execute(
                "UPDATE stats SET total_wordlists_sent = total_wordlists_sent + 1, total_validated_words = total_validated_words + ? WHERE user_id=?",
                (validated_words, uid)
            )
        _conn.commit()

async def update_after_game(user_ids: list):
    global _conn, _lock
    if _conn is None:
        raise RuntimeError("DB not initialized")
    async with _lock:
        c = _conn.cursor()
        for uid in user_ids:
            c.execute("INSERT OR IGNORE INTO stats (user_id) VALUES (?)", (uid,))
            c.execute("UPDATE stats SET games_played = games_played + 1 WHERE user_id=?", (uid,))
        _conn.commit()

async def get_stats(uid: str):
    global _conn, _lock
    if _conn is None:
        raise RuntimeError("DB not initialized")
    async with _lock:
        c = _conn.cursor()
        c.execute("SELECT games_played, total_validated_words, total_wordlists_sent FROM stats WHERE user_id=?", (uid,))
        row = c.fetchone()
        if row:
            return {"games_played": row[0] or 0, "total_validated_words": row[1] or 0, "total_wordlists_sent": row[2] or 0}
        return {"games_played": 0, "total_validated_words": 0, "total_wordlists_sent": 0}

async def dump_all():
    global _conn, _lock
    if _conn is None:
        raise RuntimeError("DB not initialized")
    async with _lock:
        c = _conn.cursor()
        c.execute("SELECT user_id,games_played,total_validated_words,total_wordlists_sent FROM stats ORDER BY total_validated_words DESC")
        return c.fetchall()

async def reset_all():
    global _conn, _lock
    if _conn is None:
        raise RuntimeError("DB not initialized")
    async with _lock:
        c = _conn.cursor()
        c.execute("UPDATE stats SET games_played=0, total_validated_words=0, total_wordlists_sent=0")
        _conn.commit()
