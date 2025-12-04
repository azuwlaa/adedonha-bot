# handlers.py — Telegram handlers for commands and callbacks
import asyncio
import logging
import re
import time
from datetime import datetime
from typing import List
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import html as _html

import config
from game import games, start_game, extract_answers_from_text
from db import init_db

logger = logging.getLogger(__name__)

def escape_html(text: str) -> str:
    if text is None:
        return ""
    return _html.escape(str(text), quote=False)

def user_mention_html(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{escape_html(name)}</a>'

# ---------------- LOBBIES ----------------

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
        "manual_accept": {},
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {config.PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Rounds: 6️⃣", callback_data="set_rounds:6"), InlineKeyboardButton("8️⃣", callback_data="set_rounds:8"), InlineKeyboardButton("🔟", callback_data="set_rounds:10"), InlineKeyboardButton("1️⃣2️⃣", callback_data="set_rounds:12")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_html = user_mention_html(user.id, user.first_name)
    text = (
        "<b>Adedonha lobby created!</b>\n\n"
        "Mode: <b>Classic</b>\n"
        f"Categories per round: <b>5</b>\n"
        f"Total rounds: <b>{config.TOTAL_ROUNDS_CLASSIC}</b>\n\n"
        f"Players:\n{players_html}\n\nPress Join to participate."
    )
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception:
        pass

    async def lobby_timeout():
        await asyncio.sleep(config.LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get("state") == "lobby":
            try:
                await context.bot.send_message(chat.id, "cancelling lobby..")
                await context.bot.send_message(chat.id, "Game cancelled due to inactivity")
            except Exception:
                pass
            games.pop(chat.id, None)

    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

async def custom_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Provide categories, e.g. /customadedonha Name Object Animal")
        return
    cats = [c.strip() for c in " ".join(args).replace(",", " ").split() if c.strip()]
    cats = cats[:12]
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
        "manual_accept": {},
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {config.PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Rounds: 6️⃣", callback_data="set_rounds:6"), InlineKeyboardButton("8️⃣", callback_data="set_rounds:8"), InlineKeyboardButton("🔟", callback_data="set_rounds:10"), InlineKeyboardButton("1️⃣2️⃣", callback_data="set_rounds:12")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_html = user_mention_html(user.id, user.first_name)
    cat_lines = "\n".join(f"- {escape_html(c)}" for c in cats)
    text = (
        "<b>Adedonha lobby created!</b>\n\n"
        "Mode: <b>Custom</b>\n"
        f"Categories pool:\n{cat_lines}\n\n"
        f"Players:\n{players_html}\n\nPress Join to participate."
    )
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception:
        pass

    async def lobby_timeout():
        await asyncio.sleep(config.LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get("state") == "lobby":
            try:
                await context.bot.send_message(chat.id, "cancelling lobby..")
                await context.bot.send_message(chat.id, "Game cancelled due to inactivity")
            except Exception:
                pass
            games.pop(chat.id, None)

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
        "manual_accept": {},
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {config.PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Rounds: 6️⃣", callback_data="set_rounds:6"), InlineKeyboardButton("8️⃣", callback_data="set_rounds:8"), InlineKeyboardButton("🔟", callback_data="set_rounds:10"), InlineKeyboardButton("1️⃣2️⃣", callback_data="set_rounds:12")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_html = user_mention_html(user.id, user.first_name)
    cats_md = "\n".join(f"- {escape_html(c)}" for c in cats)
    text = (
        "<b>Adedonha lobby created!</b>\n\n"
        "Mode: <b>Fast</b>\n"
        f"Fixed categories:\n{cats_md}\n"
        f"Total rounds: <b>{config.TOTAL_ROUNDS_FAST}</b>\n\n"
        f"Players:\n{players_html}\n\nPress Join to participate."
    )
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception:
        pass

    async def lobby_timeout():
        await asyncio.sleep(config.LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get("state") == "lobby":
            try:
                await context.bot.send_message(chat.id, "cancelling lobby..")
                await context.bot.send_message(chat.id, "Game cancelled due to inactivity")
            except Exception:
                pass
            games.pop(chat.id, None)

    lobby["lobby_task"] = asyncio.create_task(lobby_timeout())

# ---------------- JOIN ----------------

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

    if len(g["players"]) >= config.MAX_PLAYERS:
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
        text = (
            "<b>Adedonha lobby created!</b>\n\n"
            "Mode: <b>Classic</b>\n"
            f"Categories per round: <b>{g['categories_per_round']}</b>\n"
            f"Total rounds: <b>{config.TOTAL_ROUNDS_CLASSIC}</b>\n\n"
            f"Players:\n{players_html}\n\nPress Join to participate."
        )
    elif g["mode"] == "custom":
        cat_lines = "\n".join(f"- {escape_html(c)}" for c in g.get("categories_pool", []))
        text = (
            "<b>Adedonha lobby created!</b>\n\n"
            "Mode: <b>Custom</b>\n"
            f"Categories pool:\n{cat_lines}\n\n"
            f"Players:\n{players_html}\n\nPress Join to participate."
        )
    else:
        cats_md = "\n".join(f"- {escape_html(c)}" for c in g.get("fixed_categories", []))
        text = (
            "<b>Adedonha lobby created!</b>\n\n"
            "Mode: <b>Fast</b>\n"
            f"Fixed categories:\n{cats_md}\n"
            f"Total rounds: <b>{config.TOTAL_ROUNDS_FAST}</b>\n\n"
            f"Players:\n{players_html}\n\nPress Join to participate."
        )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {config.PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Rounds: 6️⃣", callback_data="set_rounds:6"), InlineKeyboardButton("8️⃣", callback_data="set_rounds:8"), InlineKeyboardButton("🔟", callback_data="set_rounds:10"), InlineKeyboardButton("1️⃣2️⃣", callback_data="set_rounds:12")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await context.bot.send_message(chat_id, f"{user_mention_html(user.id, user.first_name)} joined the lobby.", parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id, f"{user.first_name} joined the lobby.")

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
        text = (f"<b>Classic Adedonha</b>\nEach round uses the fixed 5 categories (Name, Object, Animal, Plant, Country).\nFirst submission gives {config.CLASSIC_FIRST_WINDOW}s window. Total rounds: {config.TOTAL_ROUNDS_CLASSIC}.")
    elif mode == "custom":
        pool = g.get("categories_pool", [])
        pool_html = "\n".join(f"- {escape_html(c)}" for c in pool)
        text = (f"<b>Custom Adedonha</b>\nCategories pool for this game:\n{pool_html}\nTiming: same as Classic.")
    else:
        cats = g.get("fixed_categories", [])
        cats_html = "\n".join(f"- {escape_html(c)}" for c in cats)
        text = (f"<b>Fast Adedonha</b>\nFixed categories:\n{cats_html}\nEach round is {config.FAST_ROUND_SECONDS} seconds total. Total rounds: {config.TOTAL_ROUNDS_FAST}.")
    await context.bot.send_message(chat_id, text, parse_mode="HTML")

# ---------------- START (button) ----------------

async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    await cq.answer()
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await context.bot.send_message(chat_id, "No lobby to start.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if user.id != g["creator_id"] and not is_admin and str(user.id) not in config.OWNERS:
        await context.bot.send_message(chat_id, "Only the creator, a chat admin, or owner can start the game.")
        return
    try:
        await context.bot.unpin_chat_message(chat_id)
    except Exception:
        pass
    if g.get("lobby_task"):
        try:
            g["lobby_task"].cancel()
        except Exception:
            pass
    g["state"] = "running"
    try:
        await context.bot.edit_message_reply_markup(chat_id, g["lobby_message_id"], reply_markup=None)
    except Exception:
        pass
    asyncio.create_task(start_game(chat_id, context))

# ---------------- START (command) ----------------

async def startgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = games.get(chat.id)
    if not g or g.get("state") != "lobby":
        await update.message.reply_text("There is no lobby waiting to start.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if user.id != g["creator_id"] and not is_admin and str(user.id) not in config.OWNERS:
        await update.message.reply_text("Only the creator, an admin, or bot owner can start the game.")
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
    if g.get("lobby_message_id"):
        try:
            await context.bot.edit_message_reply_markup(chat.id, g["lobby_message_id"], reply_markup=None)
        except Exception:
            pass
    g["state"] = "running"
    await update.message.reply_text("Starting game…")
    asyncio.create_task(start_game(chat.id, context))

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
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answer_lines = 0
    for ln in lines:
        if ':' in ln:
            answer_lines += 1
        elif re.match(r'^[0-9]+\.', ln):
            answer_lines += 1
    needed = len(g.get('current_categories', [])) or g.get('categories_per_round', 0)
    if answer_lines < needed:
        try:
            await update.message.reply_text(f"⚠️ Please send a complete list with all {needed} categories, one per line (use the lobby template).")
        except Exception:
            pass
        return
    g['submissions'][uid] = text

# ---------------- MANUAL VALIDATION PANEL (simplified) ----------------

async def run_manual_validation_panel(chat_id: int, context):
    """Optional admin panel - left minimal for now."""
    g = games.get(chat_id)
    if not g:
        return
    preview = "\n\n".join(f"{g['players'].get(uid, uid)}: {txt[:120]}" for uid, txt in g.get('submissions', {}).items())
    try:
        await context.bot.send_message(chat_id, f"Manual validation required:\n\n{preview}")
    except Exception:
        pass

# ---------------- CALLBACK ROUTER ----------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = (update.callback_query.data or "")
    if data == "join_lobby":
        await join_callback(update, context)
    elif data == "start_game":
        await start_game_callback(update, context)
    elif data == "mode_info":
        await mode_info_callback(update, context)
    else:
        try:
            await update.callback_query.answer("Unknown action.", show_alert=False)
        except Exception:
            pass

    # rounds setter
    if data.startswith("set_rounds:"):
        try:
            n = int(data.split(":",1)[1])
            chat = update.effective_chat
            g = games.get(chat.id)
            if g and g.get("state") == "lobby":
                g[\"rounds_total\"] = n
                await update.callback_query.edit_message_text(f\"✅ Total rounds set to {n}.\")
            else:
                await update.callback_query.answer(\"No lobby to set rounds.\", show_alert=True)
        except Exception:
            await update.callback_query.answer(\"Invalid rounds.\", show_alert=True)
        return

# ---------------- RUNINFO ----------------

async def runinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if str(user.id) not in config.OWNERS:
        await update.message.reply_text("Only bot owners can use /runinfo.")
        return
    now = time.time()
    uptime_seconds = int(now - config.START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    if days > 0:
        uptime_str = f"{days}.{hours:02}:{minutes:02}:{seconds:02}"
    else:
        uptime_str = f"{hours:02}:{minutes:02}:{seconds:02}"
    active_games = 0
    total_players = 0
    for chat_id, g in games.items():
        if g.get("state") in ("lobby", "running"):
            active_games += 1
            total_players += len(g.get("players", {}))
    text = (
        "<b>Run information</b>\n"
        f"Uptime: <code>{uptime_str}</code>\n"
        f"Current Games: <b>{active_games}</b>\n"
        f"Current Players: <b>{total_players}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ---------------- GAME CANCEL ----------------

async def gamecancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = games.get(chat.id)
    if not g:
        await update.message.reply_text("No active game or lobby to cancel.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if user.id != g["creator_id"] and not is_admin and str(user.id) not in config.OWNERS:
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
