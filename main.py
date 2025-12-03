
---

## Now — the full `main.py` (copy and paste)

> This is the single file you requested. It contains configuration at top (for owners, tokens, and options). Edit OWNER_IDS and environment variables as needed.

```python
#!/usr/bin/env python3
# main.py - Adedonha Telegram Bot (PTB 21.4)
# Run: python main.py
# Requirements: python-telegram-bot==21.4, openai

import os
import logging
import random
import asyncio
import csv
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Telegram imports
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# OpenAI import - support both modern and classic interfaces (graceful)
ai_available = False
openai_modern = None
openai_classic = None
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

try:
    # Try modern OpenAI client (if installed)
    from openai import OpenAI as ModernOpenAI
    openai_modern = ModernOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    ai_available = bool(openai_modern)
except Exception:
    openai_modern = None
    try:
        # Try classic openai package fallback
        import openai as openai_classic_pkg
        if OPENAI_API_KEY:
            openai_classic_pkg.api_key = OPENAI_API_KEY
            openai_classic = openai_classic_pkg
            ai_available = True
    except Exception:
        openai_classic = None
        ai_available = False

# ---------------- Configuration (edit if desired) ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN environment variable before running.")

# Bot owners (numeric Telegram user IDs). You supplied two IDs earlier.
OWNER_IDS = {624102836, 1707015091}

# Database file name
DB_FILE = "stats.db"

# Player emoji for lobby list
PLAYER_EMOJI = "🦩"

# Category pool (12)
ALL_CATEGORIES = [
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

# Classic default categories (the "classic five")
CLASSIC_CATEGORIES = ["Name", "Object", "Plant", "Animal", "City/Country/State"]

# Limits and timeouts
MAX_PLAYERS = 10
LOBBY_TIMEOUT = 5 * 60  # 5 minutes to auto-cancel if nobody joins (or optional logic)
CLASSIC_NO_SUBMIT_TIMEOUT = 3 * 60  # 3 minutes if no one submits => round ends no penalties
FAST_ROUND_DURATION = 60  # 1 minute per round in fast mode
POST_FIRST_SUBMIT_WINDOW = 2  # after first submit, others have this many seconds
# Rounds
CLASSIC_ROUNDS = 10
FAST_ROUNDS = 12

# SQLite: stats fields -- NO mvps in DB as requested
# fields: games_played, total_validated_words, total_wordlists_sent

# Scoring values
POINT_UNIQUE = 10
POINT_SHARED = 5

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- Game state (per chat) ----------------
# Each chat_id maps to a dict with keys:
# state: 'lobby'|'running'
# mode: 'classic'|'custom'|'fast'
# creator_id, creator_name
# players: dict uid -> name
# lobby_message_id, lobby_chat_message object stored minimally
# categories_setting: list (for custom or fast) or None
# categories_per_round_count: int (for custom if using count option) - not used for classic
# current_round, rounds_total
# round_categories (list for current round)
# round_letter
# submissions: dict uid (str) -> raw text
# tasks: references to asyncio tasks for timeouts
games: Dict[int, Dict] = {}

# ---------------- Utilities ----------------
def escape_md_v2(text: str) -> str:
    """Escape text for Telegram MarkdownV2 formatting."""
    if text is None:
        return ""
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return "".join("\\" + c if c in to_escape else c for c in str(text))

def user_link_md(uid: int, name: str) -> str:
    return f"[{escape_md_v2(name)}](tg://user?id={uid})"

# ---------------- Database (SQLite) ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY,
            games_played INTEGER DEFAULT 0,
            total_validated_words INTEGER DEFAULT 0,
            total_wordlists_sent INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

def db_ensure_user(uid: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM stats WHERE user_id=?", (uid,))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO stats (user_id, games_played, total_validated_words, total_wordlists_sent) VALUES (?,?,?,?)",
            (uid, 0, 0, 0),
        )
    conn.commit()
    conn.close()

def db_update_after_round(uid: str, validated_words: int, submitted_any: bool):
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if submitted_any:
        c.execute(
            "UPDATE stats SET total_wordlists_sent = total_wordlists_sent + 1, total_validated_words = total_validated_words + ? WHERE user_id=?",
            (validated_words, uid),
        )
    else:
        # only validated words may be added (0)
        c.execute(
            "UPDATE stats SET total_validated_words = total_validated_words + 0 WHERE user_id=?",
            (uid,),
        )
    conn.commit()
    conn.close()

def db_update_after_game(user_ids: List[str]):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for uid in user_ids:
        db_ensure_user(uid)
        c.execute("UPDATE stats SET games_played = games_played + 1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def db_get_stats(uid: str) -> Dict[str, int]:
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT games_played, total_validated_words, total_wordlists_sent FROM stats WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"games_played": row[0], "total_validated_words": row[1], "total_wordlists_sent": row[2]}
    return {"games_played": 0, "total_validated_words": 0, "total_wordlists_sent": 0}

def db_get_leaderboard_top10() -> List[Tuple[str, int]]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, total_validated_words FROM stats ORDER BY total_validated_words DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    return rows

def db_reset_stats():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE stats SET games_played=0, total_validated_words=0, total_wordlists_sent=0")
    conn.commit()
    conn.close()

# ---------------- AI validation ----------------
async def ai_validate_answer(category: str, answer: str, letter: str) -> bool:
    """
    Returns True if AI judges answer valid for the category and starts with letter.
    If modern OpenAI client available, use it; else use classic openai package.
    If both unavailable or API fails, raise RuntimeError to trigger manual validation.
    """
    if not answer:
        return False

    question = (
        f"You are a terse validator. Answer only YES or NO.\n"
        f"Does the answer '{answer}' belong to category '{category}' and start with letter '{letter}' (case-insensitive)?\n"
        "Answer YES or NO."
    )

    try:
        if openai_modern:
            # Modern client (responses)
            resp = openai_modern.responses.create(model="gpt-3.5-turbo", input=question, max_output_tokens=6)
            # Extract text content robustly
            out = ""
            if getattr(resp, "output", None):
                for block in resp.output:
                    if isinstance(block, dict):
                        content = block.get("content", "")
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
        elif openai_classic:
            # classic openai.chat completion
            resp = openai_classic.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": question}],
                max_tokens=6,
            )
            if resp and resp.choices and len(resp.choices) > 0:
                out = resp.choices[0].message.content.strip().upper()
                return out.startswith("YES")
        else:
            # No AI client available
            raise RuntimeError("OpenAI client not available")
    except Exception as e:
        logger.exception("AI validation error: %s", e)
        # Raise to trigger manual validation workflow
        raise RuntimeError("AI validation failed") from e

# ---------------- Helper: choose categories for a round ----------------
def choose_random_categories(pool: List[str], count: int) -> List[str]:
    if count >= len(pool):
        return pool.copy()
    return random.sample(pool, count)

# ---------------- Lobby creation and management ----------------
async def create_lobby_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, g: Dict):
    """
    Build and send lobby message and pin it. Also set g['lobby_message_id'].
    """
    mode = g["mode"]
    mode_text = "Classic Adedonha" if mode == "classic" else ("Fast Adedonha" if mode == "fast" else "Custom Adedonha")
    if mode == "classic":
        cat_info = f"This game uses the classic categories per round: {', '.join(CLASSIC_CATEGORIES)}"
        rounds = CLASSIC_ROUNDS
    elif mode == "fast":
        cat_info = "This fast game uses 3 categories per round."
        rounds = FAST_ROUNDS
        if g.get("categories_setting"):
            cat_info += f" (Chosen categories: {', '.join(g['categories_setting'])})"
    else:  # custom
        chosen = g.get("categories_setting") or []
        cat_info = f"This custom game will randomly pick {len(chosen)} categories from the custom set each round."
        rounds = g.get("rounds_total", CLASSIC_ROUNDS)

    players_md = "\n".join(f"{PLAYER_EMOJI} {user_link_md(uid, name)}" for uid, name in g["players"].items())
    if not players_md:
        players_md = f"{PLAYER_EMOJI} (no players yet)"

    text = (
        f"*Adedonha Lobby — {escape_md_v2(mode_text)}*\n\n"
        f"{escape_md_v2(cat_info)}\n"
        f"*Total rounds:* {rounds}\n\n"
        f"*Creator:* {user_link_md(g['creator_id'], g['creator_name'])}\n\n"
        f"*Players:*\n{players_md}\n\n"
        "Press *Join* to enter the lobby. Use /joingame or /join as an alternative (bot will delete that message to reduce spam)."
    )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Join ✅", callback_data="join_lobby"), InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")],
                               [InlineKeyboardButton("Start ▶️", callback_data="start_game")]])

    sent = await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2", reply_markup=kb)
    g["lobby_message_id"] = sent.message_id
    g["lobby_message_chat_id"] = chat_id

    # try to pin the message (requires bot to be admin)
    try:
        await context.bot.pin_chat_message(chat_id, sent.message_id)
        g["lobby_pinned"] = True
    except Exception as e:
        logger.info("Could not pin lobby message: %s", e)
        g["lobby_pinned"] = False

# ---------------- Command handlers ----------------
async def start_classic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /classicadedonha - creates lobby that uses classic categories (CLASSIC_CATEGORIES)
    if update.effective_chat.type == "private":
        await update.message.reply_text("This bot runs games only in groups/supergroups. Please add it to a group.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    # one lobby/game at a time per chat
    if chat_id in games:
        await update.message.reply_text("A game or lobby already exists in this chat. Use /gamecancel to cancel it first.")
        return
    games[chat_id] = {
        "state": "lobby",
        "mode": "classic",
        "creator_id": user.id,
        "creator_name": user.first_name,
        "players": {user.id: user.first_name},
        "lobby_message_id": None,
        "lobby_pinned": False,
        "categories_setting": CLASSIC_CATEGORIES.copy(),
        "rounds_total": CLASSIC_ROUNDS,
        "current_round": 0,
        "submissions": {},
        "tasks": {},
    }
    await create_lobby_message(context, chat_id, games[chat_id])

    # schedule auto-cancel if nobody else joins within LOBBY_TIMEOUT
    async def auto_cancel():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat_id)
        if not g or g.get("state") != "lobby":
            return
        # if only the creator is present, cancel
        if len(g["players"]) <= 1:
            try:
                await context.bot.send_message(chat_id, "Lobby cancelled due to inactivity (nobody joined).")
            except Exception:
                pass
            await cleanup_game(chat_id, context)
    games[chat_id]["tasks"]["lobby_timeout"] = asyncio.create_task(auto_cancel())

async def custom_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /customadedonha cat1,cat2,... or passed as separate args
    if update.effective_chat.type == "private":
        await update.message.reply_text("This bot runs games only in groups/supergroups. Please add it to a group.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id in games:
        await update.message.reply_text("A game or lobby already exists in this chat. Use /gamecancel to cancel it first.")
        return

    # parse categories: support comma-separated in message after command or space-separated arguments
    args = context.args
    cats = []
    if args:
        # allow comma separated combined arg
        joined_args = " ".join(args)
        # split by comma
        split_cats = [x.strip() for x in joined_args.replace(";", ",").split(",") if x.strip()]
        # validate that category names are in ALL_CATEGORIES (case-insensitive)
        for sc in split_cats:
            for pool in ALL_CATEGORIES:
                if sc.lower() == pool.lower():
                    cats.append(pool)
                    break
            else:
                # invalid category
                await update.message.reply_text(f"Unknown category: {sc}. Use /categories to view allowed categories.")
                return
    else:
        await update.message.reply_text("Please provide categories. Example:\n/customadedonha Name,Object,Animal,Plant,Food")
        return

    if len(cats) < 1 or len(cats) > 12:
        await update.message.reply_text("You may provide between 1 and 12 categories.")
        return

    games[chat_id] = {
        "state": "lobby",
        "mode": "custom",
        "creator_id": user.id,
        "creator_name": user.first_name,
        "players": {user.id: user.first_name},
        "lobby_message_id": None,
        "lobby_pinned": False,
        "categories_setting": cats.copy(),
        "rounds_total": CLASSIC_ROUNDS,
        "current_round": 0,
        "submissions": {},
        "tasks": {},
    }
    await create_lobby_message(context, chat_id, games[chat_id])

    async def auto_cancel():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat_id)
        if not g or g.get("state") != "lobby":
            return
        if len(g["players"]) <= 1:
            try:
                await context.bot.send_message(chat_id, "Lobby cancelled due to inactivity (nobody joined).")
            except Exception:
                pass
            await cleanup_game(chat_id, context)
    games[chat_id]["tasks"]["lobby_timeout"] = asyncio.create_task(auto_cancel())

async def start_fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /fastdedonha cat1 cat2 cat3
    if update.effective_chat.type == "private":
        await update.message.reply_text("This bot runs games only in groups/supergroups. Please add it to a group.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id in games:
        await update.message.reply_text("A game or lobby already exists in this chat. Use /gamecancel to cancel it first.")
        return

    args = context.args
    cats = []
    if args and len(args) >= 3:
        # take first 3 args
        raw = args[:3]
        # validate against pool
        for r in raw:
            matched = False
            for pool in ALL_CATEGORIES:
                if r.lower() == pool.lower():
                    cats.append(pool)
                    matched = True
                    break
            if not matched:
                await update.message.reply_text(f"Unknown category: {r}. Use /categories to view allowed categories.")
                return
    else:
        # pick 3 random categories
        cats = random.sample(ALL_CATEGORIES, 3)

    games[chat_id] = {
        "state": "lobby",
        "mode": "fast",
        "creator_id": user.id,
        "creator_name": user.first_name,
        "players": {user.id: user.first_name},
        "lobby_message_id": None,
        "lobby_pinned": False,
        "categories_setting": cats.copy(),
        "rounds_total": FAST_ROUNDS,
        "current_round": 0,
        "submissions": {},
        "tasks": {},
    }
    await create_lobby_message(context, chat_id, games[chat_id])

    async def auto_cancel():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat_id)
        if not g or g.get("state") != "lobby":
            return
        if len(g["players"]) <= 1:
            try:
                await context.bot.send_message(chat_id, "Lobby cancelled due to inactivity (nobody joined).")
            except Exception:
                pass
            await cleanup_game(chat_id, context)
    games[chat_id]["tasks"]["lobby_timeout"] = asyncio.create_task(auto_cancel())

# ---------------- Join handlers ----------------
async def join_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = cq.from_user
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    await cq.answer()
    if not g or g.get("state") != "lobby":
        await cq.answer("No lobby to join.", show_alert=True)
        return
    if len(g["players"]) >= MAX_PLAYERS:
        await cq.answer("Lobby is full.", show_alert=True)
        return
    if user.id in g["players"]:
        await cq.answer("You're already in the lobby.")
        return
    g["players"][user.id] = user.first_name
    # Update lobby message players list
    try:
        await context.bot.edit_message_text(
            text=build_lobby_text(g),
            chat_id=chat_id,
            message_id=g["lobby_message_id"],
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join ✅", callback_data="join_lobby"), InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")],
                                              [InlineKeyboardButton("Start ▶️", callback_data="start_game")]])
        )
    except Exception:
        pass
    await cq.answer("Joined the lobby.")

def build_lobby_text(g: Dict) -> str:
    mode = g["mode"]
    mode_text = "Classic Adedonha" if mode == "classic" else ("Fast Adedonha" if mode == "fast" else "Custom Adedonha")
    if mode == "classic":
        cat_info = f"Uses classic categories per round: {', '.join(CLASSIC_CATEGORIES)}"
        rounds = CLASSIC_ROUNDS
    elif mode == "fast":
        cat_info = f"Uses 3 categories per round: {', '.join(g.get('categories_setting', []))}"
        rounds = FAST_ROUNDS
    else:
        chosen = g.get("categories_setting", [])
        cat_info = f"Custom set: {', '.join(chosen)}"
        rounds = g.get("rounds_total", CLASSIC_ROUNDS)
    players_md = "\n".join(f"{PLAYER_EMOJI} {user_link_md(uid, name)}" for uid, name in g["players"].items())
    if not players_md:
        players_md = f"{PLAYER_EMOJI} (no players yet)"
    text = (
        f"*Adedonha Lobby — {escape_md_v2(mode_text)}*\n\n"
        f"{escape_md_v2(cat_info)}\n"
        f"*Total rounds:* {rounds}\n\n"
        f"*Creator:* {user_link_md(g['creator_id'], g['creator_name'])}\n\n"
        f"*Players:*\n{players_md}\n\n"
        "Press *Join* to enter the lobby. Use /joingame or /join as an alternative (bot will delete that message to reduce spam)."
    )
    return text

async def join_command_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /join or /joingame alias — add player and delete the command message to avoid spam
    chat_id = update.effective_chat.id
    user = update.effective_user
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await update.message.reply_text("No active lobby to join.")
        return
    if len(g["players"]) >= MAX_PLAYERS:
        await update.message.reply_text("Lobby is full (10 players).")
        return
    if user.id in g["players"]:
        # already in lobby
        try:
            await update.message.delete()
        except Exception:
            pass
        return
    g["players"][user.id] = user.first_name
    # update lobby message
    try:
        await context.bot.edit_message_text(
            text=build_lobby_text(g),
            chat_id=chat_id,
            message_id=g["lobby_message_id"],
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join ✅", callback_data="join_lobby"), InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")],
                                              [InlineKeyboardButton("Start ▶️", callback_data="start_game")]])
        )
    except Exception:
        pass
    # delete join command message to reduce spam
    try:
        await update.message.delete()
    except Exception:
        pass

# ---------------- Start game flow ----------------
async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = cq.from_user
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    await cq.answer()
    if not g or g.get("state") != "lobby":
        await cq.answer("No lobby to start.", show_alert=True)
        return
    # Only allow creator or group admin or owner to start
    is_creator = user.id == g["creator_id"]
    is_owner = user.id in OWNER_IDS
    is_admin = False
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False

    if not (is_creator or is_admin or is_owner):
        await cq.answer("Only the lobby creator, a chat admin, or a bot owner may start the game.", show_alert=True)
        return

    # must have at least 2 players
    if len(g["players"]) < 2:
        await cq.answer("Need at least 2 players to start.", show_alert=True)
        return

    await cq.answer("Starting game...")
    # Cancel lobby timeout task
    lt = g["tasks"].get("lobby_timeout")
    if lt:
        lt.cancel()
    # Unpin lobby message if pinned
    if g.get("lobby_pinned"):
        try:
            await context.bot.unpin_chat_message(chat_id, g["lobby_message_id"])
        except Exception:
            pass

    # set state to running and launch round loop in background
    g["state"] = "running"
    g["scores"] = {str(uid): 0 for uid in g["players"].keys()}
    # persist games_played
    db_update_after_game([str(uid) for uid in g["players"].keys()])

    # start rounds task
    g["tasks"]["game_task"] = asyncio.create_task(run_game_loop(chat_id, context))

async def run_game_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Runs the rounds sequentially for a game.
    """
    g = games.get(chat_id)
    if not g:
        return
    rounds_total = g.get("rounds_total", CLASSIC_ROUNDS)
    mode = g.get("mode", "classic")
    for r in range(1, rounds_total + 1):
        # re-check
        g = games.get(chat_id)
        if not g or g.get("state") != "running":
            return
        g["current_round"] = r

        # pick categories for this round
        if mode == "classic":
            # if you prefer classical set to be random among the classic five for each round,
            # we decided classically to use the classic five always (per user's final instruction).
            round_cats = CLASSIC_CATEGORIES.copy()
        elif mode == "fast":
            # for fast mode we use the categories_setting (exactly 3)
            round_cats = g.get("categories_setting", [])[:3]
        else:  # custom
            # pick random categories count == len(categories_setting)
            pool = g.get("categories_setting", ALL_CATEGORIES)
            k = len(pool)
            if k <= 0:
                pool = ALL_CATEGORIES
            # choose k randomly from pool (or if they provided exact set, pick random subset of it)
            if len(pool) <= k:
                round_cats = pool.copy()
            else:
                round_cats = random.sample(pool, k)

        g["round_categories"] = round_cats
        # random letter
        letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        g["round_letter"] = letter
        g["submissions"] = {}  # clear submissions for the round
        g["manual_validation"] = {}  # used if AI fails

        # send round start message with template
        cat_lines = "\n".join(f"{i+1}. {escape_md_v2(cat)}:" for i, cat in enumerate(round_cats))
        rounds_total_val = g.get("rounds_total", CLASSIC_ROUNDS)
        text = (
            f"*Round {r}/{rounds_total_val} — {escape_md_v2(g.get('mode',''))}*\n"
            f"Letter: *{escape_md_v2(letter)}*\n\n"
            "Send your answers in ONE message using this template (copy/paste):\n\n"
            f"```\n{cat_lines}\n```\n\n"
            f"• First submission starts a *{POST_FIRST_SUBMIT_WINDOW}s* window for others to submit.\n"
            f"• If nobody submits in {CLASSIC_NO_SUBMIT_TIMEOUT//60 if g['mode']=='classic' else FAST_ROUND_DURATION//60} minute(s), the round ends with no penalties."
        )
        await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")

        # schedule no-submission timeout
        no_submit_task = None
        if g["mode"] == "classic":
            delay = CLASSIC_NO_SUBMIT_TIMEOUT
        else:
            delay = FAST_ROUND_DURATION

        async def no_submission_end(chat_id_local: int, round_num: int):
            await asyncio.sleep(delay)
            g_local = games.get(chat_id_local)
            if not g_local or g_local.get("state") != "running":
                return
            if not g_local.get("submissions"):
                # no submissions — end round with no penalties and append empty round to history
                try:
                    await context.bot.send_message(chat_id_local, f"⏱ Round {round_num} ended: no submissions. No penalties.", parse_mode="MarkdownV2")
                except Exception:
                    pass
                # append empty history and continue
                g_local.setdefault("round_scores_history", []).append({})
                return

        no_submit_task = asyncio.create_task(no_submission_end(chat_id, r))
        g["tasks"]["no_submit_task"] = no_submit_task

        # Wait until round has a recorded history entry
        # The scoring function will append to round_scores_history once done
        while True:
            await asyncio.sleep(0.5)
            g = games.get(chat_id)
            if not g:
                return
            # round_results appended by scoring phase
            if len(g.get("round_scores_history", [])) >= r:
                break

    # All rounds complete -> final scoreboard
    g = games.get(chat_id)
    if not g:
        return
    # build final leaderboard sorted by validated words or total score? The user requested leaderboard validated
    # We'll show final scores (sum of per-round category points) and produce validated-words leaderboard via /leaderboard
    final_scores = g.get("scores", {})
    items = sorted(final_scores.items(), key=lambda x: -x[1])
    text = "*Game Over — Final Scores*\n\n"
    for uid_str, pts in items:
        uid = int(uid_str)
        name = g["players"].get(uid, "Player")
        text += f"{user_link_md(uid, name)} — `{pts}` pts\n"
    await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
    # cleanup game state
    await cleanup_game(chat_id, context)

async def cleanup_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return
    # cancel tasks
    for t in g.get("tasks", {}).values():
        try:
            t.cancel()
        except Exception:
            pass
    # unpin lobby if needed
    try:
        if g.get("lobby_pinned") and g.get("lobby_message_id"):
            await context.bot.unpin_chat_message(chat_id, g["lobby_message_id"])
    except Exception:
        pass
    # remove from games
    games.pop(chat_id, None)

# ---------------- Message submission handler ----------------
async def submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return  # ignore private
    chat_id = update.effective_chat.id
    user = update.effective_user
    text = update.message.text or ""
    g = games.get(chat_id)
    if not g or g.get("state") != "running":
        return
    if user.id not in g["players"]:
        return
    # ignore commands
    if text.startswith("/"):
        return
    uid_str = str(user.id)
    if uid_str in g.get("submissions", {}):
        # already submitted this round
        try:
            await update.message.reply_text("You already submitted for this round.")
        except Exception:
            pass
        return
    g["submissions"][uid_str] = text
    # announce first submit and start short window
    if len(g["submissions"]) == 1:
        # cancel no_submit task (if any)
        t = g["tasks"].get("no_submit_task")
        if t:
            try:
                t.cancel()
            except Exception:
                pass
            g["tasks"]["no_submit_task"] = None
        # notify
        try:
            await context.bot.send_message(chat_id, f"⏱ {user_link_md(user.id, user.first_name)} submitted first! Others have {POST_FIRST_SUBMIT_WINDOW}s to submit.", parse_mode="MarkdownV2")
        except Exception:
            pass
        # schedule short window
        async def short_window_end(chat_id_local: int, round_num: int):
            await asyncio.sleep(POST_FIRST_SUBMIT_WINDOW)
            # after short window, trigger scoring
            await perform_scoring(chat_id_local, context)
        g["tasks"]["short_window_task"] = asyncio.create_task(short_window_end(chat_id, g["current_round"]))
    else:
        # other players can still submit within short window; do nothing
        pass

# ---------------- Scoring ----------------
def parse_submission_text_to_answers(text: str, expected_count: int) -> List[str]:
    """
    Extract answers from submission text. Only take the first expected_count answers.
    Each answer is parsed from the line after ':' if present, else take the line.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answers = []
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            answers.append(parts[1].strip())
        else:
            answers.append(line.strip())
    while len(answers) < expected_count:
        answers.append("")
    return answers[:expected_count]

async def perform_scoring(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Validate each submitted answer (AI or manual) and score:
    - Unique valid: 10 points
    - Shared valid: 5 points
    No penalties for missing answers (as requested).
    """
    g = games.get(chat_id)
    if not g:
        return
    # cancel any pending tasks
    for key in ("short_window_task", "no_submit_task"):
        t = g["tasks"].get(key)
        if t:
            try:
                t.cancel()
            except Exception:
                pass
            g["tasks"][key] = None

    submissions = g.get("submissions", {})
    if not submissions:
        # no submissions this round; record empty history
        g.setdefault("round_scores_history", []).append({})
        return

    round_cats = g.get("round_categories", [])
    letter = g.get("round_letter", "A")
    expected_count = len(round_cats)

    # Parse answers per player
    parsed = {}  # uid_str -> list[str]
    for uid_str, raw in submissions.items():
        parsed[uid_str] = parse_submission_text_to_answers(raw, expected_count)

    # We'll try AI validation for each answer. If AI validation fails (raises), fall into manual validation flow.
    ai_failure = False
    validation_results: Dict[str, List[bool]] = {}  # uid_str -> list of booleans per category

    try:
        for uid_str, answers in parsed.items():
            validation_results[uid_str] = []
            for idx, ans in enumerate(answers):
                ans_clean = ans.strip()
                if not ans_clean:
                    validation_results[uid_str].append(False)
                    continue
                try:
                    ok = await ai_validate_answer(round_cats[idx], ans_clean, letter)
                    validation_results[uid_str].append(bool(ok))
                except RuntimeError:
                    # AI failed -> trigger manual validation
                    ai_failure = True
                    break
            if ai_failure:
                break
    except Exception:
        ai_failure = True

    if ai_failure:
        # Notify that AI unavailable and allow admins to perform manual validation
        admin_notify_text = "⚠️ *AI validation temporarily unavailable.* Admins may validate submissions manually.\n\n"
        admin_notify_text += "Press *Manual Validate* to open a single message with inline buttons for admins to approve/reject each player's submission. This avoids spamming the group."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Manual Validate 🛠️", callback_data="start_manual_validation")]])
        msg = await context.bot.send_message(chat_id, admin_notify_text, parse_mode="MarkdownV2", reply_markup=kb)
        # store the pending manual review object
        g["manual_review_request"] = {
            "trigger_message_id": msg.message_id,
            "parsed": parsed,
            "round_cats": round_cats,
            "letter": letter,
        }
        return

    # If we reached here, AI validated all answers
    # Compute uniqueness per category among validated answers
    per_category_counts: List[Dict[str, int]] = []
    for idx in range(expected_count):
        freq = {}
        for uid_str, answers in parsed.items():
            a = answers[idx].strip()
            if a and validation_results[uid_str][idx]:
                key = a.strip().lower()
                freq[key] = freq.get(key, 0) + 1
        per_category_counts.append(freq)

    # Compute scores for players
    round_scores = {}
    for uid_str, answers in parsed.items():
        pts = 0
        validated_words = 0
        for idx, ans in enumerate(answers):
            a = ans.strip()
            if not a:
                continue
            if not validation_results[uid_str][idx]:
                continue
            validated_words += 1
            key = a.strip().lower()
            if per_category_counts[idx].get(key, 0) == 1:
                pts += POINT_UNIQUE
            else:
                pts += POINT_SHARED
        round_scores[uid_str] = {"points": pts, "validated_words": validated_words, "submitted_any": True}
        # update totals
        g["scores"][uid_str] = g["scores"].get(uid_str, 0) + pts
        db_update_after_round(uid_str, validated_words, submitted_any=True)

    # Append history
    g.setdefault("round_scores_history", []).append(round_scores)

    # Send round summary
    summary = f"*Round {g['current_round']} Results*\nLetter: *{escape_md_v2(letter)}*\n\n"
    # list players sorted by points (round points)
    sorted_players = sorted(list(g["players"].keys()), key=lambda uid: -round_scores.get(str(uid), {}).get("points", 0))
    for uid in sorted_players:
        uid_str = str(uid)
        name = g["players"].get(uid, "Player")
        pts = round_scores.get(uid_str, {}).get("points", 0)
        validated = round_scores.get(uid_str, {}).get("validated_words", 0)
        summary += f"{user_link_md(uid, name)} — `{pts}` pts (validated: `{validated}`)\n"
    await context.bot.send_message(chat_id, summary, parse_mode="MarkdownV2")

# ---------------- Manual validation callback flow ----------------
async def manual_validation_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = cq.from_user
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    await cq.answer()
    if not g or g.get("state") != "running":
        await cq.answer("No active game to validate.", show_alert=True)
        return
    # only allow admins or owners to open manual validation
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not (is_admin or user.id in OWNER_IDS):
        await cq.answer("Only group admins or bot owner may perform manual validation.", show_alert=True)
        return

    req = g.get("manual_review_request")
    if not req:
        await cq.answer("No manual validation pending.")
        return

    parsed = req["parsed"]
    round_cats = req["round_cats"]
    letter = req["letter"]

    # Build one compact message listing each player's answers and inline buttons (Approve / Reject per player)
    lines = []
    for uid_str, answers in parsed.items():
        uid = int(uid_str)
        name = g["players"].get(uid, "Player")
        lines.append(f"{user_link_md(uid, name)}")
        for idx, cat in enumerate(round_cats):
            ans = answers[idx].strip()
            lines.append(f"  {escape_md_v2(cat)}: {escape_md_v2(ans) if ans else '_(blank)_'}")
        lines.append("")  # blank line

    message_text = "*Manual Validation — Pending*\n\n" + "\n".join(lines) + "\n\nSelect for each player: Approve (accept all their answers) or Reject (count none). Then press Finalize."

    # Build inline keyboard with a row per player (approve/reject) + finalize button
    kb_rows = []
    for uid_str in parsed.keys():
        uid = int(uid_str)
        name = g["players"].get(uid, "Player")
        kb_rows.append([
            InlineKeyboardButton(f"✅ Approve {escape_md_v2(name)}", callback_data=f"manual_approve:{uid_str}"),
            InlineKeyboardButton(f"❌ Reject {escape_md_v2(name)}", callback_data=f"manual_reject:{uid_str}")
        ])
    kb_rows.append([InlineKeyboardButton("Finalize ✅", callback_data="manual_finalize")])
    kb = InlineKeyboardMarkup(kb_rows)

    # Send the single message and store its id for edits
    sent = await context.bot.send_message(chat_id, message_text, parse_mode="MarkdownV2", reply_markup=kb)
    g["manual_review_message_id"] = sent.message_id
    # initialize manual_review_results defaults to None (undecided)
    g["manual_validation_results"] = {uid_str: None for uid_str in parsed.keys()}
    await cq.answer("Manual validation message posted. Use buttons to approve/reject each player.")

async def manual_validation_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data
    user = cq.from_user
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    await cq.answer()
    if not g:
        await cq.answer("No active manual validation.")
        return
    # Only allow admins or owners to press these buttons
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not (is_admin or user.id in OWNER_IDS):
        await cq.answer("Only group admins or bot owner may moderate this.", show_alert=True)
        return

    if data.startswith("manual_approve:"):
        uid_str = data.split(":", 1)[1]
        g["manual_validation_results"][uid_str] = True
        await cq.answer("Approved.")
    elif data.startswith("manual_reject:"):
        uid_str = data.split(":", 1)[1]
        g["manual_validation_results"][uid_str] = False
        await cq.answer("Rejected.")
    elif data == "manual_finalize":
        # finalize: compute scoring using manual_validation_results
        # build validation_results from manual decisions
        parsed = g.get("manual_review_request", {}).get("parsed", {})
        round_cats = g.get("manual_review_request", {}).get("round_cats", [])
        expected_count = len(round_cats)
        validation_results = {}
        for uid_str, answers in parsed.items():
            decision = g["manual_validation_results"].get(uid_str, None)
            if decision is None:
                # default reject if undecided
                decision = False
            # decision True => all provided answers validated True (not granular)
            validation_results[uid_str] = [True if answers[idx].strip() else False for idx in range(expected_count)] if decision else [False]*expected_count

        # compute per-category counts
        per_category_counts = []
        for idx in range(expected_count):
            freq = {}
            for uid_str, answers in parsed.items():
                a = answers[idx].strip()
                if a and validation_results[uid_str][idx]:
                    key = a.strip().lower()
                    freq[key] = freq.get(key, 0) + 1
            per_category_counts.append(freq)

        # scoring as usual
        round_scores = {}
        for uid_str, answers in parsed.items():
            pts = 0
            validated_words = 0
            for idx, ans in enumerate(answers):
                a = ans.strip()
                if not a:
                    continue
                if not validation_results[uid_str][idx]:
                    continue
                validated_words += 1
                key = a.strip().lower()
                if per_category_counts[idx].get(key, 0) == 1:
                    pts += POINT_UNIQUE
                else:
                    pts += POINT_SHARED
            round_scores[uid_str] = {"points": pts, "validated_words": validated_words, "submitted_any": True}
            g["scores"][uid_str] = g["scores"].get(uid_str, 0) + pts
            db_update_after_round(uid_str, validated_words, submitted_any=True)
        g.setdefault("round_scores_history", []).append(round_scores)
        # send summary message and remove manual validation message
        summary = f"*Round {g['current_round']} Results (Manual Validation)*\n\n"
        sorted_players = sorted(list(g["players"].keys()), key=lambda uid: -round_scores.get(str(uid), {}).get("points", 0))
        for uid in sorted_players:
            uid_str = str(uid)
            name = g["players"].get(uid, "Player")
            pts = round_scores.get(uid_str, {}).get("points", 0)
            validated = round_scores.get(uid_str, {}).get("validated_words", 0)
            summary += f"{user_link_md(uid, name)} — `{pts}` pts (validated: `{validated}`)\n"
        try:
            await context.bot.edit_message_reply_markup(chat_id, g.get("manual_review_message_id"), reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(chat_id, summary, parse_mode="MarkdownV2")
        await cq.answer("Finalized manual validation and scored the round.")
    else:
        await cq.answer("Unknown action.")

# ---------------- Mode info callback ----------------
async def mode_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    await cq.answer()
    if not g:
        await cq.answer("No game/lobby active.")
        return
    mode = g.get("mode")
    if mode == "classic":
        text = (
            "*Classic Adedonha help*\n\n"
            "• Classic mode uses the classic categories per round: Name, Object, Plant, Animal, City/Country/State.\n"
            "• Rounds: 10. If nobody submits in 3 minutes, the round ends with no penalties.\n"
            "• After the first submission, others have 2 seconds to submit.\n"
            "• Scoring: 10 points for a unique validated answer, 5 points for shared validated answer."
        )
    elif mode == "fast":
        text = (
            "*Fast Adedonha help*\n\n"
            "• Fast mode uses 3 categories per round (either passed in command or chosen randomly).\n"
            "• Rounds: 12. Each round lasts 1 minute.\n"
            "• After the first submission, others have 2 seconds to submit.\n"
            "• Scoring: 10 for unique validated answer, 5 for shared."
        )
    else:
        cats = g.get("categories_setting", [])
        text = (
            "*Custom Adedonha help*\n\n"
            f"• Custom mode uses a custom set of categories (count: {len(cats)}).\n"
            "• Each round picks categories from that set. Rounds: 10.\n"
            "• 3-minute no-submission timeout; 2-second window after first submission.\n"
            "• Scoring: 10 for unique validated answer, 5 for shared."
        )
    try:
        await cq.answer()  # hide loading
        await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
    except Exception:
        pass

# ---------------- Admin owner commands: dumpstats, statsreset, leaderboard ----------------
async def dumpstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OWNER_IDS:
        await update.message.reply_text("Only bot owner(s) may use this command.")
        return
    # create formatted text
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, games_played, total_validated_words, total_wordlists_sent FROM stats ORDER BY total_validated_words DESC")
    rows = c.fetchall()
    conn.close()
    # build text table
    lines = ["UserID | Games | ValidatedWords | ListsSent"]
    for row in rows:
        lines.append(f"{row[0]} | {row[1]} | {row[2]} | {row[3]}")
    text = "```\n" + "\n".join(lines) + "\n```"
    await update.message.reply_text(text, parse_mode="MarkdownV2")
    # write CSV
    csv_file = "stats_export.csv"
    with open(csv_file, "w", newline="", encoding="utf8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "games_played", "total_validated_words", "total_wordlists_sent"])
        for row in rows:
            writer.writerow(row)
    # send CSV and DB file
    try:
        await update.message.reply_document(document=InputFile(csv_file))
        await update.message.reply_document(document=InputFile(DB_FILE))
    except Exception as e:
        logger.exception("Failed to send files: %s", e)

async def statsreset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OWNER_IDS:
        await update.message.reply_text("Only bot owner(s) may use this command.")
        return
    db_reset_stats()
    await update.message.reply_text("All stats reset to zero.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in OWNER_IDS:
        await update.message.reply_text("Only bot owner(s) may use this command.")
        return
    top = db_get_leaderboard_top10()
    text = "*Top 10 — validated words*\n\n"
    rank = 1
    for uid_str, validated in top:
        uid = int(uid_str)
        # try to fetch a name from any active game; else just show id
        name = None
        for g in games.values():
            if uid in g.get("players", {}):
                name = g["players"].get(uid)
                break
        if not name:
            name = str(uid)
        text += f"{rank}. {user_link_md(uid, name)} — `{validated}` validated words\n"
        rank += 1
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ---------------- /mystats command ----------------
async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if reply -> show replied user stats
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
    else:
        u = update.effective_user
    uid_str = str(u.id)
    s = db_get_stats(uid_str)
    # global position by validated words
    top = db_get_leaderboard_top10()
    pos = 1
    for idx, (uid_row, _) in enumerate(top, start=1):
        if uid_row == uid_str:
            pos = idx
            break
    text = (
        f"*Stats of {user_link_md(u.id, u.first_name)}*\n\n"
        f"• *Games played:* `{s['games_played']}`\n"
        f"• *Total validated words:* `{s['total_validated_words']}`\n"
        f"• *Wordlists sent:* `{s['total_wordlists_sent']}`\n"
        f"• *Global position (by validated words):* `{pos}`\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ---------------- /categories command ----------------
async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "*All possible categories (12):*\n\n"
    for i, c in enumerate(ALL_CATEGORIES, start=1):
        text += f"{i}. {escape_md_v2(c)}\n"
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ---------------- /gamecancel command ----------------
async def gamecancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    g = games.get(chat_id)
    if not g:
        await update.message.reply_text("No active game or lobby to cancel.")
        return
    # only creator, chat admin, or owner can cancel
    is_creator = user.id == g["creator_id"]
    is_owner = user.id in OWNER_IDS
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not (is_creator or is_admin or is_owner):
        await update.message.reply_text("Only the creator, a chat admin, or bot owner may cancel the game.")
        return
    await cleanup_game(chat_id, context)
    await update.message.reply_text("Game/lobby cancelled.")

# ---------------- CallbackQuery registration ----------------
async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # route various callback_data values
    data = update.callback_query.data
    if data == "join_lobby":
        await join_lobby_callback(update, context)
    elif data == "start_game":
        await start_game_callback(update, context)
    elif data == "mode_info":
        await mode_info_callback(update, context)
    elif data == "start_manual_validation" or data == "manual_validate" or data == "start_manual_validation":
        await manual_validation_start_callback(update, context)
    elif data.startswith("manual_") or data == "manual_finalize":
        await manual_validation_button_handler(update, context)
    else:
        # unknown; answer to avoid spinner
        await update.callback_query.answer()

# ---------------- Startup ----------------
def main():
    # init db
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("classicadedonha", start_classic))
    app.add_handler(CommandHandler("customadedonha", custom_lobby))
    app.add_handler(CommandHandler("fastdedonha", start_fast))

    app.add_handler(CommandHandler("join", join_command_alias))
    app.add_handler(CommandHandler("joingame", join_command_alias))

    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler("modeinfo", mode_info_callback))
    app.add_handler(CommandHandler("mystats", mystats_command))

    app.add_handler(CommandHandler("dumpstats", dumpstats_command))
    app.add_handler(CommandHandler("statsreset", statsreset_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("gamecancel", gamecancel_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_query_router))

    # Submission message handler for running games
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submission_handler))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
