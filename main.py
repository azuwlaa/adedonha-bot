# ---------------- CONFIG - set tokens directly here ----------------
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY_HERE"  # optional - leave empty to use manual admin validation

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

# ---------------- IMPORTS ----------------
import logging
import random
import asyncio
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from openai import OpenAI
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
if OPENAI_API_KEY:
    try:
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning("OpenAI client init failed: %s. Bot will fall back to manual admin validation.", e)
        ai_client = None

# ---------------- SQLITE STATS ----------------
def init_db() -> None:
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

def db_ensure_user(uid: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM stats WHERE user_id=?", (uid,))
    if not c.fetchone():
        c.execute("INSERT INTO stats (user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()

def db_update_after_round(uid: str, validated_words: int, submitted_any: bool) -> None:
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if submitted_any:
        c.execute("""
            UPDATE stats
            SET total_wordlists_sent = total_wordlists_sent + 1,
                total_validated_words = total_validated_words + ?
            WHERE user_id=?
        """, (validated_words, uid))
    conn.commit()
    conn.close()

def db_update_after_game(user_ids: List[str]) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for uid in user_ids:
        db_ensure_user(uid)
        c.execute("UPDATE stats SET games_played = games_played + 1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def db_get_stats(uid: str):
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT games_played, total_validated_words, total_wordlists_sent FROM stats WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"games_played": row[0], "total_validated_words": row[1], "total_wordlists_sent": row[2]}
    return {"games_played": 0, "total_validated_words": 0, "total_wordlists_sent": 0}

def db_dump_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, games_played, total_validated_words, total_wordlists_sent FROM stats ORDER BY total_validated_words DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def db_reset_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE stats SET games_played=0, total_validated_words=0, total_wordlists_sent=0")
    conn.commit()
    conn.close()

# ---------------- UTIL ----------------
def escape_md_v2(text: str) -> str:
    if text is None:
        return ""
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return ''.join(('\\' + c) if c in to_escape else c for c in str(text))

def user_mention_md(uid: int, name: str) -> str:
    return f"[{escape_md_v2(name)}](tg://user?id={uid})"

def choose_random_categories(count: int) -> List[str]:
    return random.sample(ALL_CATEGORIES, count)

def extract_answers_from_text(text: str, count: int) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answers = []
    for line in lines:
        if ":" in line:
            parts = line.split(":", 1)
            answers.append(parts[1].strip())
        else:
            answers.append(line.strip())
        if len(answers) >= count:
            break
    while len(answers) < count:
        answers.append("")
    return answers[:count]

# ---------------- AI VALIDATION ----------------
async def ai_validate(category: str, answer: str, letter: str) -> bool:
    if not answer:
        return False
    if not answer[0].isalpha() or answer[0].upper() != letter.upper():
        return False
    if not ai_client:
        # permissive fallback so gameplay continues; admins can manually validate
        return True
    prompt = f"""
You are a terse validator for the game Adedonha.
Rules:
- The answer must start with the letter '{letter}' (case-insensitive).
- It must correctly belong to the category: '{category}'.
Respond with only YES or NO.
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

# ---------------- GAME STATE ----------------
games: Dict[int, Dict] = {}  # chat_id -> game dict

def is_owner(uid: int) -> bool:
    return str(uid) in OWNERS

# ---------------- COMMANDS / LOBBY ----------------
async def classic_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    # parse optional number (but per user request classic cannot define categories; they later preferred /customadedonha for custom categories -> here we accept optional but encourage custom)
    args = context.args or []
    num = 5
    if args:
        try:
            val = int(args[0])
            if 5 <= val <= 8:
                num = val
            else:
                await update.message.reply_text("Please provide a number between 5 and 8. Using default 5.")
        except Exception:
            await update.message.reply_text("Invalid number provided. Using default 5.")
    if chat.id in games and games[chat.id].get("state") in ("lobby", "running"):
        await update.message.reply_text("A game or lobby is already active in this group.")
        return
    lobby = {
        "mode": "classic",
        "categories_per_round": num,
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
        "manual_accept": {}
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_md = user_mention_md(user.id, user.first_name)
    text = (f"*Adedonha lobby created!*\\n\\nMode: *Classic*\\nCategories per round: *{num}*\\nTotal rounds: *{TOTAL_ROUNDS_CLASSIC}*\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    # attempt to pin
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info("Pin failed: %s", e)
    # schedule lobby timeout
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get("state") == "lobby":
            if len(g.get("players", {})) <= 1:
                try:
                    await context.bot.send_message(chat.id, "Lobby cancelled due to inactivity.")
                except Exception:
                    pass
                games.pop(chat.id, None)
    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

async def custom_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /customadedonha categories up to 12 comma separated OR as multiple args
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Please provide categories, e.g. /customadedonha Name Object Animal Plant Country")
        return
    cats = []
    # accept comma separated or space separated
    joined = " ".join(args)
    parts = [p.strip() for p in joined.replace(",", " ").split() if p.strip()]
    # map short names to pool where possible
    for p in parts[:12]:
        match = next((c for c in ALL_CATEGORIES if c.lower().startswith(p.lower())), None)
        cats.append(match or p)
    if len(cats) < 1:
        await update.message.reply_text("Provide at least one category.")
        return
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
        "manual_accept": {}
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_md = user_mention_md(user.id, user.first_name)
    cat_lines = "\\n".join(f"- {escape_md_v2(c)}" for c in cats)
    text = (f"*Adedonha lobby created!*\\n\\nMode: *Custom*\\nCategories pool:\\n{cat_lines}\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info("Pin failed: %s", e)
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get("state") == "lobby":
            if len(g.get("players", {})) <= 1:
                try:
                    await context.bot.send_message(chat.id, "Lobby cancelled due to inactivity.")
                except Exception:
                    pass
                games.pop(chat.id, None)
    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

async def fast_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /fastadedonha cat1 cat2 cat3
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    args = context.args or []
    if args and len(args) >= 3:
        cats = []
        for a in args[:3]:
            match = next((c for c in ALL_CATEGORIES if c.lower().startswith(a.lower())), None)
            cats.append(match or a)
    else:
        cats = choose_random_categories(3)
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
        "manual_accept": {}
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_md = user_mention_md(user.id, user.first_name)
    cats_md = "\\n".join(f"- {escape_md_v2(c)}" for c in cats)
    text = (f"*Adedonha lobby created!*\\n\\nMode: *Fast*\\nFixed categories:\\n{cats_md}\\nTotal rounds: *{TOTAL_ROUNDS_FAST}*\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info("Pin failed: %s", e)
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get("state") == "lobby":
            if len(g.get("players", {})) <= 1:
                try:
                    await context.bot.send_message(chat.id, "Lobby cancelled due to inactivity.")
                except Exception:
                    pass
                games.pop(chat.id, None)
    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

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
    players_md = "\\n".join(user_mention_md(int(uid), name) for uid, name in g["players"].items())
    if g["mode"] == "classic":
        text = (f"*Adedonha lobby created!*\\n\\nMode: *Classic*\\nCategories per round: *{g['categories_per_round']}*\\nTotal rounds: *{TOTAL_ROUNDS_CLASSIC}*\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.")
    elif g["mode"] == "custom":
        cat_lines = "\\n".join(f"- {escape_md_v2(c)}" for c in g.get("categories_pool", []))
        text = (f"*Adedonha lobby created!*\\n\\nMode: *Custom*\\nCategories pool:\\n{cat_lines}\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.")
    else:
        cats_md = "\\n".join(f"- {escape_md_v2(c)}" for c in g.get("fixed_categories", []))
        text = (f"*Adedonha lobby created!*\\n\\nMode: *Fast*\\nFixed categories:\\n{cats_md}\\nTotal rounds: *{TOTAL_ROUNDS_FAST}*\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.")
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="MarkdownV2", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],[InlineKeyboardButton("Start ▶️", callback_data="start_game")],[InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]]))
    except Exception:
        await context.bot.send_message(chat_id, f"{user_mention_md(user.id, user.first_name)} joined the lobby.", parse_mode="MarkdownV2")

async def joingame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # delete user's command to keep chat clean
    try:
        await update.message.delete()
    except Exception:
        pass
    await join_callback(update, context, by_command=True)

# ---------------- MODE INFO ----------------
async def mode_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # works in lobby and during game
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
        text = (f"*Classic Adedonha*\\nEach round selects *{num}* categories randomly from 12.\\nIf no one submits, the round ends after 3 minutes. After the first submission others have 2 seconds to submit. Total rounds: {TOTAL_ROUNDS_CLASSIC}.")
    elif mode == "custom":
        pool = g.get("categories_pool", [])
        pool_md = "\\n".join(f"- {escape_md_v2(c)}" for c in pool)
        text = (f"*Custom Adedonha*\\nCategories pool for this game:\\n{pool_md}\\nEach round selects {len(pool)} categories randomly from the pool. Timing: same as Classic.")
    else:
        cats = g.get("fixed_categories", [])
        cats_md = "\\n".join(f"- {escape_md_v2(c)}" for c in cats)
        text = (f"*Fast Adedonha*\\nFixed categories:\\n{cats_md}\\nEach round is {FAST_ROUND_SECONDS} seconds total. Total rounds: {TOTAL_ROUNDS_FAST}. First submission gives 2s immediate window.")
    await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")

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
    asyncio.create_task(run_game(chat_id, context))

# ---------------- RUN GAME (main loop) ----------------
async def run_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return
    mode = g["mode"]
    if mode == "classic":
        rounds = TOTAL_ROUNDS_CLASSIC
        per_round = g.get("categories_per_round", 5)
    elif mode == "custom":
        rounds = TOTAL_ROUNDS_CLASSIC
        pool = g.get("categories_pool", ALL_CATEGORIES)
        per_round = min(len(pool), max(1, len(pool)))
    else:
        rounds = TOTAL_ROUNDS_FAST
        per_round = 3
    # initialize scores
    g["scores"] = {uid: 0 for uid in g["players"].keys()}
    # update DB games played
    db_update_after_game(list(g["players"].keys()))
    for r in range(1, rounds + 1):
        g["round"] = r
        if mode == "classic":
            categories = choose_random_categories(per_round)
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        elif mode == "custom":
            pool = g.get("categories_pool", ALL_CATEGORIES)
            categories = random.sample(pool, min(per_round, len(pool)))
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        else:  # fast
            categories = g.get("fixed_categories", choose_random_categories(3))
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = FAST_FIRST_WINDOW
            no_submit_timeout = FAST_ROUND_SECONDS
            round_time_limit = FAST_ROUND_SECONDS
        g["current_categories"] = categories
        g["round_letter"] = letter
        g["submissions"] = {}
        g["manual_accept"] = {}
        # send round template (as plain text to avoid MarkdownV2 entity issues)
        cat_lines_plain = "\n".join(f"{i+1}. {c}:" for i, c in enumerate(categories))
        intro = (f"Round {r} / {rounds}\nLetter: {letter}\n\nSend your answers in ONE MESSAGE using this template (first {len(categories)} answers will be used):\n\n{cat_lines_plain}\n\n")
        intro += f"First submission starts a {window_seconds}s window for others (fast mode total round {FAST_ROUND_SECONDS}s)."
        await context.bot.send_message(chat_id, intro)
        # schedule no submit timeout
        end_event = asyncio.Event()
        first_submitter = None
        first_submit_time = None
        async def no_submit_worker():
            await asyncio.sleep(no_submit_timeout)
            if not g.get("submissions"):
                await context.bot.send_message(chat_id, f"⏱ Round {r} ended: no submissions. No penalties.")
                g["round_scores_history"] = g.get("round_scores_history", []) + [{}]
                end_event.set()
        no_submit_task = asyncio.create_task(no_submit_worker())
        # wait loop - submissions are collected via submission_handler
        while not end_event.is_set():
            await asyncio.sleep(0.5)
            if g.get("submissions") and not first_submitter:
                first_submitter = next(iter(g["submissions"].keys()))
                first_submit_time = datetime.utcnow()
                # announce (escaped)
                try:
                    await context.bot.send_message(chat_id, f"⏱ {user_mention_md(int(first_submitter), g['players'][first_submitter])} submitted first! Others have {window_seconds}s to submit.", parse_mode="MarkdownV2")
                except Exception:
                    await context.bot.send_message(chat_id, f"{g['players'][first_submitter]} submitted first! Others have {window_seconds}s to submit.")
                async def window_worker():
                    await asyncio.sleep(window_seconds)
                    end_event.set()
                asyncio.create_task(window_worker())
            if round_time_limit and first_submit_time:
                if (datetime.utcnow() - first_submit_time).total_seconds() >= round_time_limit:
                    end_event.set()
        # cancel no_submit_task
        try:
            no_submit_task.cancel()
        except Exception:
            pass
        # scoring
        submissions = g.get("submissions", {})
        if not submissions:
            continue
        parsed = {}
        for uid, txt in submissions.items():
            parsed[uid] = extract_answers_from_text(txt, len(categories))
        per_cat_freq = [ {} for _ in range(len(categories)) ]
        for idx in range(len(categories)):
            for uid, answers in parsed.items():
                a = answers[idx].strip()
                if a:
                    key = a.lower()
                    per_cat_freq[idx][key] = per_cat_freq[idx].get(key, 0) + 1
        round_scores = {}
        for uid, answers in parsed.items():
            pts = 0
            validated_count = 0
            submitted_any = any(a.strip() for a in answers)
            for idx, a in enumerate(answers):
                a_clean = a.strip()
                if not a_clean:
                    continue
                # letter check
                if a_clean[0].upper() != letter.upper():
                    continue
                valid = await ai_validate(categories[idx], a_clean, letter)
                # if AI unavailable, admin manual validation panel can set manual_accept flags to True/False
                if not valid:
                    # if manual_accept exists for this uid and True, treat as valid
                    man = g.get("manual_accept", {}).get(uid)
                    if man is True:
                        valid = True
                    else:
                        valid = False
                if not valid:
                    continue
                key = a_clean.lower()
                cnt = per_cat_freq[idx].get(key, 0)
                if cnt == 1:
                    pts += 10
                else:
                    pts += 5
                validated_count += 1
            round_scores[uid] = {"points": pts, "validated": validated_count, "submitted_any": submitted_any}
            g["scores"][uid] = g["scores"].get(uid, 0) + pts
            db_update_after_round(uid, validated_count, submitted_any)
        g["round_scores_history"] = g.get("round_scores_history", []) + [round_scores]
        # summary message
        header = f"*Round {r} Results*\\nLetter: *{escape_md_v2(letter)}*\\n\\n"
        body = ""
        sorted_players = sorted(g["players"].items(), key=lambda x: -g["scores"].get(x[0],0))
        for uid, name in sorted_players:
            pts = round_scores.get(uid, {}).get("points", 0)
            body += f"{user_mention_md(int(uid), name)} — `{pts}` pts\\n"
        try:
            await context.bot.send_message(chat_id, header + body, parse_mode="MarkdownV2")
        except Exception:
            await context.bot.send_message(chat_id, header + body.replace("`",""))
        await asyncio.sleep(1)
    # final leaderboard
    lb = sorted(g["scores"].items(), key=lambda x: -x[1])
    text = "*Game Over — Final Scores*\\n\\n"
    for uid, pts in lb:
        text += f"{user_mention_md(int(uid), g['players'][uid])} — `{pts}` pts\\n"
    await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")
    # cleanup
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
        await update.message.reply_text("You already submitted for this round.")
        return
    text = update.message.text or ""
    g["submissions"][uid] = text
    # if AI unavailable, create a single manual validation message with button (one message)
    if not ai_client:
        if not g.get("manual_validation_msg_id"):
            preview = ""
            for uid2, txt in g["submissions"].items():
                preview += f"{g['players'][uid2]}: {txt[:120]}\\n"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open validation panel ✅", callback_data="open_manual_validate")]])
            msg = await context.bot.send_message(chat.id, f"*Manual validation required*\\nAI not configured. Admins may validate via panel.\\n\\nSubmissions preview:\\n{escape_md_v2(preview)}", parse_mode="MarkdownV2", reply_markup=kb)
            g["manual_validation_msg_id"] = msg.message_id
    # normal flow: run_game waits and will score after window

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
        lbl = escape_md_v2(g["players"].get(uid, "Player"))
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
            name = escape_md_v2(g["players"].get(uid2, "Player"))
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
    else:
        await update.callback_query.answer("Unknown action.", show_alert=True)

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
    if g.get("lobby_task"):
        try:
            g["lobby_task"].cancel()
        except Exception:
            pass
    games.pop(chat.id, None)
    await update.message.reply_text("Game cancelled.")

# ---------------- CATEGORIES / MYSTATS / DUMP / RESET / LEADERBOARD ----------------
async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "*All possible categories (12):*\\n" + "\\n".join(f"{i+1}. {escape_md_v2(c)}" for i, c in enumerate(ALL_CATEGORIES))
    await update.message.reply_text(text, parse_mode="MarkdownV2")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    uid = str(target.id)
    s = db_get_stats(uid)
    all_rows = db_dump_all()
    rank = 1
    for idx, row in enumerate(all_rows, start=1):
        if row[0] == uid:
            rank = idx
            break
    text = (f"*Stats of {user_mention_md(int(uid), target.first_name)}*\\n\\n"
            f"• *Games played:* `{s.get('games_played',0)}`\\n"
            f"• *Total validated words:* `{s.get('total_validated_words',0)}`\\n"
            f"• *Wordlists sent:* `{s.get('total_wordlists_sent',0)}`\\n"
            f"• *Global position:* `{rank}`\\n")
    await update.message.reply_text(text, parse_mode="MarkdownV2")

async def dumpstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    rows = db_dump_all()
    header = "user_id,games_played,total_validated_words,total_wordlists_sent\\n"
    csv_path = "/tmp/stats_export.csv"
    with open(csv_path, "w", encoding="utf8") as f:
        f.write(header)
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\\n")
    # send formatted text first
    text = "*Stats export (top by validated words)*\\n\\n"
    for r in rows[:50]:
        text += f"{escape_md_v2(r[0])} — games:{r[1]} validated:{r[2]} lists:{r[3]}\\n"
    await update.message.reply_text(text, parse_mode="MarkdownV2")
    await update.message.reply_document(open(csv_path, "rb"))
    await update.message.reply_document(open(DB_FILE, "rb"))

async def statsreset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    db_reset_all()
    await update.message.reply_text("All stats reset to zero.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    rows = db_dump_all()
    top10 = rows[:10]
    text = "*Leaderboard — Top 10 (by validated words)*\\n\\n"
    for idx, r in enumerate(top10, start=1):
        text += f"{idx}. {escape_md_v2(r[0])} — validated:{r[2]} lists:{r[3]}\\n"
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ---------------- VALIDATE (admin-triggered manual validation) ----------------
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
    # open panel
    await open_manual_validate(update, context)

# ---------------- APP SETUP ----------------
def main():
    init_db()
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("YOUR_"):
        print("Please set TELEGRAM_BOT_TOKEN in the script before running.")
        return
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # lobby/start commands
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

    # submission handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submission_handler))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
