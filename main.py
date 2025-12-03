# ---------------- CONFIG - set tokens directly here ----------------
TELEGRAM_BOT_TOKEN = ""
OPENAI_API_KEY = ""  # optional - leave empty to use manual admin validation

# ---------------- OWNERS / ADMINS ----------------
OWNERS = {"624102836", "1707015091"}  # string IDs of bot owners who can run owner-only commands

# ---------------- CONSTANTS ----------------
MAX_PLAYERS = 10
LOBBY_TIMEOUT = 5 * 60  # 5 minutes for lobby auto-cancel
CLASSIC_NO_SUBMIT_TIMEOUT = 3 * 60  # 3 minutes if no first submission
CLASSIC_FIRST_WINDOW = 2  # 2 seconds after first submission
FAST_ROUND_SECONDS = 60  # 1 minute per round in fast mode
FAST_FIRST_WINDOW = 2  # 2 seconds immediate window after first submission
TOTAL_ROUNDS_CLASSIC = 10
TOTAL_ROUNDS_FAST = 12
DB_FILE = "stats.db"
AI_MODEL = "gpt-4.1-mini"
PLAYER_EMOJI = "🦩"
LOBBY_EMOJI = "✨"
MIN_ROUNDS = 6
MAX_ROUNDS = 12
DEFAULT_TOTAL_ROUNDS = 10

ALL_CATEGORIES = [
    "Name",
    "Object",
    "Animal",
    "Plant",
    "City",
    "Country",
    "State",
    "Food",
    "Color",
    "Movie/Series/TV Show",
    "Place",
    "Fruit",
    "Profession",
    "Adjective",
]

# ---------------- IMPORTS ----------------
import logging
import random
import asyncio
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import html as _html
import os
import re
import traceback

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- OPENAI CLIENT ----------------
ai_client: Optional[OpenAI] = None
if OPENAI_API_KEY and OpenAI is not None and not OPENAI_API_KEY.startswith("YOUR_"):
    try:
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning("OpenAI client init failed: %s. Bot will fall back to manual admin validation.", e)
        ai_client = None

# ---------------- GLOBAL DB CONNECTION + LOCK (initialized in setup_db) ----------------
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
    """Set up a global SQLite connection with WAL mode and an asyncio lock."""
    global db_conn, db_lock
    init_db()
    db_conn = sqlite3.connect(DB_FILE, check_same_thread=False, isolation_level=None)
    try:
        db_conn.execute("PRAGMA journal_mode=WAL;")
    except Exception as e:
        logger.warning("Failed to set WAL mode: %s", e)
    db_migrate(db_conn)
    db_lock = asyncio.Lock()

# ---------------- UTIL ----------------
def escape_html(text: str) -> str:
    if text is None:
        return ""
    return _html.escape(str(text), quote=False)

