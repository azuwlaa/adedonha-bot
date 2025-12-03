import os
import logging
import random
import asyncio
import sqlite3
import csv
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG ----------------
# You can override via environment variables (preferred)
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
OPENAI_API_KEY = "OPENAI_API_KEY"

# Owners (can run /dumpstats /statsreset /leaderboard)
OWNER_IDS = {624102836, 1707015091}

# Game constants
MAX_PLAYERS = 10
LOBBY_TIMEOUT_SECONDS = 5 * 60  # 5 minutes to auto-cancel lobby if only creator
CLASSIC_ROUNDS = 10
FAST_ROUNDS = 12
CLASSIC_NO_SUBMIT_TIMEOUT = 3 * 60  # 3 minutes
FAST_ROUND_TIME = 60  # 1 minute
POST_FIRST_SUBMIT_WINDOW_FAST = 2  # 2 seconds window for fast after first submit
POST_FIRST_SUBMIT_WINDOW_CLASSIC = 2  # 2 seconds as per your request
# scoring
POINTS_UNIQUE = 10
POINTS_SHARED = 5
# DB file
DB_FILE = "stats.db"

# Category pool (12)
CATEGORY_POOL = [
    "Name",
    "Object",
    "Animal",
    "Plant",
    "City/Country/State",
    "Food",
    "Color",
    "Movie/Series/TV Show",
    "Place",
    "Fruit",
    "Profession",
    "Adjective",
]

# classic default categories (if random not desired)
CLASSIC_DEFAULT_CATEGORIES = [
    "Name",
    "Object",
    "Plant",
    "Animal",
    "City/Country/State",
]

# Emojis
EMOJI_LOBBY_PLAYER = "🦩"
EMOJI_LOBBY = "🎯"
EMOJI_JOIN = "✅"
EMOJI_START = "▶️"
EMOJI_CANCEL = "❌"
EMOJI_INFO = "ℹ️"
EMOJI_SUCCESS = "✅"
EMOJI_WARN = "⚠️"
EMOJI_CLOCK = "⏱️"

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------- OpenAI client ----------------
if OPENAI_API_KEY:
    ai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    ai_client = None
    logger.warning("OPENAI_API_KEY not set — AI validation will be permissive unless /validate used.")

# ---------------- State ----------------
# games keyed by chat_id (only one game per group)
# structure:
# {
#   chat_id: {
#       "state": "lobby"|"running",
#       "mode": "classic"|"fast"|"custom",
#       "creator_id": int,
#       "players": {user_id: display_name},
#       "categories_per_round": int,
#       "category_pool": [...],
#       "lobby_message_id": int,
#       "lobby_task": asyncio.Task,
#       "pinned": bool,
#       "current_round": int,
#       "round_letter": str,
#       "round_categories": [...],
#       "submissions": {str(uid): text},
#       "round_task": asyncio.Task,
#       "no_submit_task": asyncio.Task,
#       "first_submit_task": asyncio.Task,
#       "validation_mode": "ai"|"manual_pending",
#       "manual_validation_message_id": int,
#       ...
#   }
# }
games: Dict[int, Dict] = {}

# ----------------- Database helpers -----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
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
    conn.close()

