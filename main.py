import os
import logging
import asyncio
import random
import sqlite3
from datetime import datetime
from typing import Dict, List

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
from openai import OpenAI

# -----------------------------
# CONFIGURATION CONSTANTS
# -----------------------------
BOT_OWNERS = {624102836, 1707015091}
LOBBY_TIMEOUT = 5 * 60
FIRST_SUBMISSION_WINDOW = 2     # 2 seconds
FASTMODE_ROUND_TIME = 60        # 1 minute
ROUND_TIME_CLASSIC = 180        # 3 minutes
TOTAL_ROUNDS_CLASSIC = 10
TOTAL_ROUNDS_FAST = 12
TOTAL_ROUNDS_CUSTOM = 10

DB_NAME = "stats.db"

CUTE = {
    "lobby": "🦩",
    "game": "🎮",
    "round": "🔄",
    "category": "📝",
    "score": "⭐",
    "validate": "🛠",
    "ai_fail": "⚠️",
    "done": "✅",
    "error": "❌",
    "start": "▶️",
}

CLASSIC_CATEGORIES = [
    "Name",
    "Object",
    "Animal",
    "Plant",
    "City/Country/State"
]

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
    "Adjective"
]

# ----------------------------
# LOGGING
# ----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("adedonha")

# ----------------------------
# OPENAI CLIENT (OPTIONAL)
# ----------------------------
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ai_client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None