def user_mention_html(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{escape_html(name)}</a>'

def choose_random_categories(count: int) -> List[str]:
    return random.sample(ALL_CATEGORIES, count)

def extract_answers_from_text(text: str, count: int) -> List[str]:
    # Accept numbered, "Category: answer", or plain lines. Return exactly count items.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answers = []
    for line in lines:
        # If colon present, assume "Category: answer" or "1. answer"
        if ":" in line:
            parts = line.split(":", 1)
            answers.append(parts[1].strip())
        else:
            # remove leading "1. ", "2) " etc
            m = re.match(r'^\s*\d+[\.\)]\s*(.*)$', line)
            if m:
                answers.append(m.group(1).strip())
            else:
                answers.append(line.strip())
        if len(answers) >= count:
            break
    while len(answers) < count:
        answers.append("")
    return answers[:count]

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

# ---------------- AI VALIDATION & CORRECTION ----------------
async def ai_validate(category: str, answer: str, letter: str) -> bool:
    """Backward-compatible simple validator: only letter check if no AI configured."""
    if not answer:
        return False
    if not answer[0].isalpha() or answer[0].upper() != letter.upper():
        return False
    if not ai_client:
        return True
    # Basic prompt to get YES/NO
    prompt = f"""
You are a terse validator for the word game Adedonha.
Rules:
- The answer must start with the letter '{letter}' (case-insensitive).
- It must belong to the category: '{category}'.
- The answer must be a real word or valid proper noun when appropriate.
Respond with only YES or NO (no extra text).

Answer: {answer}
"""
    try:
        resp = ai_client.responses.create(model=AI_MODEL, input=prompt, max_output_tokens=6)
        out = ""
        if getattr(resp, "output", None):
            for block in resp.output:
                if isinstance(block, dict):
                    content = block.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and "text" in item:
                                out += item["text"]
                            elif isinstance(item, str):
                                out += item
                    elif isinstance(content, str):
                        out += content
                elif isinstance(block, str):
                    out += block
        out = out.strip().upper()
        return out.startswith("YES")
    except Exception as e:
        logger.warning("AI validation error: %s", e)
        return True

async def ai_correct_and_validate(category: str, answer: str, letter: str) -> Tuple[bool, str]:
    """
    Use OpenAI to:
    - Correct minor spelling mistakes
    - Confirm the corrected word starts with the letter
    - Confirm the word fits the category
    Returns: (is_valid, corrected_word)
    If OpenAI unavailable, do basic letter-check and return original.
    """
    orig = (answer or "").strip()
    if not orig:
        return False, orig
    if not ai_client:
        # fallback: letter check only
        if orig[0].upper() != letter.upper():
            return False, orig
        return True, orig
    prompt = f"""
You are a helpful assistant used to validate player submissions for the game Adedonha.
Take the input word or phrase and:
1) If there is a minor spelling mistake, correct it (reply with CORRECTED: <word>).
2) Decide if the corrected word belongs to the category "{category}".
3) Ensure the corrected word starts with the letter "{letter}".
4) Reply in JSON format ONLY with keys:
{{
  "corrected": "<corrected word>",
  "starts_with_letter": "YES" or "NO",
  "in_category": "YES" or "NO"
}}
Input: {orig}
Notes:
- If the input is a multi-word phrase, correct common misspellings individually but keep the phrase if appropriate.
- Be strict about category membership.
"""
    try:
        resp = ai_client.responses.create(model=AI_MODEL, input=prompt, max_output_tokens=200)
        out = ""
        if getattr(resp, "output", None):
            for block in resp.output:
                if isinstance(block, dict):
                    content = block.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and "text" in item:
                                out += item["text"]
                            elif isinstance(item, str):
                                out += item
                    elif isinstance(content, str):
                        out += content
                elif isinstance(block, str):
                    out += block
        out = out.strip()
        # Try to extract JSON-like parts
        # We will look for corrected:, starts_with_letter, in_category tokens if JSON parse fails.
        corrected = orig
        starts = "NO"
        incat = "NO"
        # simple heuristics: look for "corrected" and values
        m_cor = re.search(r'"corrected"\s*:\s*"([^"]+)"', out)
        if m_cor:
            corrected = m_cor.group(1).strip()
        else:
            m_cor = re.search(r'CORRECTED:\s*([^\n\r]+)', out, re.IGNORECASE)
            if m_cor:
                corrected = m_cor.group(1).strip()
        m_start = re.search(r'"starts_with_letter"\s*:\s*"([^"]+)"', out)
        if m_start:
            starts = m_start.group(1).strip().upper()
        else:
            if re.search(r'\bstarts_with_letter\b.*YES', out, re.IGNORECASE):
                starts = "YES"
        m_in = re.search(r'"in_category"\s*:\s*"([^"]+)"', out)
        if m_in:
            incat = m_in.group(1).strip().upper()
        else:
            if re.search(r'\bin_category\b.*YES', out, re.IGNORECASE):
                incat = "YES"
        # final checks
        if corrected and corrected[0].isalpha() and corrected[0].upper() != letter.upper():
            # corrected doesn't start with desired letter
            starts = "NO"
        is_valid = (starts == "YES" and incat == "YES")
        return is_valid, corrected
    except Exception as e:
        logger.warning("AI correction error: %s", e)
        return True, orig

# ---------------- GAME STATE ----------------
games: Dict[int, Dict] = {}  # chat_id -> game dict

def is_owner(uid: int) -> bool:
    return str(uid) in OWNERS

# ---------------- LOBBY / GAME CREATION ----------------
async def classic_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    if chat.id in games and games[chat.id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active in this group.")
        return
    lobby = {
        "mode": "classic",
        "categories_per_round": 5,
        "creator_id": user.id,
        "creator_name": user.first_name,
        "players": {str(user.id): user.first_name},
        "state": "lobby",
        "created_at": datetime.utcnow().isoformat(),
        "lobby_message_id": None,
        "lobby_task": None,
        "round": 0,
        "submissions": {},
        "manual_validation_msg_id": None,
        "validation_panel_message_id": None,
        "manual_accept": {},
        "total_rounds": DEFAULT_TOTAL_ROUNDS,
        "cancelled": False,
    }
    games[chat.id] = lobby
    kb = _lobby_keyboard(lobby)
    players_html = user_mention_html(user.id, user.first_name)
    text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Classic</b>\nCategories per round: <b>{lobby['categories_per_round']}</b>\nTotal rounds: <b>{lobby['total_rounds']}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info("Pin failed: %s", e)
    async def lobby_timeout():
        try:
            await asyncio.sleep(LOBBY_TIMEOUT)
            g = games.get(chat.id)
            if g and g.get("state") == "lobby":
                if len(g.get("players", {})) <= 1:
                    try:
                        await context.bot.send_message(chat.id, "Lobby cancelled due to inactivity.")
                    except Exception:
                        pass
                    games.pop(chat.id, None)
        except asyncio.CancelledError:
            return
    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

async def custom_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Please provide categories, e.g. /customadedonha Name Object Animal Plant Country")
        return
    joined = " ".join(args)
    parts = [p.strip() for p in joined.replace(",", " ").split() if p.strip()]
    cats = parts[:12]
    if chat.id in games and games[chat.id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active in this group.")
        return
    lobby = {
        "mode": "custom",
        "categories_pool": cats,
        "creator_id": user.id,
        "creator_name": user.first_name,
        "players": {str(user.id): user.first_name},
        "state": "lobby",
        "created_at": datetime.utcnow().isoformat(),
        "lobby_message_id": None,
        "lobby_task": None,
        "round": 0,
        "submissions": {},
        "manual_validation_msg_id": None,
        "validation_panel_message_id": None,
        "manual_accept": {},
        "total_rounds": DEFAULT_TOTAL_ROUNDS,
        "cancelled": False,
    }
    games[chat.id] = lobby
    kb = _lobby_keyboard(lobby)
    players_html = user_mention_html(user.id, user.first_name)
    cat_lines = "\n".join(f"- {escape_html(c)}" for c in cats)
    text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Custom</b>\nCategories pool:\n{cat_lines}\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info("Pin failed: %s", e)
    async def lobby_timeout():
        try:
            await asyncio.sleep(LOBBY_TIMEOUT)
            g = games.get(chat.id)
            if g and g.get("state") == "lobby":
                if len(g.get("players", {})) <= 1:
                    try:
                        await context.bot.send_message(chat.id, "Lobby cancelled due to inactivity.")
                    except Exception:
                        pass
                    games.pop(chat.id, None)
        except asyncio.CancelledError:
            return
    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

async def fast_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    args = context.args or []
    if not args or len(args) < 3:
        await update.message.reply_text("Please provide exactly 3 categories, e.g. /fastadedonha Name Object Animal")
        return
    cats = [a.strip() for a in args[:3]]
    if chat.id in games and games[chat.id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active in this group.")
        return
    lobby = {
        "mode": "fast",
        "fixed_categories": cats,
        "creator_id": user.id,
        "creator_name": user.first_name,
        "players": {str(user.id): user.first_name},
        "state": "lobby",
        "created_at": datetime.utcnow().isoformat(),
        "lobby_message_id": None,
        "lobby_task": None,
        "round": 0,
        "submissions": {},
        "manual_validation_msg_id": None,
        "validation_panel_message_id": None,
        "manual_accept": {},
        "total_rounds": TOTAL_ROUNDS_FAST,
        "cancelled": False,
    }
    games[chat.id] = lobby
    kb = _lobby_keyboard(lobby)
    players_html = user_mention_html(user.id, user.first_name)
    cats_md = "\n".join(f"- {escape_html(c)}" for c in cats)
    text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Fast</b>\nFixed categories:\n{cats_md}\nTotal rounds: <b>{lobby['total_rounds']}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info("Pin failed: %s", e)
    async def lobby_timeout():
        try:
            await asyncio.sleep(LOBBY_TIMEOUT)
            g = games.get(chat.id)
            if g and g.get("state") == "lobby":
                if len(g.get("players", {})) <= 1:
                    try:
                        await context.bot.send_message(chat.id, "Lobby cancelled due to inactivity.")
                    except Exception:
                        pass
                    games.pop(chat.id, None)
        except asyncio.CancelledError:
            return
    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

def _lobby_keyboard(lobby: Dict) -> InlineKeyboardMarkup:
    """Build lobby keyboard with rounds +/- and join/start buttons."""
    rounds = lobby.get("total_rounds", DEFAULT_TOTAL_ROUNDS)
    kb = [
        [InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],
        [
            InlineKeyboardButton("➖", callback_data="rounds_dec"),
            InlineKeyboardButton(f"Rounds: {rounds}", callback_data="rounds_show"),
            InlineKeyboardButton("➕", callback_data="rounds_inc"),
        ],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ]
    return InlineKeyboardMarkup(kb)

# ---------------- JOIN (button + /joingame) ----------------
async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, by_command: bool = False):
    if update.callback_query:
        cq = update.callback_query
        chat_id = cq.message.chat.id
        user = cq.from_user
        await cq.answer()
    else:
        chat_id = update.effective_chat.id
        user = update.effective_user
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        if by_command:
            await context.bot.send_message(chat_id, "No active lobby to join.")
        return
    if len(g["players"]) >= MAX_PLAYERS:
        await context.bot.send_message(chat_id, "Lobby is full (10 players).")
        return
    if str(user.id) in g["players"]:
        if by_command:
            await context.bot.send_message(chat_id, "You already joined.")
        else:
            try:
                await update.callback_query.answer("You already joined.")
            except Exception:
                pass
        return
    g["players"][str(user.id)] = user.first_name
    # update lobby message
    players_html = "\n".join(user_mention_html(int(uid), name) for uid, name in g["players"].items())
    if g["mode"] == "classic":
        text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Classic</b>\nCategories per round: <b>{g['categories_per_round']}</b>\nTotal rounds: <b>{g['total_rounds']}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    elif g["mode"] == "custom":
        cat_lines = "\n".join(f"- {escape_html(c)}" for c in g.get("categories_pool", []))
        text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Custom</b>\nCategories pool:\n{cat_lines}\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    else:
        cats_md = "\n".join(f"- {escape_html(c)}" for c in g.get("fixed_categories", []))
        text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Fast</b>\nFixed categories:\n{cats_md}\nTotal rounds: <b>{g['total_rounds']}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="HTML", reply_markup=_lobby_keyboard(g))
    except Exception:
        await context.bot.send_message(chat_id, f"{user_mention_html(user.id, user.first_name)} joined the lobby.", parse_mode="HTML")

async def joingame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    await join_callback(update, context, by_command=True)

# ---------------- MODE INFO ----------------
async def mode_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    await cq.answer()
    g = games.get(chat_id)
    if not g:
        await context.bot.send_message(chat_id, "No active lobby/game.")
        return
    mode = g["mode"]
    if mode == "classic":
        num = g.get("categories_per_round", 5)
        text = (f"<b>Classic Adedonha</b>\nEach round uses the fixed 5 categories (Name, Object, Animal, Plant, Country).\nIf no one submits, the round ends after 3 minutes. After the first submission others have 2 seconds to submit.")
    elif mode == "custom":
        pool = g.get("categories_pool", [])
        pool_html = "\n".join(f"- {escape_html(c)}" for c in pool)
        text = (f"<b>Custom Adedonha</b>\nCategories pool for this game:\n{pool_html}\nThis game uses exactly the categories provided when creating the custom game (no randomization). Timing: same as Classic.")
    else:
        cats = g.get("fixed_categories", [])
        cats_html = "\n".join(f"- {escape_html(c)}" for c in cats)
        text = (f"<b>Fast Adedonha</b>\nFixed categories:\n{cats_html}\nEach round is {FAST_ROUND_SECONDS} seconds total. First submission gives 2s immediate window.")
    await context.bot.send_message(chat_id, text, parse_mode="HTML")

# ---------------- START GAME (button only starts game) ----------------
async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    await cq.answer()
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await context.bot.send_message(chat_id, "No lobby to start.")
        return
    # only allow creator or chat admin to start
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if user.id != g["creator_id"] and not is_admin and not is_owner(user.id):
        await context.bot.send_message(chat_id, "Only the creator, a chat admin, or owner can start the game.")
        return
    # unpin lobby
    try:
        await context.bot.unpin_chat_message(chat_id)
    except Exception:
        pass
    # cancel lobby timeout
    if g.get("lobby_task"):
        try:
            g["lobby_task"].cancel()
        except Exception:
            pass
    g["state"] = "running"
    # remove buttons from lobby message
    try:
        await context.bot.edit_message_reply_markup(chat_id, g["lobby_message_id"], reply_markup=None)
    except Exception:
        pass
    # Start a background task for the game
    asyncio.create_task(run_game(chat_id, context))

# ---------------- RUN GAME (main loop) ----------------
async def run_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return
    # If cancelled flag present, abort
    if g.get("cancelled"):
        return
    mode = g["mode"]
    total_rounds = g.get("total_rounds", DEFAULT_TOTAL_ROUNDS)
    if mode == "classic":
        rounds = total_rounds
        per_round = g.get("categories_per_round", 5)
    elif mode == "custom":
        rounds = total_rounds
        pool = g.get("categories_pool", ALL_CATEGORIES)
        per_round = min(len(pool), max(1, len(pool)))
    else:
        rounds = total_rounds
        per_round = len(g.get("fixed_categories", []))
    # initialize scores and per-player word counts
    g["scores"] = {uid: 0 for uid in g["players"].keys()}
    g["word_counts"] = {uid: 0 for uid in g["players"].keys()}
    g["round_scores_history"] = []
    await db_update_after_game(list(g["players"].keys()))

    # announce starting
    try:
        await context.bot.send_message(chat_id, "🌟 Game is starting... 🌟")
        await asyncio.sleep(3)  # required 3-second delay
    except Exception:
        pass

    for r in range(1, rounds + 1):
        # check cancellation
        if g.get("cancelled"):
            try:
                await context.bot.send_message(chat_id, "Game cancelled ✋. All running rounds stopped.")
            except Exception:
                pass
            break

        g["round"] = r
        if mode == "classic":
            categories = ["Name", "Object", "Animal", "Plant", "Country"][:g.get("categories_per_round", 5)]
            per_round = len(categories)
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        elif mode == "custom":
            categories = g.get("categories_pool", ALL_CATEGORIES)
            per_round = len(categories)
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        else:
            categories = g.get("fixed_categories", [])
            per_round = len(categories)
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = FAST_FIRST_WINDOW
            no_submit_timeout = FAST_ROUND_SECONDS
            round_time_limit = FAST_ROUND_SECONDS

        g["current_categories"] = categories
        g["round_letter"] = letter
        g["submissions"] = {}
        g["manual_accept"] = {}

        # send round template in monospace (pre) and cute emojis
        pre_block = "\n".join(f"{i+1}. {escape_html(c)}:" for i, c in enumerate(categories))
        intro = (f"🕹️ <b>Round {r} / {rounds}</b>  {PLAYER_EMOJI}\n"
                 f"Letter: <b>{escape_html(letter)}</b>\n\n"
                 f"<pre>{pre_block}</pre>\n\n"
                 "Send your answers in ONE MESSAGE using the template above (first "
                 f"{len(categories)} answers will be used). Only IN-GAME players are accepted. "
                 f"First submission starts a {window_seconds}s window for others.")
        try:
            await context.bot.send_message(chat_id, intro, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id, intro)

        # schedule no_submit timeout worker
        end_event = asyncio.Event()
        first_submitter = None
        first_submit_time: Optional[datetime] = None

        async def no_submit_worker():
            await asyncio.sleep(no_submit_timeout)
            if not g.get("submissions"):
                try:
                    await context.bot.send_message(chat_id, f"⏱ Round {r} ended: no submissions. No points awarded.")
                except Exception:
                    pass
                g["round_scores_history"].append({})
                end_event.set()

        no_submit_task = asyncio.create_task(no_submit_worker())

        # wait loop - submissions are collected by submission_handler
        while not end_event.is_set():
            await asyncio.sleep(0.5)
            if g.get("submissions") and not first_submitter:
                first_submitter = next(iter(g["submissions"].keys()))
                first_submit_time = datetime.utcnow()
                try:
                    await context.bot.send_message(chat_id, f"⏱ {user_mention_html(int(first_submitter), g['players'][first_submitter])} submitted first! Others have {window_seconds}s to submit.", parse_mode="HTML")
                except Exception:
                    await context.bot.send_message(chat_id, f"{escape_html(g['players'][first_submitter])} submitted first! Others have {window_seconds}s to submit.")
                async def window_worker():
                    await asyncio.sleep(window_seconds)
                    end_event.set()
                asyncio.create_task(window_worker())
            if round_time_limit and first_submit_time:
                if (datetime.utcnow() - first_submit_time).total_seconds() >= round_time_limit:
                    end_event.set()
            # allow cancellation mid-round
            if g.get("cancelled"):
                end_event.set()

        # cancel no_submit_task if running
        try:
            no_submit_task.cancel()
        except Exception:
            pass

        submissions = g.get("submissions", {})
        if not submissions:
            continue

        # Parsing submissions
        parsed = {}
        for uid, txt in submissions.items():
            parsed[uid] = extract_answers_from_text(txt, len(categories))

        # Frequency counts per category for uniqueness scoring
        per_cat_freq = [ {} for _ in range(len(categories)) ]
        for idx in range(len(categories)):
            for uid, answers in parsed.items():
                a = answers[idx].strip()
                if a:
                    key = a.lower()
                    per_cat_freq[idx][key] = per_cat_freq[idx].get(key, 0) + 1

        # Scoring loop: for each player's answers, run correction+validation and compute points
        round_scores = {}
        for uid, answers in parsed.items():
            pts = 0
            validated_count = 0
            submitted_any = any(a.strip() for a in answers)
            for idx, a in enumerate(answers):
                a_clean = a.strip()
                if not a_clean:
                    continue
                # letter check & AI correction/validation
                try:
                    is_valid, corrected = await ai_correct_and_validate(categories[idx], a_clean, letter)
                except Exception as e:
                    logger.exception("ai_correct_and_validate error: %s", e)
                    is_valid, corrected = await ai_validate(categories[idx], a_clean, letter), a_clean

                # allow manual override if admin marked it
                man = g.get("manual_accept", {}).get(uid)
                if man is True:
                    is_valid = True
                elif man is False:
                    is_valid = False

                if not is_valid:
                    continue

                key = corrected.lower()
                cnt = per_cat_freq[idx].get(a_clean.lower(), 0)
                # uniqueness: if nobody else wrote same (case-insensitive on original submission) => 10 points, else 5
                if cnt == 1:
                    pts += 10
                else:
                    pts += 5
                validated_count += 1
                # count words for summary: each validated answer counts as 1
                g["word_counts"][uid] = g["word_counts"].get(uid, 0) + 1

            round_scores[uid] = {"points": pts, "validated": validated_count, "submitted_any": submitted_any}
            g["scores"][uid] = g["scores"].get(uid, 0) + pts
            await db_update_after_round(uid, validated_count, submitted_any)

        g["round_scores_history"].append(round_scores)

        # Round summary message with emojis
        header = f"🏁 <b>Round {r} Results</b>\nLetter: <b>{escape_html(letter)}</b>\n\n"
        body = ""
        sorted_players = sorted(g["players"].items(), key=lambda x: -g["scores"].get(x[0],0))
        for uid, name in sorted_players:
            pts = round_scores.get(uid, {}).get("points", 0)
            validated = round_scores.get(uid, {}).get("validated", 0)
            body += f"{user_mention_html(int(uid), name)} — <code>{pts}</code> pts ({validated} validated)\n"
        try:
            await context.bot.send_message(chat_id, header + body, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id, header + body)

        # tiny pause between rounds
        await asyncio.sleep(1)

    # if game was cancelled skip final leaderboard details besides message
    if g.get("cancelled"):
        try:
            await context.bot.send_message(chat_id, "Game was cancelled. Returning to idle.")
        except Exception:
            pass
        games.pop(chat_id, None)
        return

    # final leaderboard and per-player total words summary
    lb = sorted(g["scores"].items(), key=lambda x: -x[1])
    text = "🎉 <b>Game Over — Final Scores</b> 🎉\n\n"
    for uid, pts in lb:
        name = g["players"].get(uid, "Player")
        words = g.get("word_counts", {}).get(uid, 0)
        text += f"{user_mention_html(int(uid), name)} — <code>{pts}</code> pts — {words} words\n"
    # add small summary: top contributor and total words given
    total_words_by_player = [(g["players"].get(uid, "Player"), g.get("word_counts", {}).get(uid, 0), g["scores"].get(uid,0)) for uid in g["players"].keys()]
    total_words = sum(x[1] for x in total_words_by_player)
    best = max(total_words_by_player, key=lambda x: x[2]) if total_words_by_player else None
    text += "\n"
    text += f"📝 Total validated words this game: <b>{total_words}</b>\n"
    if best:
        text += f"🏆 MVP: {escape_html(best[0])} with <b>{best[2]}</b> pts and {best[1]} words! {PLAYER_EMOJI}\n"
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id, text)

    # cleanup state
    games.pop(chat_id, None)

# ---------------- SUBMISSIONS ----------------
async def submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        return
    g = games.get(chat.id)
    if not g or g.get("state") != "running":
        return
    if str(user.id) not in g["players"]:
        return
    uid = str(user.id)
    if uid in g.get("submissions", {}):
        try:
            await update.message.reply_text("You already submitted for this round.")
        except Exception:
            pass
        return
    text = update.message.text or ""
    # Strict template detection: require at least N answer lines where N = number of categories this round.
    needed = len(g.get('current_categories', [])) or g.get('categories_per_round', 0)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answer_lines = 0
    for ln in lines:
        if ':' in ln:
            answer_lines += 1
        elif re.match(r'^\s*\d+[\.\)]\s*', ln):
            answer_lines += 1
    if answer_lines < needed:
        # not considered a valid submission - ignore silently
        return
    # register the submission
    g['submissions'][uid] = text
    # If AI not configured create manual validation message for admins to review
    if not ai_client:
        if not g.get('manual_validation_msg_id'):
            preview = ''
            for uid2, txt in g['submissions'].items():
                preview += f"{g['players'][uid2]}: {txt[:120]}\n"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open validation panel ✅", callback_data="open_manual_validate")]])
            msg_text = f"<b>Manual validation required</b>\nAI not configured. Admins may validate via panel.\n\nSubmissions preview:\n{escape_html(preview)}"
            try:
                msg = await context.bot.send_message(chat.id, msg_text, parse_mode="HTML", reply_markup=kb)
                g['manual_validation_msg_id'] = msg.message_id
            except Exception:
                pass

# ---------------- MANUAL VALIDATION PANEL ----------------
async def open_manual_validate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    g = games.get(chat_id)
    await cq.answer()
    # only admins allowed to validate manually
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status not in ("administrator", "creator"):
            await cq.answer("Only chat admins can validate manually.", show_alert=True)
            return
    except Exception:
        await cq.answer("Only chat admins can validate manually.", show_alert=True)
        return
    # build panel - one shared message with buttons per submission to Accept/Reject
    buttons = []
    for uid, txt in g.get("submissions", {}).items():
        lbl = escape_html(g["players"].get(uid, "Player"))
        buttons.append([InlineKeyboardButton(f"✅ {lbl}", callback_data=f"validate_accept|{uid}"), InlineKeyboardButton(f"❌ {lbl}", callback_data=f"validate_reject|{uid}")])
    buttons.append([InlineKeyboardButton("Close 🛑", callback_data="validate_close")])
    if g.get("validation_panel_message_id"):
        try:
            await context.bot.edit_message_text("Validation panel (admins):", chat_id, g["validation_panel_message_id"], reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass
    else:
        msg = await context.bot.send_message(chat_id, "Validation panel (admins):", reply_markup=InlineKeyboardMarkup(buttons))
        g["validation_panel_message_id"] = msg.message_id

async def validation_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    user = cq.from_user
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    await cq.answer()
    # admin check
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status not in ("administrator", "creator"):
            await cq.answer("Only chat admins can use this panel.", show_alert=True)
            return
    except Exception:
        await cq.answer("Only chat admins can use this panel.", show_alert=True)
        return
    if data == "validate_close":
        try:
            await context.bot.delete_message(chat_id, cq.message.message_id)
        except Exception:
            pass
        g.pop("validation_panel_message_id", None)
        return
    if data.startswith("validate_accept|") or data.startswith("validate_reject|"):
        action, uid = data.split("|", 1)
        if action == "validate_accept":
            g.setdefault("manual_accept", {})[uid] = True
            await cq.answer("Marked as accepted.")
        else:
            g.setdefault("manual_accept", {})[uid] = False
            await cq.answer("Marked as rejected.")
        # rebuild buttons to reflect state
        buttons = []
        for uid2, txt in g.get("submissions", {}).items():
            name = escape_html(g["players"].get(uid2, "Player"))
            acc = g.get("manual_accept", {}).get(uid2)
            if acc is True:
                b1 = InlineKeyboardButton(f"✅ {name}", callback_data=f"validate_accept|{uid2}")
            elif acc is False:
                b1 = InlineKeyboardButton(f"❌ {name}", callback_data=f"validate_reject|{uid2}")
            else:
                b1 = InlineKeyboardButton(name, callback_data=f"validate_none|{uid2}")
            buttons.append([b1, InlineKeyboardButton("Toggle", callback_data=f"validate_toggle|{uid2}")])
        buttons.append([InlineKeyboardButton("Close 🛑", callback_data="validate_close")])
        try:
            await context.bot.edit_message_reply_markup(chat_id, g.get("validation_panel_message_id"), reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass

# ---------------- CALLBACK ROUTER ----------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data or ""
    if data == "join_lobby":
        await join_callback(update, context)
    elif data == "mode_info":
        await mode_info_callback(update, context)
    elif data == "start_game":
        await start_game_callback(update, context)
    elif data == "open_manual_validate":
        await open_manual_validate(update, context)
    elif data.startswith("validate_"):
        await validation_button_handler(update, context)
    elif data == "rounds_inc":
        await adjust_rounds_callback(update, context, inc=True)
    elif data == "rounds_dec":
        await adjust_rounds_callback(update, context, inc=False)
    elif data == "rounds_show":
        await update.callback_query.answer("Adjust total rounds using ➕ / ➖ (6-12).")
    else:
        await update.callback_query.answer("Unknown action.", show_alert=True)

# ---------------- ADJUST ROUNDS CALLBACK ----------------
async def adjust_rounds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, inc: bool):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    await cq.answer()
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await context.bot.send_message(chat_id, "No active lobby to adjust rounds.")
        return
    # only lobby creator or admin may change rounds
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if user.id != g["creator_id"] and not is_admin and not is_owner(user.id):
        await context.bot.send_message(chat_id, "Only the creator, a chat admin, or owner can change rounds.")
        return
    cur = g.get("total_rounds", DEFAULT_TOTAL_ROUNDS)
    if inc:
        cur = min(MAX_ROUNDS, cur + 1)
    else:
        cur = max(MIN_ROUNDS, cur - 1)
    g["total_rounds"] = cur
    # update lobby message
    players_html = "\n".join(user_mention_html(int(uid), name) for uid, name in g["players"].items())
    if g["mode"] == "classic":
        text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Classic</b>\nCategories per round: <b>{g['categories_per_round']}</b>\nTotal rounds: <b>{g['total_rounds']}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    elif g["mode"] == "custom":
        cat_lines = "\n".join(f"- {escape_html(c)}" for c in g.get("categories_pool", []))
        text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Custom</b>\nCategories pool:\n{cat_lines}\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    else:
        cats_md = "\n".join(f"- {escape_html(c)}" for c in g.get("fixed_categories", []))
        text = (f"<b>{LOBBY_EMOJI} Adedonha lobby created! {LOBBY_EMOJI}</b>\n\nMode: <b>Fast</b>\nFixed categories:\n{cats_md}\nTotal rounds: <b>{g['total_rounds']}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="HTML", reply_markup=_lobby_keyboard(g))
    except Exception:
        pass

# ---------------- GAMECANCEL ----------------
async def gamecancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = games.get(chat.id)
    if not g:
        await update.message.reply_text("No active game/lobby to cancel.")
        return
    # permission: creator, chat admin, or owner
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if user.id != g["creator_id"] and not is_admin and not is_owner(user.id):
        await update.message.reply_text("Only the creator, a chat admin, or bot owner can cancel the game.")
        return
    try:
        await context.bot.unpin_chat_message(chat.id)
    except Exception:
        pass
    # set cancelled flag and attempt to cancel lobby task
    g["cancelled"] = True
    if g.get("lobby_task"):
        try:
            g["lobby_task"].cancel()
        except Exception:
            pass
    games.pop(chat.id, None)
    await update.message.reply_text("Game cancelled. ✅")

# ---------------- CATEGORIES / MYSTATS / DUMP / RESET / LEADERBOARD ----------------
async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "<b>All possible categories (14):</b>\n" + "\n".join(f"{i+1}. {escape_html(c)}" for i, c in enumerate(ALL_CATEGORIES))
    await update.message.reply_text(text, parse_mode="HTML")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    uid = str(target.id)
    s = await db_get_stats(uid)
    all_rows = await db_dump_all()
    rank = 1
    for idx, row in enumerate(all_rows, start=1):
        if row[0] == uid:
            rank = idx
            break
    text = (f"<b>Stats of {user_mention_html(int(uid), target.first_name)}</b>\n\n"
            f"• <b>Games played:</b> <code>{s.get('games_played',0)}</code>\n"
            f"• <b>Total validated words:</b> <code>{s.get('total_validated_words',0)}</code>\n"
            f"• <b>Wordlists sent:</b> <code>{s.get('total_wordlists_sent',0)}</code>\n"
            f"• <b>Global position:</b> <code>{rank}</code>\n")
    await update.message.reply_text(text, parse_mode="HTML")

async def dumpstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    rows = await db_dump_all()
    header = "user_id,games_played,total_validated_words,total_wordlists_sent\n"
    csv_path = "/tmp/stats_export.csv"
    with open(csv_path, "w", encoding="utf8") as f:
        f.write(header)
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    text = "<b>Stats export (top by validated words)</b>\n\n"
    for r in rows[:50]:
        text += f"{escape_html(r[0])} — games:{r[1]} validated:{r[2]} lists:{r[3]}\n"
    await update.message.reply_text(text, parse_mode="HTML")
    await update.message.reply_document(open(csv_path, "rb"))
    await update.message.reply_document(open(DB_FILE, "rb"))

async def statsreset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    await db_reset_all()
    await update.message.reply_text("All stats reset to zero.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    rows = await db_dump_all()
    top10 = rows[:10]
    text = "<b>Leaderboard — Top 10 (by validated words)</b>\n\n"
    for idx, r in enumerate(top10, start=1):
        text += f"{idx}. {escape_html(r[0])} — validated:{r[2]} lists:{r[3]}\n"
    await update.message.reply_text(text, parse_mode="HTML")

# ---------------- RUNINFO & VALIDATE ----------------
async def runinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owners can use this command.")
        return
    lines = []
    for chat_id, g in games.items():
        if g.get("state") in ("lobby", "running"):
            players = len(g.get("players", {}))
            mode = g.get("mode")
            round_no = g.get("round", 0)
            creator = escape_html(g.get("creator_name", "Unknown"))
            lines.append(f"• Chat: {chat_id}\n  Mode: {mode}\n  Round: {round_no}\n  Players: {players}\n  Creator: {creator}")
    if not lines:
        await update.message.reply_text("No active games currently.")
        return
    text = "<b>Active games:</b>\n\n" + "\n\n".join(lines)
    await update.message.reply_text(text, parse_mode="HTML")

async def validate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = games.get(chat.id)
    if not g:
        await update.message.reply_text("No active game.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("Only chat admins can trigger manual validation.")
            return
    except Exception:
        await update.message.reply_text("Only chat admins can trigger manual validation.")
        return
    await open_manual_validate(update, context)

# ---------------- APP SETUP & MAIN ----------------
def main():
    init_db()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(setup_db())
    except Exception as e:
        logger.exception("DB setup failed: %s", e)
        return

    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.strip() == "":
        print("Please set TELEGRAM_BOT_TOKEN in the script before running.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # register handlers
    app.add_handler(CommandHandler("runinfo", runinfo_command))
    app.add_handler(CommandHandler("classicadedonha", classic_lobby))
    app.add_handler(CommandHandler("customadedonha", custom_lobby))
    app.add_handler(CommandHandler("fastadedonha", fast_lobby))
    app.add_handler(CommandHandler(["joingame","join"], joingame_command))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(CommandHandler("gamecancel", gamecancel_command))
    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("dumpstats", dumpstats_command))
    app.add_handler(CommandHandler("statsreset", statsreset_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("validate", validate_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submission_handler))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