def db_ensure_user(uid: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM stats WHERE user_id = ?", (uid,))
    if not c.fetchone():
        c.execute("INSERT INTO stats (user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()

def db_update_after_round(uid: str, validated_words: int, submitted_any: bool):
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if submitted_any:
        c.execute("""
            UPDATE stats
            SET total_wordlists_sent = total_wordlists_sent + 1,
                total_validated_words = total_validated_words + ?
            WHERE user_id = ?
        """, (validated_words, uid))
    else:
        # still ensure user exists, nothing else to add
        pass
    conn.commit()
    conn.close()

def db_update_after_game(user_ids: List[str]):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for uid in user_ids:
        db_ensure_user(uid)
        c.execute("UPDATE stats SET games_played = games_played + 1 WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()

def db_get_stats(uid: str) -> Dict[str,int]:
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT games_played, total_validated_words, total_wordlists_sent FROM stats WHERE user_id = ?", (uid,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"games_played": row[0], "total_validated_words": row[1], "total_wordlists_sent": row[2]}
    return {"games_played": 0, "total_validated_words": 0, "total_wordlists_sent": 0}

def db_dump_all() -> List[Tuple]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, games_played, total_validated_words, total_wordlists_sent FROM stats")
    rows = c.fetchall()
    conn.close()
    return rows

def db_reset_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE stats SET games_played=0, total_validated_words=0, total_wordlists_sent=0")
    conn.commit()
    conn.close()

# ----------------- Utilities -----------------
def escape_md_v2(text: str) -> str:
    if not text:
        return ""
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return "".join("\\" + c if c in to_escape else c for c in text)

def user_mention_md(uid: int, name: str) -> str:
    return f"[{escape_md_v2(name)}](tg://user?id={uid})"

def choose_random_categories(pool: List[str], count: int) -> List[str]:
    if count >= len(pool):
        return pool.copy()
    return random.sample(pool, count)

def extract_answers_from_text(text: str, expected_count: int) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answers = []
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            answers.append(parts[1].strip())
        else:
            answers.append(line)
    # pad to expected_count
    while len(answers) < expected_count:
        answers.append("")
    return answers[:expected_count]

async def is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        mem = await context.bot.get_chat_member(chat_id, user_id)
        return mem.status in ("administrator", "creator")
    except Exception:
        return False

# ----------------- AI Validation -----------------
async def ai_validate_answer(category: str, answer: str, letter: str) -> bool:
    """Return True if AI says YES. If ai_client None or failure => raise exception to be handled by caller."""
    if not answer:
        return False
    if not ai_client:
        # indicate AI not available => raise
        raise RuntimeError("AI client not configured")
    prompt = f"""
You are a terse validator for the game Adedonha.
Rules:
- The answer must start with the letter '{letter}' (case-insensitive).
- It must match the category: "{category}".
Reply only "YES" or "NO".
Question: Does "{answer}" satisfy both conditions?
"""
    try:
        resp = ai_client.responses.create(model="gpt-4.1-mini", input=prompt, max_output_tokens=6)
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
        logger.exception("AI validation error: %s", e)
        raise

# ----------------- Lobby & Game Commands -----------------
async def classic_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create classic lobby: fixed classic categories per round."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    # ensure group-only
    if update.effective_chat.type == "private":
        await update.message.reply_text("This bot runs games only in groups.")
        return
    # check existing
    if chat_id in games and games[chat_id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active here. Use /gamecancel to cancel.")
        return
    # create lobby
    games[chat_id] = {
        "state": "lobby",
        "mode": "classic",
        "creator_id": user.id,
        "players": {user.id: user.first_name},
        "categories_per_round": len(CLASSIC_DEFAULT_CATEGORIES),
        "category_pool": CLASSIC_DEFAULT_CATEGORIES.copy(),
        "lobby_message_id": None,
        "lobby_task": None,
        "pinned": False,
        "current_round": 0,
        "submissions": {},
        "validation_mode": "ai",  # ai by default
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI_JOIN} Join", callback_data="join_lobby" )],
                               [InlineKeyboardButton(f"{EMOJI_START} Start", callback_data="start_game"),
                                InlineKeyboardButton(f"{EMOJI_INFO} Mode Info", callback_data="mode_info")]])
    players_md = "\n".join(f"{EMOJI_LOBBY_PLAYER} {user_mention_md(uid, name)}" for uid, name in games[chat_id]["players"].items())
    text = (f"{EMOJI_LOBBY} *Adedonha Lobby (Classic)*\n\n"
            f"Created by: {user_mention_md(user.id, user.first_name)}\n\n"
            f"*Mode:* Classic (fixed categories)\n"
            f"*Categories per round:* {games[chat_id]['categories_per_round']}\n\n"
            f"*Players:*\n{players_md}\n\n"
            f"Press Start to begin the game.")
    msg = await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    games[chat_id]["lobby_message_id"] = msg.message_id
    # pin
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id)
        games[chat_id]["pinned"] = True
    except Exception:
        pass
    # auto-cancel if nobody joins in 5 minutes (only creator)
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
        g = games.get(chat_id)
        if not g or g.get("state") != "lobby":
            return
        if len(g.get("players", {})) <= 1:
            try:
                await context.bot.send_message(chat_id, f"{EMOJI_WARN} Lobby cancelled due to inactivity.")
            except Exception:
                pass
            # unpin
            try:
                if g.get("pinned"):
                    await context.bot.unpin_chat_message(chat_id)
            except Exception:
                pass
            games.pop(chat_id, None)
    task = asyncio.create_task(lobby_timeout())
    games[chat_id]["lobby_task"] = task

async def fast_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create fast lobby: default 3 categories; optionally provided categories."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    if update.effective_chat.type == "private":
        await update.message.reply_text("This bot runs games only in groups.")
        return
    if chat_id in games and games[chat_id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active here. Use /gamecancel to cancel.")
        return
    # parse optional categories from command args
    args = context.args or []
    pool = CATEGORY_POOL.copy()
    if args:
        # user provided categories (space-separated) -> use those 3
        provided = [a.strip() for a in args if a.strip()]
        if len(provided) >= 3:
            pool = provided[:3]
        else:
            # if not enough provided, fallback to pool but inform
            pool = random.sample(CATEGORY_POOL, 3)
    else:
        pool = random.sample(CATEGORY_POOL, 3)
    games[chat_id] = {
        "state": "lobby",
        "mode": "fast",
        "creator_id": user.id,
        "players": {user.id: user.first_name},
        "categories_per_round": 3,
        "category_pool": pool,
        "lobby_message_id": None,
        "lobby_task": None,
        "pinned": False,
        "current_round": 0,
        "submissions": {},
        "validation_mode": "ai",
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI_JOIN} Join", callback_data="join_lobby")],
                               [InlineKeyboardButton(f"{EMOJI_START} Start", callback_data="start_game"),
                                InlineKeyboardButton(f"{EMOJI_INFO} Mode Info", callback_data="mode_info")]])
    players_md = "\n".join(f"{EMOJI_LOBBY_PLAYER} {user_mention_md(uid, name)}" for uid, name in games[chat_id]["players"].items())
    pool_md = "\n".join(f"{i+1}. {escape_md_v2(c)}" for i, c in enumerate(pool))
    text = (f"{EMOJI_LOBBY} *Adedonha Lobby (Fast)*\n\n"
            f"Created by: {user_mention_md(user.id, user.first_name)}\n\n"
            f"*Mode:* Fast\n"
            f"*Categories per round:* 3\n"
            f"*Category pool for this game:*\n{pool_md}\n\n"
            f"*Players:*\n{players_md}\n\n"
            f"Press Start to begin the game.")
    msg = await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    games[chat_id]["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id)
        games[chat_id]["pinned"] = True
    except Exception:
        pass
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
        g = games.get(chat_id)
        if not g or g.get("state") != "lobby":
            return
        if len(g.get("players", {})) <= 1:
            try:
                await context.bot.send_message(chat_id, f"{EMOJI_WARN} Lobby cancelled due to inactivity.")
            except Exception:
                pass
            try:
                if g.get("pinned"):
                    await context.bot.unpin_chat_message(chat_id)
            except Exception:
                pass
            games.pop(chat_id, None)
    games[chat_id]["lobby_task"] = asyncio.create_task(lobby_timeout())

async def custom_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
    /customadedonha <count> <cat1|cat2|cat3|...>
    Example:
    /customadedonha 5 Name|Food|Color|Object|Animal
    count = categories per round (3..8)
    categories separated by | (pipe). Up to 12 unique categories allowed.
    """
    chat_id = update.effective_chat.id
    user = update.effective_user
    if update.effective_chat.type == "private":
        await update.message.reply_text("This bot runs games only in groups.")
        return
    if chat_id in games and games[chat_id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active here. Use /gamecancel to cancel.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /customadedonha <count> <cat1|cat2|...>\nExample: /customadedonha 5 Name|Food|Color|Object|Animal")
        return
    try:
        count = int(args[0])
        if count < 3 or count > 8:
            raise ValueError()
    except Exception:
        await update.message.reply_text("First argument must be a number between 3 and 8 (categories per round).")
        return
    # rest combined
    rest = " ".join(args[1:])
    provided = [c.strip() for c in rest.split("|") if c.strip()]
    if not provided:
        await update.message.reply_text("Provide categories separated by |. Example: Name|Food|Color")
        return
    if len(provided) > 12:
        provided = provided[:12]
    pool = list(dict.fromkeys(provided))  # unique preserve order
    games[chat_id] = {
        "state": "lobby",
        "mode": "custom",
        "creator_id": user.id,
        "players": {user.id: user.first_name},
        "categories_per_round": count,
        "category_pool": pool,
        "lobby_message_id": None,
        "lobby_task": None,
        "pinned": False,
        "current_round": 0,
        "submissions": {},
        "validation_mode": "ai",
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI_JOIN} Join", callback_data="join_lobby")],
                               [InlineKeyboardButton(f"{EMOJI_START} Start", callback_data="start_game"),
                                InlineKeyboardButton(f"{EMOJI_INFO} Mode Info", callback_data="mode_info")]])
    players_md = "\n".join(f"{EMOJI_LOBBY_PLAYER} {user_mention_md(uid, name)}" for uid, name in games[chat_id]["players"].items())
    pool_md = "\n".join(f"{i+1}. {escape_md_v2(c)}" for i, c in enumerate(pool))
    text = (f"{EMOJI_LOBBY} *Adedonha Lobby (Custom)*\n\n"
            f"Created by: {user_mention_md(user.id, user.first_name)}\n\n"
            f"*Mode:* Custom\n"
            f"*Categories per round:* {count}\n"
            f"*Category pool:*\n{pool_md}\n\n"
            f"*Players:*\n{players_md}\n\n"
            f"Press Start to begin the game.")
    msg = await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    games[chat_id]["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id)
        games[chat_id]["pinned"] = True
    except Exception:
        pass
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT_SECONDS)
        g = games.get(chat_id)
        if not g or g.get("state") != "lobby":
            return
        if len(g.get("players", {})) <= 1:
            try:
                await context.bot.send_message(chat_id, f"{EMOJI_WARN} Lobby cancelled due to inactivity.")
            except Exception:
                pass
            try:
                if g.get("pinned"):
                    await context.bot.unpin_chat_message(chat_id)
            except Exception:
                pass
            games.pop(chat_id, None)
    games[chat_id]["lobby_task"] = asyncio.create_task(lobby_timeout())

# ----------------- Join handlers -----------------
async def join_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles join button press."""
    cq = update.callback_query
    await cq.answer()
    chat_id = cq.message.chat.id
    user = cq.from_user
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await cq.answer("No active lobby.")
        return
    if user.id in g["players"]:
        await cq.answer("You already joined.")
        return
    if len(g["players"]) >= MAX_PLAYERS:
        await cq.answer("Lobby full.")
        return
    g["players"][user.id] = user.first_name
    # update lobby message
    players_md = "\n".join(f"{EMOJI_LOBBY_PLAYER} {user_mention_md(uid, name)}" for uid, name in g["players"].items())
    text_header = f"{EMOJI_LOBBY} *Adedonha Lobby ({g['mode'].capitalize()})*"
    text = (f"{text_header}\n\nCreated by: {user_mention_md(g['creator_id'], g['players'][g['creator_id']])}\n\n"
            f"*Mode:* {g['mode'].capitalize()}\n"
            f"*Categories per round:* {g['categories_per_round']}\n\n"
            f"*Players:*\n{players_md}\n\n"
            f"Press Start to begin the game.")
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="MarkdownV2")
    except Exception:
        pass
    await cq.answer("Joined lobby!")

