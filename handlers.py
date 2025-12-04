# handlers.py - command and callback handlers
import asyncio
import re
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from .utils import (
    games,
    escape_html,
    user_mention_html,
    PLAYER_EMOJI,
    ALL_CATEGORIES,
    MAX_PLAYERS,
    CLASSIC_FIRST_WINDOW,
    CLASSIC_NO_SUBMIT_TIMEOUT,
    FAST_FIRST_WINDOW,
    FAST_ROUND_SECONDS,
    TOTAL_ROUNDS_CLASSIC,
    TOTAL_ROUNDS_FAST,
    OWNERS,
)
from .database import db_update_after_round, db_update_after_game, db_get_stats, db_dump_all, db_reset_all
from .ai import ai_validate
from . import game as game_module

# ---------------- HELPERS ----------------
def is_owner(uid: int) -> bool:
    return str(uid) in OWNERS

# ---------------- COMMANDS / LOBBY ----------------
async def classic_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    num = 5
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
        "manual_accept": {}
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_html = user_mention_html(user.id, user.first_name)
    text = (f"<b>Adedonha lobby created!</b>\n\nMode: <b>Classic</b>\nCategories per round: <b>{num}</b>\nTotal rounds: <b>{TOTAL_ROUNDS_CLASSIC}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception:
        pass
    async def lobby_timeout():
        await asyncio.sleep(CLASSIC_NO_SUBMIT_TIMEOUT)
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
    joined = " ".join(args)
    parts = [p.strip() for p in joined.replace(",", " ").split() if p.strip()]
    for p in parts[:12]:
        cats.append(p)
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
    players_html = user_mention_html(user.id, user.first_name)
    cat_lines = "\n".join(f"- {escape_html(c)}" for c in cats)
    text = (f"<b>Adedonha lobby created!</b>\n\nMode: <b>Custom</b>\nCategories pool:\n{cat_lines}\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception:
        pass
    async def lobby_timeout():
        await asyncio.sleep(CLASSIC_NO_SUBMIT_TIMEOUT)
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
        "manual_accept": {}
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],
        [InlineKeyboardButton("Start ▶️", callback_data="start_game")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]
    ])
    players_html = user_mention_html(user.id, user.first_name)
    cats_md = "\n".join(f"- {escape_html(c)}" for c in cats)
    text = (f"<b>Adedonha lobby created!</b>\n\nMode: <b>Fast</b>\nFixed categories:\n{cats_md}\nTotal rounds: <b>{TOTAL_ROUNDS_FAST}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    lobby["lobby_message_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception:
        pass
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
async def join_callback(update, context, by_command: bool = False):
    if update.callback_query:
        cq = update.callback_query
        chat_id = cq.message.chat.id
        user = cq.from_user
        try:
            await cq.answer()
        except Exception:
            pass
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
        text = (f"<b>Adedonha lobby created!</b>\n\nMode: <b>Classic</b>\nCategories per round: <b>{g['categories_per_round']}</b>\nTotal rounds: <b>{TOTAL_ROUNDS_CLASSIC}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    elif g["mode"] == "custom":
        cat_lines = "\n".join(f"- {escape_html(c)}" for c in g.get("categories_pool", []))
        text = (f"<b>Adedonha lobby created!</b>\n\nMode: <b>Custom</b>\nCategories pool:\n{cat_lines}\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    else:
        cats_md = "\n".join(f"- {escape_html(c)}" for c in g.get("fixed_categories", []))
        text = (f"<b>Adedonha lobby created!</b>\n\nMode: <b>Fast</b>\nFixed categories:\n{cats_md}\nTotal rounds: <b>{TOTAL_ROUNDS_FAST}</b>\n\nPlayers:\n{players_html}\n\nPress Join to participate.")
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g["lobby_message_id"], parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Join {PLAYER_EMOJI}", callback_data="join_lobby")],[InlineKeyboardButton("Start ▶️", callback_data="start_game")],[InlineKeyboardButton("Mode Info ℹ️", callback_data="mode_info")]]))
    except Exception:
        await context.bot.send_message(chat_id, f"{user_mention_html(user.id, user.first_name)} joined the lobby.", parse_mode="HTML")

async def joingame_command(update, context):
    try:
        await update.message.delete()
    except Exception:
        pass
    await join_callback(update, context, by_command=True)

# ---------------- MODE INFO ----------------
async def mode_info_callback(update, context):
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
        text = (f"<b>Classic Adedonha</b>\nEach round uses the fixed 5 categories (Name, Object, Animal, Plant, Country).\nIf no one submits, the round ends after 3 minutes. After the first submission others have 2 seconds to submit. Total rounds: {TOTAL_ROUNDS_CLASSIC}.")
    elif mode == "custom":
        pool = g.get("categories_pool", [])
        pool_html = "\n".join(f"- {escape_html(c)}" for c in pool)
        text = (f"<b>Custom Adedonha</b>\nCategories pool for this game:\n{pool_html}\nThis game uses exactly the categories provided when creating the custom game (no randomization). Timing: same as Classic.")
    else:
        cats = g.get("fixed_categories", [])
        cats_html = "\n".join(f"- {escape_html(c)}" for c in cats)
        text = (f"<b>Fast Adedonha</b>\nFixed categories:\n{cats_html}\nEach round is {FAST_ROUND_SECONDS} seconds total. Total rounds: {TOTAL_ROUNDS_FAST}. First submission gives 2s immediate window.")
    await context.bot.send_message(chat_id, text, parse_mode="HTML")

# ---------------- START GAME (button only starts game) ----------------
async def start_game_callback(update, context):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    await cq.answer()
    g = games.get(chat_id)
    if not g or g.get("state") != "lobby":
        await context.bot.send_message(chat_id, "No lobby to start.")
        return
    # only allow creator or chat admin to start
    is_admin = False
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
    asyncio.create_task(game_module.run_game(chat_id, context))

# ---------------- SUBMISSIONS ----------------
async def submission_handler(update, context):
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
    # Strict submission detection: require at least N answer lines where N = number of categories this round.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answer_lines = 0
    for ln in lines:
        if ':' in ln:
            answer_lines += 1
        elif re.match(r'^[0-9]+\.', ln):
            answer_lines += 1
    needed = len(g.get('current_categories', [])) or g.get('categories_per_round', 0)
    if answer_lines < needed:
        # not considered a submission (chat message) — ignore silently
        return
    # register the submission (only first valid message per player counted)
    g['submissions'][uid] = text
    # if AI unavailable, create a single manual validation message with button (one message)
    from .ai import ai_client  # local import to avoid circular issues
    if not ai_client:
        if not g.get('manual_validation_msg_id'):
            preview = ''
            for uid2, txt in g['submissions'].items():
                preview += f"{g['players'][uid2]}: {txt[:120]}\n"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open validation panel ✅", callback_data="open_manual_validate")]])
            msg_text = f"<b>Manual validation required</b>\nAI not configured. Admins may validate via panel.\n\nSubmissions preview:\n{escape_html(preview)}"
            msg = await context.bot.send_message(chat.id, msg_text, parse_mode="HTML", reply_markup=kb)
            g['manual_validation_msg_id'] = msg.message_id

# ---------------- MANUAL VALIDATION PANEL ----------------
async def open_manual_validate(update, context):
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

async def validation_button_handler(update, context):
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
async def callback_router(update, context):
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
async def gamecancel_command(update, context):
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
async def categories_command(update, context):
    text = "<b>All possible categories (14):</b>\n" + "\n".join(f"{i+1}. {escape_html(c)}" for i, c in enumerate(ALL_CATEGORIES))
    await update.message.reply_text(text, parse_mode="HTML")

async def mystats_command(update, context):
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

async def dumpstats_command(update, context):
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
    await update.message.reply_document(open("stats.db", "rb"))

async def statsreset_command(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("Only bot owner can use this command.")
        return
    await db_reset_all()
    await update.message.reply_text("All stats reset to zero.")

async def leaderboard_command(update, context):
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

async def runinfo_command(update, context):
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

async def validate_command(update, context):
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