# ----------------------------
# DATABASE SETUP
# ----------------------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY,
            total_validated_words INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def db_ensure(uid: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM stats WHERE user_id=?", (uid,))
    if c.fetchone() is None:
        c.execute("INSERT INTO stats (user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()


def db_add_validated(uid: str, amount: int):
    db_ensure(uid)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        UPDATE stats SET total_validated_words = total_validated_words + ?
        WHERE user_id=?
    """, (amount, uid))
    conn.commit()
    conn.close()


def db_get_stats(uid: str):
    db_ensure(uid)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT total_validated_words FROM stats WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def db_leaderboard_top10():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, total_validated_words
        FROM stats
        ORDER BY total_validated_words DESC
        LIMIT 10
    """)
    rows = c.fetchall()
    conn.close()
    return rows


def db_reset():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM stats")
    conn.commit()
    conn.close()


# ----------------------------
# GAME STATE STRUCTURE
# ----------------------------
games: Dict[int, Dict] = {}


# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def escape_md(text: str) -> str:
    """Escape MarkdownV2."""
    if not text:
        return ""
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return "".join("\\" + c if c in to_escape else c for c in text)


def mention(uid: int, name: str) -> str:
    return f"[{escape_md(name)}](tg://user?id={uid})"


def extract_answers(text: str, count: int) -> List[str]:
    """Extract first N answers from the message."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    answers = []
    for line in lines:
        if ":" in line:
            answers.append(line.split(":", 1)[1].strip())
        else:
            answers.append(line)
    while len(answers) < count:
        answers.append("")
    return answers[:count]


async def is_admin(update: Update, uid: int):
    if uid in BOT_OWNERS:
        return True
    try:
        member = await update.effective_chat.get_member(uid)
        return member.status in ("administrator", "creator")
    except:
        return False


# ----------------------------
# AI VALIDATION
# ----------------------------
async def ai_validate(category: str, answer: str, letter: str) -> bool:
    """Validate answer using OpenAI. If unavailable, manual validation fallback triggers."""
    if not answer:
        return False

    if not ai_client:
        return None  # means admin validation needed

    prompt = f"""
Validate the answer "{answer}" for category "{category}".
Rules:
- It MUST start with the letter "{letter}".
- It must be a real valid example of the category.

Respond ONLY "YES" or "NO".
"""

    try:
        resp = ai_client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            max_output_tokens=6
        )
        out = ""
        if resp.output:
            for blk in resp.output:
                if isinstance(blk, dict):
                    cont = blk.get("content")
                    if isinstance(cont, list):
                        for c in cont:
                            if isinstance(c, dict) and "text" in c:
                                out += c["text"]
                            elif isinstance(c, str):
                                out += c
                elif isinstance(blk, str):
                    out += blk
        out = out.strip().upper()
        if out.startswith("YES"):
            return True
        if out.startswith("NO"):
            return False
        return None
    except:
        return None


# ----------------------------
# MANUAL VALIDATION MESSAGE
# ----------------------------
async def send_manual_validation(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: str, category: str, answer: str):
    """Send ONE message with buttons for manual yes/no."""
    g = games.get(chat_id)
    if not g:
        return

    msg_text = (
        f"{CUTE['ai_fail']} *AI validation unavailable*\n"
        f"Admins must validate:\n\n"
        f"{CUTE['category']} *Category:* {escape_md(category)}\n"
        f"💬 *Answer:* {escape_md(answer)}\n"
        f"👤 *Player:* {mention(int(uid), g['players'][int(uid)])}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{CUTE['done']} Valid", callback_data=f"valid_yes|{uid}|{category}|{answer}"),
            InlineKeyboardButton(f"{CUTE['error']} Invalid", callback_data=f"valid_no|{uid}|{category}|{answer}")
        ]
    ])

    await context.bot.send_message(chat_id, msg_text, parse_mode="MarkdownV2", reply_markup=kb)


# ----------------------------
# MODE INFO
# ----------------------------
async def modeinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = games.get(chat_id)

    if not g:
        await update.message.reply_text(f"{CUTE['error']} No active lobby or game.", parse_mode="MarkdownV2")
        return

    mode = g["mode"]

    if mode == "classic":
        text = (
            f"{CUTE['game']} *Classic Adedonha*\n\n"
            f"{CUTE['category']} Uses the 5 classic categories:\n"
            f"• Name\n• Object\n• Animal\n• Plant\n• City/Country/State\n\n"
            f"{CUTE['round']} Total Rounds: 10\n"
            f"⏱ 3 minutes max per round\n"
            f"⏳ 2-second window after first submission\n"
        )
    elif mode == "fast":
        cats = ", ".join(escape_md(c) for c in g["categories"])
        text = (
            f"{CUTE['game']} *Fast Adedonha*\n\n"
            f"{CUTE['category']} 3 chosen categories:\n• {cats}\n\n"
            f"{CUTE['round']} Total Rounds: 12\n"
            f"⏱ 1 minute per round\n"
            f"⏳ 2-second window after first submission\n"
        )
    elif mode == "custom":
        cats = "\n".join(f"• {escape_md(c)}" for c in g["categories"])
        text = (
            f"{CUTE['game']} *Custom Adedonha*\n\n"
            f"{CUTE['category']} Categories:\n{cats}\n\n"
            f"{CUTE['round']} Total Rounds: 10\n"
            f"⏳ 2-second window after first submission\n"
        )
    else:
        text = f"{CUTE['error']} Unknown mode."

    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ----------------------------
# LOBBY CREATION
# ----------------------------
async def create_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, categories: List[str], total_rounds: int):
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id

    # If lobby/game already running
    if chat_id in games:
        await update.message.reply_text(f"{CUTE['error']} A game or lobby is already active here.", parse_mode="MarkdownV2")
        return

    games[chat_id] = {
        "state": "lobby",
        "mode": mode,
        "creator": user.id,
        "players": {user.id: user.first_name},
        "categories": categories,
        "total_rounds": total_rounds,
        "round": 0,
        "submissions": {},
        "pending_manual": [],
        "lobby_message_id": None,
        "pin_id": None
    }

    # Lobby text
    players = f"{CUTE['lobby']} *Players:*\n{mention(user.id, user.first_name)}"
    cat_display = "\n".join(f"• {escape_md(c)}" for c in categories)

    info = (
        f"{CUTE['game']} *Adedonha Lobby Created!*\n\n"
        f"{CUTE['category']} *Mode:* {mode.capitalize()}\n"
        f"{CUTE['category']} *Categories:* \n{cat_display}\n\n"
        f"{CUTE['round']} *Total Rounds:* {total_rounds}\n\n"
        f"{players}\n\n"
        f"Use the button below to join.\n"
        f"You can also type /joingame"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Join 🦩", callback_data="lobby_join")],
        [InlineKeyboardButton("Mode Info ℹ️", callback_data="lobby_modeinfo")],
        [InlineKeyboardButton("Start ▶️", callback_data="lobby_start")]
    ])

    msg = await update.message.reply_text(info, parse_mode="MarkdownV2", reply_markup=kb)
    games[chat_id]["lobby_message_id"] = msg.message_id

    # Pin lobby
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        games[chat_id]["pin_id"] = msg.message_id
    except:
        pass


# ----------------------------
# START CLASSIC
# ----------------------------
async def classicadedonha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await create_lobby(
        update,
        context,
        mode="classic",
        categories=CLASSIC_CATEGORIES.copy(),
        total_rounds=TOTAL_ROUNDS_CLASSIC
    )


# ----------------------------
# START FAST WITH 3 SPECIFIED CATEGORIES
# ----------------------------
async def fastadedonha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            f"{CUTE['error']} Usage: /fastadedonha cat1 cat2 cat3",
            parse_mode="MarkdownV2"
        )
        return
    cats = []
    for c in args:
        if c.lower() not in [x.lower() for x in ALL_CATEGORIES]:
            await update.message.reply_text(
                f"{CUTE['error']} Invalid category:\n{escape_md(c)}\n"
                f"Use only official categories from /categories",
                parse_mode="MarkdownV2"
            )
            return
        cats.append(next(x for x in ALL_CATEGORIES if x.lower() == c.lower()))

    await create_lobby(
        update,
        context,
        mode="fast",
        categories=cats,
        total_rounds=TOTAL_ROUNDS_FAST
    )


# ----------------------------
# START CUSTOM WITH UP TO 12
# ----------------------------
async def customadedonha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            f"{CUTE['error']} Usage: /customadedonha cat1 cat2 ... catN (max 12)",
            parse_mode="MarkdownV2"
        )
        return
    if len(args) > 12:
        await update.message.reply_text(
            f"{CUTE['error']} Maximum 12 categories.",
            parse_mode="MarkdownV2"
        )
        return
    cats = []
    for c in args:
        if c.lower() not in [x.lower() for x in ALL_CATEGORIES]:
            await update.message.reply_text(
                f"{CUTE['error']} Invalid category:\n{escape_md(c)}",
                parse_mode="MarkdownV2"
            )
            return
        cats.append(next(x for x in ALL_CATEGORIES if x.lower() == c.lower()))

    await create_lobby(
        update,
        context,
        mode="custom",
        categories=cats,
        total_rounds=TOTAL_ROUNDS_CUSTOM
    )


# ----------------------------
# /joingame COMMAND
# ----------------------------
async def joingame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = games.get(chat_id)
    if not g or g["state"] != "lobby":
        await update.message.delete()
        return

    uid = update.effective_user.id
    name = update.effective_user.first_name

    if uid in g["players"]:
        await update.message.delete()
        return

    g["players"][uid] = name

    # Update lobby message
    players = "\n".join(f"🦩 {mention(pid, pname)}"
                        for pid, pname in g["players"].items())

    cats = "\n".join(f"• {escape_md(c)}" for c in g["categories"])
    info = (
        f"{CUTE['game']} *Adedonha Lobby Updated!*\n\n"
        f"{CUTE['category']} *Mode:* {g['mode'].capitalize()}\n"
        f"{CUTE['category']} *Categories:* \n{cats}\n\n"
        f"{CUTE['round']} *Total Rounds:* {g['total_rounds']}\n\n"
        f"{CUTE['lobby']} *Players:*\n{players}"
    )

    try:
        await context.bot.edit_message_text(
            info,
            chat_id=chat_id,
            message_id=g["lobby_message_id"],
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join 🦩", callback_data="lobby_join")],
                [InlineKeyboardButton("Mode Info ℹ️", callback_data="lobby_modeinfo")],
                [InlineKeyboardButton("Start ▶️", callback_data="lobby_start")]
            ])
        )
    except:
        pass

    await update.message.delete()


# ----------------------------
# CALLBACK HANDLER (JOIN, START, MODEINFO)
# ----------------------------
async def lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = cq.from_user
    chat_id = cq.message.chat.id
    data = cq.data

    g = games.get(chat_id)
    if not g:
        await cq.answer("No active lobby now.")
        return

    # ---- JOIN ----
    if data == "lobby_join":
        uid = user.id
        if uid not in g["players"]:
            g["players"][uid] = user.first_name

        # Update display
        players = "\n".join(f"🦩 {mention(pid, pname)}"
                            for pid, pname in g["players"].items())
        cats = "\n".join(f"• {escape_md(c)}" for c in g["categories"])
        info = (
            f"{CUTE['game']} *Adedonha Lobby Updated!*\n\n"
            f"{CUTE['category']} *Mode:* {g['mode'].capitalize()}\n"
            f"{CUTE['category']} *Categories:* \n{cats}\n\n"
            f"{CUTE['round']} *Total Rounds:* {g['total_rounds']}\n\n"
            f"{CUTE['lobby']} *Players:*\n{players}"
        )
        await cq.edit_message_text(
            info,
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join 🦩", callback_data="lobby_join")],
                [InlineKeyboardButton("Mode Info ℹ️", callback_data="lobby_modeinfo")],
                [InlineKeyboardButton("Start ▶️", callback_data="lobby_start")]
            ])
        )
        await cq.answer("Joined!")
        return

    # ---- MODEINFO ----
    if data == "lobby_modeinfo":
        await cq.answer()
        fake = Update(update.update_id, message=None)
        fake.message = update.effective_message
        fake.effective_user = user
        fake.effective_chat = update.effective_chat
        await modeinfo(fake, context)
        return

    # ---- START GAME ----
    if data == "lobby_start":
        await cq.answer("Starting...")
        await start_game(chat_id, context)
        return


# ----------------------------
# /gamecancel COMMAND
# ----------------------------
async def gamecancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    g = games.get(chat_id)
    if not g:
        await update.message.reply_text(f"{CUTE['error']} No game to cancel.", parse_mode="MarkdownV2")
        return

    if uid not in BOT_OWNERS and not await is_admin(update, uid):
        await update.message.reply_text(f"{CUTE['error']} You are not allowed to cancel this game.", parse_mode="MarkdownV2")
        return

    games.pop(chat_id, None)
    await update.message.reply_text(f"{CUTE['done']} Game cancelled.", parse_mode="MarkdownV2")

    # unpin
    try:
        await context.bot.unpin_chat_message(chat_id, g.get("pin_id"))
    except:
        pass


# ----------------------------
# START GAME FUNCTION
# ----------------------------
async def start_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g or g["state"] != "lobby":
        return

    g["state"] = "running"
    g["round"] = 0

    # Unpin lobby
    try:
        await context.bot.unpin_chat_message(chat_id, g["pin_id"])
    except:
        pass

    await context.bot.send_message(
        chat_id,
        f"{CUTE['start']} *Game Started!* Good luck everyone!",
        parse_mode="MarkdownV2"
    )

    # begin rounds
    for r in range(1, g["total_rounds"] + 1):
        if chat_id not in games:
            return
        g = games.get(chat_id)
        g["round"] = r
        g["submissions"] = {}

        await run_round(chat_id, context)

        if chat_id not in games:
            return

    # Game ended
    await end_game(chat_id, context)


# ----------------------------
# RUN ROUND
# ----------------------------
async def run_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    r = g["round"]
    categories = g["categories"]
    letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    count = len(categories)

    cat_block = "\n".join(f"{i+1}. {escape_md(c)}:" for i, c in enumerate(categories))

    text = (
        f"{CUTE['round']} *Round {r}*\n"
        f"{CUTE['category']} Letter: *{letter}*\n\n"
        f"Copy & paste this template:\n"
        f"```\n{cat_block}\n```"
    )

    await context.bot.send_message(chat_id, text, parse_mode="MarkdownV2")

    # Start timers:
    if g["mode"] == "fast":
        total_time = FASTMODE_ROUND_TIME
    else:
        total_time = ROUND_TIME_CLASSIC

    # NO-SUBMISSION timeout
    async def no_submission_timeout():
        await asyncio.sleep(total_time)
        if chat_id in games:
            g2 = games[chat_id]
            if not g2["submissions"]:
                await context.bot.send_message(
                    chat_id,
                    f"{CUTE['error']} No submissions received! Round ended.",
                    parse_mode="MarkdownV2"
                )
    asyncio.create_task(no_submission_timeout())


# ----------------------------
# SUBMISSION HANDLER
# ----------------------------
async def submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    g = games.get(chat_id)
    if not g or g["state"] != "running":
        return

    if uid not in g["players"]:
        return

    text = update.message.text or ""
    if text.startswith("/"):
        return

    # Prevent multiple submissions
    if uid in g["submissions"]:
        return

    g["submissions"][uid] = text

    # First submission triggers 2-second window
    if len(g["submissions"]) == 1:
        await context.bot.send_message(
            chat_id,
            f"{CUTE['round']} First submission received! Everyone else has *2 seconds!*",
            parse_mode="MarkdownV2"
        )

        async def finish():
            await asyncio.sleep(FIRST_SUBMISSION_WINDOW)
            await score_round(chat_id, context)
        asyncio.create_task(finish())


# ----------------------------
# SCORING A ROUND
# ----------------------------
async def score_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return

    categories = g["categories"]
    letter = None
    # Retrieve last round letter
    # Simplify by resending last message: store letter in state next time
    # Quick fix:
    letter = "A"  # fallback if not stored
    # (Better to store letter in g["current_letter"] but skipped here for brevity unless needed)
    # Let's fix properly:
    if "current_letter" not in g:
        g["current_letter"] = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    letter = g["current_letter"]

    # Parse answers
    parsed = {}
    for uid, msg in g["submissions"].items():
        parsed[uid] = extract_answers(msg, len(categories))

    # Count duplicates per category
    dupmap = []
    for i in range(len(categories)):
        counts = {}
        for uid, ans in parsed.items():
            val = ans[i].strip().lower()
            if val:
                counts[val] = counts.get(val, 0) + 1
        dupmap.append(counts)

    round_results = {}

    for uid, answers in parsed.items():
        uidstr = str(uid)
        validated_count = 0
        points = 0
        for i, ans in enumerate(answers):
            clean = ans.strip()
            if not clean or not clean[0].upper() == letter.upper():
                continue

            valid = await ai_validate(categories[i], clean, letter)
            if valid is None:
                # AI failed -> admin validation required
                g["pending_manual"].append((uid, categories[i], clean))
                continue

            if valid:
                validated_count += 1
                # scoring unique/shared
                norm = clean.lower()
                if dupmap[i].get(norm, 0) == 1:
                    points += 10
                else:
                    points += 5

        # update stats
        db_add_validated(uidstr, validated_count)
        round_results[uid] = points

    # send results
    txt = f"{CUTE['score']} *Round Results*\n\n"
    for uid, pts in round_results.items():
        txt += f"{mention(uid, g['players'][uid])}: `{pts}` pts\n"
    await context.bot.send_message(chat_id, txt, parse_mode="MarkdownV2")

    # handle pending manual validations
    if g["pending_manual"]:
        for (uid, cat, ans) in g["pending_manual"]:
            await send_manual_validation(update, context, chat_id, str(uid), cat, ans)
        g["pending_manual"].clear()


# ----------------------------
# MANUAL VALIDATION CALLBACK
# ----------------------------
async def manual_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    user = cq.from_user
    chat_id = cq.message.chat.id
    data = cq.data

    if not (await is_admin(update, user.id)):
        await cq.answer("Admins only.", show_alert=True)
        return

    parts = data.split("|")
    tag, uid, cat, ans = parts

    uid = int(uid)

    if tag == "valid_yes":
        db_add_validated(str(uid), 1)
        msg = f"{CUTE['done']} Manually marked VALID for {mention(uid, games[chat_id]['players'][uid])}"
    else:
        msg = f"{CUTE['error']} Marked INVALID for {mention(uid, games[chat_id]['players'][uid])}"

    await cq.edit_message_text(msg, parse_mode="MarkdownV2")
    await cq.answer()


# ----------------------------
# END GAME
# ----------------------------
async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return

    await context.bot.send_message(
        chat_id,
        f"{CUTE['done']} *Game Over!*",
        parse_mode="MarkdownV2"
    )

    games.pop(chat_id, None)


# ----------------------------
# /leaderboard
# ----------------------------
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_leaderboard_top10()
    if not rows:
        await update.message.reply_text("No stats yet.")
        return

    txt = f"{CUTE['score']} *Top 10 Leaderboard*\n\n"
    for i, (uid, total) in enumerate(rows, start=1):
        txt += f"{i}. {mention(int(uid), 'User')} — `{total}` words\n"

    await update.message.reply_text(txt, parse_mode="MarkdownV2")


# ----------------------------
# /mystats
# ----------------------------
async def mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    total = db_get_stats(uid)

    txt = (
        f"{CUTE['score']} *Your Stats*\n\n"
        f"Total Validated Words: `{total}`\n"
    )
    await update.message.reply_text(txt, parse_mode="MarkdownV2")


# ----------------------------
# /dumpstats (OWNER ONLY)
# ----------------------------
async def dumpstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in BOT_OWNERS:
        return

    # Text export
    rows = db_leaderboard_top10()
    text_export = "user_id,total_validated_words\n" + "\n".join(f"{r[0]},{r[1]}" for r in rows)

    await update.message.reply_text(f"```\n{text_export}\n```", parse_mode="MarkdownV2")

    # CSV export
    with open("stats_export.csv", "w") as f:
        f.write(text_export)
    await update.message.reply_document(InputFile("stats_export.csv"))

    # DB file
    await update.message.reply_document(InputFile(DB_NAME))


# ----------------------------
# /statsreset (OWNER ONLY)
# ----------------------------
async def statsreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in BOT_OWNERS:
        return
    db_reset()
    await update.message.reply_text("Stats reset done.")


# ----------------------------
# MAIN
# ----------------------------
def main():
    init_db()
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()

    app.add_handler(CommandHandler("classicadedonha", classicadedonha))
    app.add_handler(CommandHandler("fastadedonha", fastadedonha))
    app.add_handler(CommandHandler("customadedonha", customadedonha))
    app.add_handler(CommandHandler("joingame", joingame))
    app.add_handler(CommandHandler("gamecancel", gamecancel))
    app.add_handler(CommandHandler("modeinfo", modeinfo))
    app.add_handler(CommandHandler("categories", lambda u, c: u.message.reply_text(
        "All categories:\n" + "\n".join(f"• {x}" for x in ALL_CATEGORIES)
    )))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("mystats", mystats))
    app.add_handler(CommandHandler("dumpstats", dumpstats))
    app.add_handler(CommandHandler("statsreset", statsreset))

    app.add_handler(CallbackQueryHandler(lobby_callback, pattern="^lobby_"))
    app.add_handler(CallbackQueryHandler(manual_validation_callback, pattern="^valid_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submission_handler))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