async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alternative join via command /joingame or /join"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await update.message.reply_text("No active lobby to join.")
        return
    if user.id in g["players"]:
        # optionally delete the user's join message to avoid chat clutter
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    if len(g["players"]) >= MAX_PLAYERS:
        await update.message.reply_text("Lobby is full.")
        return
    g["players"][user.id] = user.first_name
    # update lobby message
    players_md = "\n".join(f"{EMOJI_LOBBY_PLAYER} {user_mention_md(uid, name)}" for uid, name in g["players"].items())
    text_header = f"{EMOJI_LOBBY} *Adedonha Lobby ({g['mode'].capitalize()})*"
    text = (f"{text_header}\n\nCreated by: {user_mention_md(g['creator_id'], g['players'][g['creator_id']])}\n\n"
            f"*Mode:* {g['mode'].capitalize()}\n"
            f"*Categories per round:* {g['categories_per_round']}\n\n"
            f"*Players:*\n{players_md}\n\n"
            f"Press Start to begin the game.")
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="MarkdownV2")
    except Exception:
        pass
    # delete join command message to avoid spam
    try:
        await update.message.delete()
    except Exception:
        pass

# ----------------- Lobby callback: start / modeinfo -----------------
async def lobby_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    data = cq.data
    chat_id = cq.message.chat.id
    user = cq.from_user
    g = games.get(chat_id)
    if not g:
        await cq.answer("No active lobby.")
        return
    if data == "start_game":
        # start only if creator or admin
        if user.id != g["creator_id"] and not await is_chat_admin(context, chat_id, user.id):
            await cq.answer("Only the lobby creator or a chat admin can start the game.")
            return
        if len(g["players"]) < 2:
            await cq.answer("Need at least 2 players to start.")
            return
        await cq.answer("Starting game...")
        # unpin lobby
        try:
            if g.get("pinned"):
                await context.bot.unpin_chat_message(chat_id)
        except Exception:
            pass
        await start_game(chat_id, context)
        return
    if data == "mode_info":
        # send mode info in an alert or chat message
        mode = g["mode"]
        if mode == "classic":
            info = (f"{EMOJI_INFO} *Classic Mode*\n\nClassic Adedonha uses the classic categories each round:\n"
                    f"{', '.join(CLASSIC_DEFAULT_CATEGORIES)}\n\n"
                    f"Round timing: If no one submits in {CLASSIC_NO_SUBMIT_TIMEOUT//60} minutes, round ends with no penalties. "
                    f"After first submission others have {POST_FIRST_SUBMIT_WINDOW_CLASSIC} seconds to submit. "
                    f"Total rounds: {CLASSIC_ROUNDS}.")
        elif mode == "fast":
            info = (f"{EMOJI_INFO} *Fast Mode*\n\nFast Adedonha uses 3 categories per round (pool shown in lobby). "
                    f"Each round lasts {FAST_ROUND_TIME} seconds. Submissions accepted up to 1 minute. "
                    f"After first submission others have {POST_FIRST_SUBMIT_WINDOW_FAST} seconds. "
                    f"Total rounds: {FAST_ROUNDS}.")
        else:
            info = (f"{EMOJI_INFO} *Custom Mode*\n\nCustom Adedonha uses a custom category pool defined by the creator. "
                    f"Categories per round: {g['categories_per_round']}. "
                    f"Each round selects randomly from the provided pool. Round timing depends on mode (classic-like timing).")
        # show as alert
        try:
            await cq.answer(info, show_alert=True)
        except Exception:
            await context.bot.send_message(chat_id, info, parse_mode="MarkdownV2")
        return

