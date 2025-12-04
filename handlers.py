# handlers.py — Telegram handlers and callback router
import asyncio, html, re, json, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import config, game, db, ai_validation

log = logging.getLogger(__name__)

async def new_lobby_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new lobby with round-selection buttons (6..12)"""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == "private":
        await update.message.reply_text("This command works in groups only.")
        return
    # prevent multiple games/lobbies
    existing = game.games.get(chat.id) or db.get_game(chat.id)
    if existing and existing.get("state") in ("lobby", "running"):
        await update.message.reply_text(f"{config.EMOJI_GAME} A game or lobby is already active in this group.")
        return
    rounds = config.DEFAULT_ROUNDS
    # build keyboard for round selection
    kb = []
    row = []
    for r in config.ROUND_OPTIONS:
        row.append(InlineKeyboardButton(str(r), callback_data=f"setrounds:{r}"))
        if len(row) >= 4:
            kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("Join ✅", callback_data="join_lobby"),
               InlineKeyboardButton("Start ▶️", callback_data="start_game")])
    text = f"{config.EMOJI_GAME} New lobby by {html.escape(user.first_name)}\nRounds: {rounds}\nChoose rounds and join!\n{config.EMOJI_INFO} Each round is {config.ROUND_DURATION_SECONDS//60} minutes."
    msg = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    # create lobby in game engine
    g = game.new_lobby(chat.id, user.id, user.first_name, rounds)
    g["lobby_message_id"] = msg.message_id
    db.set_game(chat.id, g)

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat = query.message.chat
    user = query.from_user
    # load game
    g = game.games.get(chat.id) or db.get_game(chat.id)
    if data.startswith("setrounds:"):
        rounds = int(data.split(":",1)[1])
        if not g:
            await query.message.reply_text("No lobby found.")
            return
        g["rounds_total"] = rounds
        db.set_game(chat.id, g)
        await query.message.edit_text(f"{config.EMOJI_GAME} Lobby updated — rounds set to {rounds}")
        return
    if data == "join_lobby":
        ok,msg = game.join_lobby(chat.id, user.id, user.first_name)
        await query.message.reply_text(f"{user.first_name}: {msg}")
        return
    if data == "start_game":
        if not g:
            await query.message.reply_text("No lobby to start.")
            return
        ok,res = game.start_game(chat.id)
        if not ok:
            await query.message.reply_text("Unable to start game.")
            return
        g = res
        # announce round and template
        letter = g.get("letter","A")
        cats = "\n".join([f"- {c}" for c in game.DEFAULT_CATEGORIES[:g.get('categories_per_round', config.CATEGORIES_PER_ROUND)]])
        template = config.ROUND_TEMPLATE.format(letter=letter, cats=cats)
        await query.message.reply_text(f"{config.EMOJI_GAME} Game started! Round {g['round_current']}/{g['rounds_total']}\n{template}", parse_mode="Markdown")
        # schedule round timeout runner
        # ensure only one runner per game
        if g.get("lobby_task") is None:
            g["lobby_task"] = True
            db.set_game(chat.id, g)
            asyncio.create_task(round_timeout_runner(chat.id, context.bot))
        return

async def round_timeout_runner(chat_id: int, bot):
    """Wait for ROUND_DURATION_SECONDS, then validate and advance."""
    await asyncio.sleep(config.ROUND_DURATION_SECONDS)
    # before validating, ensure game still running
    g = game.games.get(chat_id) or db.get_game(chat_id)
    if not g or g.get("state") != "running":
        return
    await bot.send_message(chat_id, f"{config.EMOJI_CLOCK} Time's up! {config.EMOJI_VALIDATE} AI validating answers...")
    round_results = await game.advance_round(chat_id, bot)
    # Send summarized results and updated scores
    scores = game.get_scores(chat_id)
    if round_results is None:
        await bot.send_message(chat_id, f"{config.EMOJI_SUCCESS} Round processed.")
    else:
        # build results text
        lines = []
        for pid, res in round_results.items():
            name = g.get("players",{}).get(pid,{}).get("name","Unknown")
            lines.append(f"{name}: +{res.get('points',0)} pts")
        await bot.send_message(chat_id, f"{config.EMOJI_SUCCESS} Round {g.get('round_current',0)-1} results:\n" + "\n".join(lines))
    # send updated scores
    stext = "\n".join([f"{v['name']}: {v['score']}" for k,v in scores.items()])
    await bot.send_message(chat_id, f"{config.EMOJI_SUCCESS} Current scores:\n{stext}")
    # check if finished
    g = game.games.get(chat_id) or db.get_game(chat_id)
    if g and g.get("state") == "finished":
        await bot.send_message(chat_id, f"{config.EMOJI_GAME} Game finished! Final scores:\n{stext}")
        game.cancel_game(chat_id)
        return
    # start next round runner
    asyncio.create_task(round_timeout_runner(chat_id, bot))

async def submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user submissions during running game. Enforce complete lists, validate and award points."""
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text or ""
    g = game.games.get(chat.id) or db.get_game(chat.id)
    if not g or g.get("state") != "running":
        return
    # parse answers
    answers = game.extract_answers_from_text(text, categories_per_round=g.get("categories_per_round"))
    # check completeness
    missing = [k for k,v in answers.items() if not v]
    if missing:
        await update.message.reply_text(f"Please submit a complete list of {len(answers)} answers (one per line). ✏️\nMissing: {', '.join(missing)}")
        return
    # save submission
    r = g.get("round_current",1)
    pid = str(user.id)
    g.setdefault("submissions", {}).setdefault(str(r), {})[pid] = answers
    db.set_game(chat.id, g)
    # send validating message and then edit with result
    status_msg = await update.message.reply_text(f"{config.EMOJI_VALIDATE} AI validating... Checking your list. Please wait...")
    # perform validation (synchronously here)
    res = ai_validation.batch_validate(g.get("letter",""), answers)
    # compute points and details
    points = 0
    details = []
    for cat, info in res.items():
        mark = config.EMOJI_SUCCESS if info.get("valid") else "❌"
        details.append(f"{mark} {cat}: {info.get('word')} ({info.get('reason')})")
        if info.get("valid"):
            points += config.POINTS_PER_VALID
    # award points to player immediately (so scores show up in live scoreboard)
    if pid in g.get("players",{}):
        g["players"][pid]["score"] = g["players"][pid].get("score",0) + points
    db.set_game(chat.id, g)
    # edit status message with summary
    await status_msg.edit_text(f"{config.EMOJI_VALIDATE} AI validation complete!\nPoints earned: {points}\n" + "\n".join(details))
    # send updated score board
    scores = game.get_scores(chat.id)
    stext = "\n".join([f"{v['name']}: {v['score']}" for k,v in scores.items()])
    await update.message.reply_text(f"{config.EMOJI_SUCCESS} Current scores:\n{stext}")

async def gamecancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    game.cancel_game(chat.id)
    await update.message.reply_text(f"{config.EMOJI_GAME} Game/lobby cancelled and removed.")
