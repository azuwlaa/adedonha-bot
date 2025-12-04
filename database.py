# database.py - sqlite helpers (async-safe with a lock)
import sqlite3
import asyncio
import logging
from typing import List, Optional
from .utils import DB_FILE

logger = logging.getLogger(__name__)

db_conn: Optional[sqlite3.Connection] = None
db_lock: Optional[asyncio.Lock] = None

def init_db():
    """Create DB file and table if missing (legacy safe)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def db_migrate(conn: sqlite3.Connection):
    """Add missing columns if they don't exist (safe)."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(stats)")
    cols = [row[1] for row in c.fetchall()]
    if "games_played" not in cols:
        try:
            c.execute("ALTER TABLE stats ADD COLUMN games_played INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug("games_played add failed: %s", e)
    if "total_validated_words" not in cols:
        try:
            c.execute("ALTER TABLE stats ADD COLUMN total_validated_words INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug("total_validated_words add failed: %s", e)
    if "total_wordlists_sent" not in cols:
        try:
            c.execute("ALTER TABLE stats ADD COLUMN total_wordlists_sent INTEGER DEFAULT 0")
        except Exception as e:
            logger.debug("total_wordlists_sent add failed: %s", e)
    conn.commit()

async def setup_db():
    global db_conn, db_lock
    init_db()
    db_conn = sqlite3.connect(DB_FILE, check_same_thread=False, isolation_level=None)
    try:
        db_conn.execute("PRAGMA journal_mode=WAL;")
    except Exception as e:
        logger.warning("Failed to set WAL mode: %s", e)
    db_migrate(db_conn)
    db_lock = asyncio.Lock()

# ---------------- ASYNC DB HELPERS ----------------
async def db_ensure_user(uid: str) -> None:
    global db_conn, db_lock
    if db_conn is None:
        raise RuntimeError("DB not initialized")
    async with db_lock:
        c = db_conn.cursor()
        c.execute("SELECT 1 FROM stats WHERE user_id=?", (uid,))
        if not c.fetchone():
            c.execute("INSERT INTO stats (user_id, games_played, total_validated_words, total_wordlists_sent) VALUES (?, 0, 0, 0)", (uid,))
        db_conn.commit()

async def db_update_after_round(uid: str, validated_words: int, submitted_any: bool) -> None:
    global db_conn, db_lock
    if db_conn is None:
        raise RuntimeError("DB not initialized")
    async with db_lock:
        c = db_conn.cursor()
        c.execute("SELECT 1 FROM stats WHERE user_id=?", (uid,))
        if not c.fetchone():
            c.execute("INSERT INTO stats (user_id, games_played, total_validated_words, total_wordlists_sent) VALUES (?, 0, 0, 0)", (uid,))
        if submitted_any:
            c.execute("""
                UPDATE stats
                SET total_wordlists_sent = total_wordlists_sent + 1,
                    total_validated_words = total_validated_words + ?
                WHERE user_id=?
            """, (validated_words, uid))
        db_conn.commit()

async def db_update_after_game(user_ids: List[str]) -> None:
    global db_conn, db_lock
    if db_conn is None:
        raise RuntimeError("DB not initialized")
    async with db_lock:
        c = db_conn.cursor()
        for uid in user_ids:
            c.execute("SELECT 1 FROM stats WHERE user_id=?", (uid,))
            if not c.fetchone():
                c.execute("INSERT INTO stats (user_id, games_played, total_validated_words, total_wordlists_sent) VALUES (?, 0, 0, 0)", (uid,))
            c.execute("UPDATE stats SET games_played = COALESCE(games_played,0) + 1 WHERE user_id=?", (uid,))
        db_conn.commit()

async def db_get_stats(uid: str):
    global db_conn, db_lock
    if db_conn is None:
        raise RuntimeError("DB not initialized")
    async with db_lock:
        c = db_conn.cursor()
        c.execute("SELECT games_played, total_validated_words, total_wordlists_sent FROM stats WHERE user_id=?", (uid,))
        row = c.fetchone()
        if row:
            return {"games_played": row[0] or 0, "total_validated_words": row[1] or 0, "total_wordlists_sent": row[2] or 0}
        c.execute("INSERT OR IGNORE INTO stats (user_id, games_played, total_validated_words, total_wordlists_sent) VALUES (?, 0, 0, 0)", (uid,))
        db_conn.commit()
        return {"games_played": 0, "total_validated_words": 0, "total_wordlists_sent": 0}

async def db_dump_all():
    global db_conn, db_lock
    if db_conn is None:
        raise RuntimeError("DB not initialized")
    async with db_lock:
        c = db_conn.cursor()
        c.execute("SELECT user_id, games_played, total_validated_words, total_wordlists_sent FROM stats ORDER BY total_validated_words DESC")
        rows = c.fetchall()
        return rows

async def db_reset_all():
    global db_conn, db_lock
    if db_conn is None:
        raise RuntimeError("DB not initialized")
    async with db_lock:
        c = db_conn.cursor()
        c.execute("UPDATE stats SET games_played=0, total_validated_words=0, total_wordlists_sent=0")
        db_conn.commit()