# ----------------- Cancel game command -----------------
async def gamecancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    g = games.get(chat_id)
    if not g:
        await update.message.reply_text("No active game or lobby to cancel.")
        return
    # only creator or admin or owner
    if user.id != g["creator_id"] and not await is_chat_admin(context, chat_id, user.id) and user.id not in OWNER_IDS:
        await update.message.reply_text("You are not allowed to cancel this game.")
        return
    # cancel tasks, unpin, remove
    if g.get("lobby_task"):
        g["lobby_task"].cancel()
    if g.get("round_task"):
        g["round_task"].cancel()
    if g.get("no_submit_task"):
        g["no_submit_task"].cancel()
    if g.get("first_submit_task"):
        g["first_submit_task"].cancel()
    try:
        if g.get("pinned"):
            await context.bot.unpin_chat_message(chat_id)
    except Exception:
        pass
    games.pop(chat_id, None)
    await update.message.reply_text(f"{EMOJI_CANCEL} Game/lobby cancelled.")

# ----------------- Start game flow -----------------
async def start_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return
    g["state"] = "running"
    # update stats: games played
    db_update_after_game([str(uid) for uid in g["players"].keys()])
    # rounds
    rounds = CLASSIC_ROUNDS if g["mode"] == "classic" else (FAST_ROUNDS if g["mode"] == "fast" else CLASSIC_ROUNDS)
    for r in range(1, rounds + 1):
        # ensure still running
        if chat_id not in games:
            return
        g = games.get(chat_id)
        if not g or g.get("state") != "running":
            return
        g["current_round"] = r
        # pick categories for this round
        pool = g["category_pool"]
        count = g["categories_per_round"]
        round_cats = choose_random_categories(pool, count)
        g["round_categories"] = round_cats
        g["round_letter"] = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        g["submissions"] = {}
        # announce round
        cat_lines = "\n".join([f"{i+1}. {escape_md_v2(c)}:" for i, c in enumerate(round_cats)])
        if g["mode"] == "fast":
            text = (f"{EMOJI_CLOCK} *Round {r}/{rounds}* — Fast Mode\n"
                    f"Letter: *{escape_md_v2(g['round_letter'])}*\n\n"
                    f"Send your answers in ONE MESSAGE (copy/paste template):\n\n"
                    f"```\n{cat_lines}\n```\n\n"
                    f"You have {FAST_ROUND_TIME} seconds for this round. After first submission others have {POST_FIRST_SUBMIT_WINDOW_FAST} seconds.")
            await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
            # schedule no-submission end after FAST_ROUND_TIME
            if g.get("no_submit_task"):
                g["no_submit_task"].cancel()
            g["no_submit_task"] = asyncio.create_task(fast_round_timeout(chat_id, context, r))
        else:
            text = (f"{EMOJI_CLOCK} *Round {r}/{rounds}* — Classic/Custom\n"
                    f"Letter: *{escape_md_v2(g['round_letter'])}*\n\n"
                    f"Send your answers in ONE MESSAGE (copy/paste template):\n\n"
                    f"```\n{cat_lines}\n```\n\n"
                    f"If nobody submits in {CLASSIC_NO_SUBMIT_TIMEOUT//60} minutes this round ends with no penalties. "
                    f"After first submission others have {POST_FIRST_SUBMIT_WINDOW_CLASSIC} seconds.")
            await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
            if g.get("no_submit_task"):
                g["no_submit_task"].cancel()
            g["no_submit_task"] = asyncio.create_task(classic_no_submit_timeout(chat_id, context, r))
        # wait for this round's scoring to be appended; a simple wait loop: we'll rely on score_round adding state "round_finished"
        while True:
            await asyncio.sleep(0.5)
            g_local = games.get(chat_id)
            if not g_local:
                return
            if g_local.get("round_finished") == r:
                # clear flag
                g_local["round_finished"] = None
                break
        # short pause between rounds
        await asyncio.sleep(1)
    # game finished: send final leaderboard
    g = games.get(chat_id)
    if not g:
        return
    leaderboard = sorted([(str(uid), pts) for uid, pts in g.get("scores", {}).items()], key=lambda x: -x[1])
    text = f"{EMOJI_SUCCESS} *Game Over — Final Scores*\n\n"
    for uid_str, pts in leaderboard:
        uid_int = int(uid_str)
        name = g["players"].get(uid_int, "Player")
        text += f"{user_mention_md(uid_int, name)} — `{pts}` pts\n"
    await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
    # cleanup
    try:
        if g.get("pinned"):
            await context.bot.unpin_chat_message(chat_id)
    except Exception:
        pass
    games.pop(chat_id, None)

# ----------------- Round timeouts/handlers -----------------
async def fast_round_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int):
    await asyncio.sleep(FAST_ROUND_TIME)
    # at the end of 60s, score whatever submissions exist
    g = games.get(chat_id)
    if not g or g.get("current_round") != round_num:
        return
    # proceed to scoring (no penalties for non-submit in fast mode)
    await score_round(chat_id, context, first_submitter_id=None)

async def classic_no_submit_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE, round_num: int):
    await asyncio.sleep(CLASSIC_NO_SUBMIT_TIMEOUT)
    g = games.get(chat_id)
    if not g or g.get("current_round") != round_num:
        return
    # if no submissions at all -> end round with no penalties
    if not g.get("submissions"):
        await context.bot.send_message(chat_id, f"{EMOJI_CLOCK} Round {round_num} ended: no submissions. No penalties.")
        # mark round finished
        g["round_finished"] = round_num
        # no change to scores
        return

# ----------------- Submission handler -----------------
async def submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    g = games.get(chat_id)
    if not g or g.get("state") != "running":
        return
    # only players in this game can submit
    if user.id not in g["players"]:
        return
    text = update.message.text or ""
    if text.startswith("/"):
        return
    uid_str = str(user.id)
    if uid_str in g.get("submissions", {}):
        await update.message.reply_text("You already submitted for this round.")
        return
    # store
    g["submissions"][uid_str] = text
    # if first submission
    if len(g["submissions"]) == 1:
        # cancel no-submit task (if classic)
        if g.get("no_submit_task"):
            try:
                g["no_submit_task"].cancel()
            except Exception:
                pass
            g["no_submit_task"] = None
        # announce and schedule short window (2s)
        post_window = POST_FIRST_SUBMIT_WINDOW_FAST if g["mode"] == "fast" else POST_FIRST_SUBMIT_WINDOW_CLASSIC
        await context.bot.send_message(chat_id, f"{EMOJI_CLOCK} {user_mention_md(user.id, user.first_name)} submitted first! Others have {post_window} seconds.", parse_mode="MarkdownV2")
        # schedule window end (for classic we still accept late submissions; for fast, window just minimal)
        async def end_after_window():
            await asyncio.sleep(post_window)
            # proceed scoring (first_submitter_id is the first submitter user.id)
            first_id = int(list(g["submissions"].keys())[0])
            await score_round(chat_id, context, first_submitter_id=first_id)
        g["first_submit_task"] = asyncio.create_task(end_after_window())
    else:
        # for fast mode we still allow until the full minute ends; for classic we accept until the post-window triggers scoring
        pass
    # delete player's message to avoid clutter if desired — optional
    try:
        await update.message.delete()
    except Exception:
        pass

# ----------------- Scoring -----------------
async def score_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE, first_submitter_id: Optional[int]):
    g = games.get(chat_id)
    if not g:
        return
    round_num = g["current_round"]
    categories = g["round_categories"]
    letter = g["round_letter"]
    submissions = g.get("submissions", {})  # uid_str -> raw text
    # cancel first_submit_task if any
    if g.get("first_submit_task"):
        try:
            g["first_submit_task"].cancel()
        except Exception:
            pass
        g["first_submit_task"] = None
    # parse each submission (only first N answers)
    parsed = {}
    for uid_str, txt in submissions.items():
        parsed[uid_str] = extract_answers_from_text(txt, len(categories))
    # prepare per-category counts for uniqueness
    per_cat_counts = [dict() for _ in categories]
    for idx in range(len(categories)):
        for uid_str, answers in parsed.items():
            a = answers[idx].strip()
            if a:
                k = a.lower()
                per_cat_counts[idx][k] = per_cat_counts[idx].get(k, 0) + 1
    # validate answers (AI) or fallback to manual if AI fails
    ai_available = True
    # attempt validating first answer to see if AI raises
    try:
        if ai_client:
            pass
        else:
            raise RuntimeError("AI not configured")
    except Exception:
        ai_available = False
    # if ai_available, validate each answer; on first exception, switch to manual_pending
    manual_required = False
    validation_results: Dict[str, List[bool]] = {}
    if ai_available:
        try:
            for uid_str, answers in parsed.items():
                res_list = []
                for idx, a in enumerate(answers):
                    a_clean = a.strip()
                    if not a_clean or not a_clean[0].isalpha() or a_clean[0].upper() != letter.upper():
                        res_list.append(False)
                        continue
                    try:
                        ok = await ai_validate_answer(categories[idx], a_clean, letter)
                        res_list.append(ok)
                    except Exception:
                        manual_required = True
                        break
                validation_results[uid_str] = res_list
                if manual_required:
                    break
        except Exception:
            manual_required = True
    else:
        manual_required = True
    # If manual_required -> set validation_mode to manual_pending and send one interactive admin message for validation
    if manual_required:
        g["validation_mode"] = "manual_pending"
        # build single validation message listing all submissions (compact)
        lines = []
        for uid_str, answers in parsed.items():
            uid_int = int(uid_str)
            name = g["players"].get(uid_int, "Player")
            line = f"{user_mention_md(uid_int, name)}:\n"
            for idx, a in enumerate(answers):
                line += f" {idx+1}. {escape_md_v2(categories[idx])}: {escape_md_v2(a or '')}\n"
            lines.append(line)
        combined = "\n\n".join(lines) or "No submissions"
        # create inline buttons: Approve_<uid>, Reject_<uid>, ApproveAll, RejectAll
        kb_buttons = []
        row = [InlineKeyboardButton("Approve All ✅", callback_data=f"manual_approve_all_{chat_id}_{round_num}"),
               InlineKeyboardButton("Reject All ❌", callback_data=f"manual_reject_all_{chat_id}_{round_num}")]
        kb_buttons.append(row)
        for uid_str in parsed.keys():
            uid_int = int(uid_str)
            kb_buttons.append([InlineKeyboardButton(f"Approve {g['players'].get(uid_int,'Player')}", callback_data=f"manual_approve_{chat_id}_{round_num}_{uid_str}"),
                               InlineKeyboardButton(f"Reject {g['players'].get(uid_int,'Player')}", callback_data=f"manual_reject_{chat_id}_{round_num}_{uid_str}")])
        kb = InlineKeyboardMarkup(kb_buttons)
        msg = await context.bot.send_message(chat_id, f"{EMOJI_WARN} AI validation unavailable — awaiting admin validation (use buttons)\n\n{combined}", parse_mode="MarkdownV2", reply_markup=kb)
        g["manual_validation_message_id"] = msg.message_id
        return
    # else compute scores using validation_results
    round_scores = {}
    for uid_str, answers in parsed.items():
        valid_count = 0
        pts = 0
        res_list = validation_results.get(uid_str, [False]*len(answers))
        for idx, ok in enumerate(res_list):
            if not ok:
                continue
            valid_count += 1
            key = answers[idx].strip().lower()
            if per_cat_counts[idx].get(key, 0) == 1:
                pts += POINTS_UNIQUE
            else:
                pts += POINTS_SHARED
        round_scores[uid_str] = {"points": pts, "validated_words": valid_count, "submitted_any": True}
        # update game totals
        if "scores" not in g:
            g["scores"] = {}
        g["scores"][uid_str] = g["scores"].get(uid_str, 0) + pts
        # update DB stats
        db_update_after_round(uid_str, valid_count, submitted_any=True)
    # players who didn't submit: no penalty in any mode per your request
    for uid in list(g["players"].keys()):
        uid_str = str(uid)
        if uid_str not in round_scores:
            round_scores[uid_str] = {"points": 0, "validated_words": 0, "submitted_any": False}
            # update DB stats (no submission)
            db_update_after_round(uid_str, 0, submitted_any=False)
    # store round results in game state for potential debug
    if "round_history" not in g:
        g["round_history"] = []
    g["round_history"].append(round_scores)
    # send summary message
    header = f"{EMOJI_SUCCESS} *Round {round_num} Results*\nLetter: *{escape_md_v2(letter)}*\n\n"
    body = ""
    # list players sorted by points for message clarity
    sorted_players = sorted(round_scores.items(), key=lambda x: -x[1]["points"])
    for uid_str, info in sorted_players:
        uid_int = int(uid_str)
        name = g["players"].get(uid_int, "Player")
        body += f"{user_mention_md(uid_int, name)} — `{info['points']}` pts (valid: {info['validated_words']})\n"
    await context.bot.send_message(chat_id, header + body, parse_mode="MarkdownV2")
    # mark round finished
    g["round_finished"] = round_num
    return

# ----------------- Manual validation callbacks -----------------
async def manual_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    data = cq.data
    # parse patterns:
    # manual_approve_all_<chat>_<round>
    # manual_reject_all_<chat>_<round>
    # manual_approve_<chat>_<round>_<uid>
    # manual_reject_<chat>_<round>_<uid>
    tokens = data.split("_")
    if len(tokens) < 4:
        await cq.answer("Invalid action")
        return
    action = tokens[1]  # approve or reject or approveall/rejectall
    chat_id = int(tokens[3])
    g = games.get(chat_id)
    if not g:
        await cq.answer("No active game")
        return
    # check permission: only chat admins can validate
    user = cq.from_user
    if user.id not in OWNER_IDS and not await is_chat_admin(context, chat_id, user.id):
        await cq.answer("Only chat admins or bot owners can validate.", show_alert=True)
        return
    round_num = int(tokens[4]) if len(tokens) > 4 and tokens[4].isdigit() else g.get("current_round")
    # implement approve/reject logic
    if action == "approve" and len(tokens) == 5:
        # manual_approve_<chat>_<round>_<uid>
        uid_str = tokens[5]
        # mark that player's each answer validated True for scoring
        # naive approach: we will compute scoring here by re-parsing the submissions and treating approved as True, others as False
        await cq.answer("Player approved")
    if action == "approve" and len(tokens) == 4:
        await cq.answer("Approve action acknowledged.")
    # For simplicity and safety: when any manual approve/reject button pressed, we'll compute the final validation map based on which user buttons have 'approved' and then score.
    # TODO: implement stateful tracking of approvals. For now: accept ApproveAll / RejectAll
    if data.startswith("manual_approve_all_"):
        # treat all submitted answers as valid
        # Build validation_results where every existing answer that matches the starting letter is marked valid
        chat_id = int(tokens[3])
        g = games.get(chat_id)
        if not g:
            await cq.answer("Game gone")
            return
        submissions = g.get("submissions", {})
        parsed = {uid: extract_answers_from_text(txt, len(g["round_categories"])) for uid, txt in submissions.items()}
        validation_results = {}
        for uid_str, answers in parsed.items():
            res_list = []
            for idx, a in enumerate(answers):
                a_clean = a.strip()
                if not a_clean or not a_clean[0].isalpha() or a_clean[0].upper() != g["round_letter"].upper():
                    res_list.append(False)
                else:
                    res_list.append(True)
            validation_results[uid_str] = res_list
        # compute scores using these validation_results
        await finalize_manual_scores(chat_id, context, validation_results)
        await cq.edit_message_reply_markup(reply_markup=None)
        await cq.answer("All approved — scoring in progress.")
        return
    if data.startswith("manual_reject_all_"):
        # treat all answers as invalid
        chat_id = int(tokens[3])
        # validation_results all False
        validation_results = {}
        g = games.get(chat_id)
        if not g:
            await cq.answer("Game gone")
            return
        submissions = g.get("submissions", {})
        for uid_str in submissions.keys():
            validation_results[uid_str] = [False]*len(g["round_categories"])
        await finalize_manual_scores(chat_id, context, validation_results)
        await cq.edit_message_reply_markup(reply_markup=None)
        await cq.answer("All rejected — scoring in progress.")
        return
    # per-player buttons (approve/reject)
    if data.startswith("manual_approve_") and len(tokens) >= 6:
        chat_id = int(tokens[3])
        uid_str = tokens[5]
        # store approval in a map on game state
        g = games.get(chat_id)
        if not g:
            await cq.answer("Game gone")
            return
        if "manual_approvals" not in g:
            g["manual_approvals"] = {}
        g["manual_approvals"][uid_str] = True
        await cq.answer("Player approved")
        # check if all players have been decided; if yes, finalize
        if all(uid in g.get("manual_approvals", {}) for uid in g.get("submissions", {}).keys()):
            await finalize_manual_scores_from_state(chat_id, context)
            await cq.edit_message_reply_markup(reply_markup=None)
        return
    if data.startswith("manual_reject_") and len(tokens) >= 6:
        chat_id = int(tokens[3])
        uid_str = tokens[5]
        g = games.get(chat_id)
        if not g:
            await cq.answer("Game gone")
            return
        if "manual_approvals" not in g:
            g["manual_approvals"] = {}
        g["manual_approvals"][uid_str] = False
        await cq.answer("Player rejected")
        if all(uid in g.get("manual_approvals", {}) for uid in g.get("submissions", {}).keys()):
            await finalize_manual_scores_from_state(chat_id, context)
            await cq.edit_message_reply_markup(reply_markup=None)
        return
    await cq.answer()

async def finalize_manual_scores(chat_id: int, context: ContextTypes.DEFAULT_TYPE, validation_results: Dict[str, List[bool]]):
    """Compute and send scores given a validation_results map for the current round."""
    g = games.get(chat_id)
    if not g:
        return
    # use same scoring logic as in score_round but with supplied validation_results
    categories = g["round_categories"]
    submissions = g.get("submissions", {})
    parsed = {uid: extract_answers_from_text(txt, len(categories)) for uid, txt in submissions.items()}
    per_cat_counts = [dict() for _ in categories]
    for idx in range(len(categories)):
        for uid_str, answers in parsed.items():
            a = answers[idx].strip()
            if a and validation_results.get(uid_str, [False]*len(categories))[idx]:
                k = a.lower()
                per_cat_counts[idx][k] = per_cat_counts[idx].get(k, 0) + 1
    round_scores = {}
    for uid_str, answers in parsed.items():
        res_list = validation_results.get(uid_str, [False]*len(categories))
        valid_count = 0
        pts = 0
        for idx, ok in enumerate(res_list):
            if not ok:
                continue
            valid_count += 1
            key = answers[idx].strip().lower()
            if per_cat_counts[idx].get(key, 0) == 1:
                pts += POINTS_UNIQUE
            else:
                pts += POINTS_SHARED
        round_scores[uid_str] = {"points": pts, "validated_words": valid_count, "submitted_any": True}
        if "scores" not in g:
            g["scores"] = {}
        g["scores"][uid_str] = g["scores"].get(uid_str, 0) + pts
        db_update_after_round(uid_str, valid_count, submitted_any=True)
    # players without submission: no penalty
    for uid in list(g["players"].keys()):
        uid_str = str(uid)
        if uid_str not in round_scores:
            round_scores[uid_str] = {"points": 0, "validated_words": 0, "submitted_any": False}
            db_update_after_round(uid_str, 0, submitted_any=False)
    # store history and send summary
    if "round_history" not in g:
        g["round_history"] = []
    g["round_history"].append(round_scores)
    header = f"{EMOJI_SUCCESS} *Manual Validation Results — Round {g['current_round']}*\n\n"
    body = ""
    for uid_str, info in sorted(round_scores.items(), key=lambda x: -x[1]['points']):
        uid_int = int(uid_str)
        name = g["players"].get(uid_int, "Player")
        body += f"{user_mention_md(uid_int, name)} — `{info['points']}` pts (valid: {info['validated_words']})\n"
    await context.bot.send_message(chat_id, header + body, parse_mode="MarkdownV2")
    g["round_finished"] = g["current_round"]

async def finalize_manual_scores_from_state(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return
    approvals = g.get("manual_approvals", {})
    # build validation_results: True for each player's answers if approvals[uid] True else False
    submissions = g.get("submissions", {})
    categories = g["round_categories"]
    validation_results = {}
    for uid_str in submissions.keys():
        approved = approvals.get(uid_str, False)
        # If approved True then each answer that matches starting letter is treated valid, else all false
        parsed = extract_answers_from_text(submissions[uid_str], len(categories))
        res = []
        for idx, a in enumerate(parsed):
            if approved and a and a[0].upper() == g["round_letter"].upper():
                res.append(True)
            else:
                res.append(False)
        validation_results[uid_str] = res
    # finalize
    await finalize_manual_scores(chat_id, context, validation_results)
    # clear manual approvals
    g["manual_approvals"] = {}

# ----------------- Admin owner-only tools -----------------
async def dumpstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OWNER_IDS:
        await update.message.reply_text("You are not allowed to use this command.")
        return
    rows = db_dump_all()
    # formatted text
    header = "user_id | games_played | total_validated_words | total_wordlists_sent\n"
    body = header
    for r in rows:
        body += f"{r[0]} | {r[1]} | {r[2]} | {r[3]}\n"
    await update.message.reply_text("📊 Stats (text):\n" + "```\n" + body + "\n```", parse_mode="MarkdownV2")
    # csv
    csv_path = "stats_export.csv"
    with open(csv_path, "w", newline="", encoding="utf8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "games_played", "total_validated_words", "total_wordlists_sent"])
        for r in rows:
            writer.writerow(r)
    await update.message.reply_document(open(csv_path, "rb"), filename="stats_export.csv")
    # send db file
    await update.message.reply_document(open(DB_FILE, "rb"), filename=DB_FILE)

async def statsreset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OWNER_IDS:
        await update.message.reply_text("You are not allowed to use this command.")
        return
    db_reset_all()
    await update.message.reply_text("All stats have been reset to zero.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OWNER_IDS:
        await update.message.reply_text("You are not allowed to use this command.")
        return
    rows = db_dump_all()
    # sort by total_validated_words desc for leaderboard
    rows_sorted = sorted(rows, key=lambda r: -r[2])[:10]
    text = "🏆 Top 10 — by validated words\n\n"
    for r in rows_sorted:
        text += f"{r[0]} — games: {r[1]}, validated words: {r[2]}, lists: {r[3]}\n"
    await update.message.reply_text(text)

# ----------------- /mystats -----------------
async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
    else:
        u = update.effective_user
    s = db_get_stats(str(u.id))
    text = f"*Stats of {user_mention_md(u.id, u.first_name)}*\n\n"
    text += f"• *Games played:* `{s['games_played']}`\n"
    text += f"• *Total validated words:* `{s['total_validated_words']}`\n"
    text += f"• *Total word lists sent:* `{s['total_wordlists_sent']}`\n"
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ----------------- categories command -----------------
async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "*All possible categories (12):*\n\n"
    for i, c in enumerate(CATEGORY_POOL, start=1):
        text += f"{i}. {escape_md_v2(c)}\n"
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ----------------- validate command (manual trigger if AI fails) -----------------
async def validate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admins may trigger manual validation (single message with buttons) when AI fails."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    g = games.get(chat_id)
    if not g:
        await update.message.reply_text("No active game here.")
        return
    if user.id not in OWNER_IDS and not await is_chat_admin(context, chat_id, user.id):
        await update.message.reply_text("Only chat admins or bot owners can trigger manual validation.")
        return
    # create a single manual validation message using current submissions
    submissions = g.get("submissions", {})
    if not submissions:
        await update.message.reply_text("No submissions to validate.")
        return
    parsed = {uid: extract_answers_from_text(txt, len(g["round_categories"])) for uid, txt in submissions.items()}
    combined = ""
    for uid_str, answers in parsed.items():
        uid_int = int(uid_str)
        combined += f"{user_mention_md(uid_int, g['players'].get(uid_int,'Player'))}:\n"
        for idx, a in enumerate(answers):
            combined += f" {idx+1}. {escape_md_v2(g['round_categories'][idx])}: {escape_md_v2(a or '')}\n"
        combined += "\n"
    kb_buttons = []
    kb_buttons.append([InlineKeyboardButton("Approve All ✅", callback_data=f"manual_approve_all_{chat_id}_{g['current_round']}"),
                       InlineKeyboardButton("Reject All ❌", callback_data=f"manual_reject_all_{chat_id}_{g['current_round']}")])
    for uid_str in submissions.keys():
        uid_int = int(uid_str)
        kb_buttons.append([InlineKeyboardButton(f"Approve {g['players'].get(uid_int,'Player')}", callback_data=f"manual_approve_{chat_id}_{g['current_round']}_{uid_str}"),
                           InlineKeyboardButton(f"Reject {g['players'].get(uid_int,'Player')}", callback_data=f"manual_reject_{chat_id}_{g['current_round']}_{uid_str}")])
    kb = InlineKeyboardMarkup(kb_buttons)
    msg = await update.message.reply_text(f"{EMOJI_WARN} Manual validation requested by admin\n\n{combined}", parse_mode="MarkdownV2", reply_markup=kb)
    g["manual_validation_message_id"] = msg.message_id

# ----------------- Bot startup -----------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set; set it as env var or in file.")
        return
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # lobbies
    app.add_handler(CommandHandler("classicadedonha", classic_lobby))
    app.add_handler(CommandHandler("fastadedonha", fast_lobby))
    app.add_handler(CommandHandler("customadedonha", custom_lobby))
    # join alternatives
    app.add_handler(CommandHandler("join", join_command))
    app.add_handler(CommandHandler("joingame", join_command))
    # lobby callbacks
    app.add_handler(CallbackQueryHandler(join_lobby_callback, pattern="^join_lobby$"))
    app.add_handler(CallbackQueryHandler(lobby_callback_handler, pattern="^(start_game|mode_info)$"))
    # game cancel
    app.add_handler(CommandHandler("gamecancel", gamecancel_command))
    # submissions
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submission_handler))
    # admin owner tools
    app.add_handler(CommandHandler("dumpstats", dumpstats_command))
    app.add_handler(CommandHandler("statsreset", statsreset_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    # stats & misc
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler("validate", validate_command))
    # manual validation callbacks
    app.add_handler(CallbackQueryHandler(manual_validation_callback, pattern="^manual_"))
    print("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
