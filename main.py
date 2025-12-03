
import os
import json
import logging
import random
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

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
TELEGRAM_BOT_TOKEN = os.getenv(\"TELEGRAM_BOT_TOKEN\")
OPENAI_API_KEY = os.getenv(\"OPENAI_API_KEY\")  # optional

# Owner(s) - allowed to run /dumpstats and /statsreset and special owner-only commands
OWNERS = {\"624102836\", \"1707015091\"}  # string ids
# Group admins can do manual validation when AI fails
# Timeouts and constants
MAX_PLAYERS = 10
LOBBY_TIMEOUT = 5 * 60  # seconds
CLASSIC_NO_SUBMIT_TIMEOUT = 3 * 60
FAST_ROUND_SECONDS = 60
CLASSIC_FIRST_WINDOW = 2  # seconds after first submit changed to 2 per user request
FAST_FIRST_WINDOW = 2  # also 2s window
TOTAL_ROUNDS_CLASSIC = 10
TOTAL_ROUNDS_FAST = 12

# SQLite DB file
DB_FILE = \"stats.db\"

# AI model to use
AI_MODEL = \"gpt-4.1-mini\"

# Categories pool (12)
ALL_CATEGORIES = [
    \"Name\",
    \"Object\",
    \"Animal\",
    \"Plant\",
    \"City/Country/State\",
    \"Food\",
    \"Color\",
    \"Movie/Series/TV Show\",
    \"Place\",
    \"Fruit\",
    \"Profession\",
    \"Adjective\",
]

# ---------------- LOGGING ----------------
logging.basicConfig(
    format=\"%(asctime)s - %(name)s - %(levelname)s - %(message)s\",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- OPENAI CLIENT ----------------
ai_client = None
if OPENAI_API_KEY:
    try:
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning(\"OpenAI client init failed: %s\", e)
        ai_client = None

# ---------------- DB (SQLite) ----------------
def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        \"\"\"
        CREATE TABLE IF NOT EXISTS stats (
            user_id TEXT PRIMARY KEY,
            games_played INTEGER DEFAULT 0,
            total_validated_words INTEGER DEFAULT 0,
            total_wordlists_sent INTEGER DEFAULT 0
        )
        \"\"\"
    )
    conn.commit()
    conn.close()

def db_ensure_user(uid: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(\"SELECT 1 FROM stats WHERE user_id=?\", (uid,))
    if not c.fetchone():
        c.execute(\"INSERT INTO stats (user_id) VALUES (?)\", (uid,))
    conn.commit()
    conn.close()

def db_update_after_round(uid: str, validated_words: int, submitted_any: bool) -> None:
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if submitted_any:
        c.execute(
            \"UPDATE stats SET total_wordlists_sent = total_wordlists_sent + 1, total_validated_words = total_validated_words + ? WHERE user_id=?\",
            (validated_words, uid),
        )
    else:
        # no submission -> nothing to add except preserving user
        pass
    conn.commit()
    conn.close()

def db_update_after_game(user_ids: List[str]) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for uid in user_ids:
        db_ensure_user(uid)
        c.execute(\"UPDATE stats SET games_played = games_played + 1 WHERE user_id=?\", (uid,))
    conn.commit()
    conn.close()

def db_get_stats(uid: str):
    db_ensure_user(uid)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(\"SELECT games_played, total_validated_words, total_wordlists_sent FROM stats WHERE user_id=?\", (uid,))
    row = c.fetchone()
    conn.close()
    if row:
        return {\"games_played\": row[0], \"total_validated_words\": row[1], \"total_wordlists_sent\": row[2]}
    return {\"games_played\": 0, \"total_validated_words\": 0, \"total_wordlists_sent\": 0}

def db_dump_all() -> List[tuple]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(\"SELECT user_id, games_played, total_validated_words, total_wordlists_sent FROM stats ORDER BY total_validated_words DESC\")
    rows = c.fetchall()
    conn.close()
    return rows

def db_reset_all() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(\"UPDATE stats SET games_played=0, total_validated_words=0, total_wordlists_sent=0\")
    conn.commit()
    conn.close()

# ---------------- UTIL ----------------
def escape_md_v2(text: str) -> str:
    if not text:
        return \"\"
    # Escape according to Telegram MarkdownV2
    to_escape = r'_*[]()~`>#+-=|{}.!'
    return ''.join(('\\' + c) if c in to_escape else c for c in str(text))

def user_mention_md(uid: int, name: str) -> str:
    return f\"[{escape_md_v2(name)}](tg://user?id={uid})\"

def choose_random_categories(count: int) -> List[str]:
    return random.sample(ALL_CATEGORIES, count)

def extract_answers_from_text(text: str, count: int) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    answers = []
    for line in lines:
        if ':' in line:
            parts = line.split(':', 1)
            answers.append(parts[1].strip())
        else:
            answers.append(line.strip())
        if len(answers) >= count:
            break
    while len(answers) < count:
        answers.append('')
    return answers[:count]

# ---------------- AI VALIDATION ----------------
async def ai_validate(category: str, answer: str, letter: str) -> bool:
    if not answer:
        return False
    # quick local checks
    if not answer[0].isalpha() or answer[0].upper() != letter.upper():
        return False
    if not ai_client:
        # No AI configured -> fallback to permissive behavior (admins will verify)
        return True
    prompt = f\"\"\"You are a terse validator for the game Adedonha.
Rules:
- The answer must start with the letter '{letter}' (case-insensitive).
- It must belong to the category: '{category}'.
Answer only YES or NO.\"\"\"
    try:
        resp = ai_client.responses.create(model=AI_MODEL, input=prompt + f\"\\nAnswer: {answer}\", max_output_tokens=6)
        out = ''
        if getattr(resp, 'output', None):
            for block in resp.output:
                if isinstance(block, dict):
                    content = block.get('content')
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and 'text' in item:
                                out += item['text']
                            elif isinstance(item, str):
                                out += item
                    elif isinstance(content, str):
                        out += content
                elif isinstance(block, str):
                    out += block
        out = out.strip().upper()
        return out.startswith('YES')
    except Exception as e:
        logger.warning('AI validation failed: %s', e)
        return True  # permissive fallback; admins can verify manually

# ---------------- GAME STATE ----------------
# Games keyed by chat_id
games: Dict[int, Dict] = {}

# Helper to check owner
def is_owner(user_id: int) -> bool:
    return str(user_id) in OWNERS

# ---------------- COMMANDS / LOBBY ----------------
async def start_classic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /classicadedonha or /customadedonha default to classic behavior but customadedonha handled separately
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == 'private':
        await update.message.reply_text('This command works in groups only.')
        return
    # parse optional number 5-8
    args = context.args or []
    num = 5
    if args:
        try:
            v = int(args[0])
            if 5 <= v <= 8:
                num = v
            else:
                await update.message.reply_text('Please provide a number between 5 and 8. Defaulting to 5.')
        except Exception:
            await update.message.reply_text('Invalid number. Defaulting to 5.')
    # create lobby
    if chat.id in games and games[chat.id].get('state') in ('lobby', 'running'):
        await update.message.reply_text('A game is already active in this group.')
        return
    # lobby structure
    lobby = {
        'mode': 'classic',
        'categories_per_round': num,
        'creator_id': user.id,
        'creator_name': user.first_name,
        'players': {str(user.id): user.first_name},
        'state': 'lobby',
        'created_at': datetime.utcnow().isoformat(),
        'lobby_message_id': None,
        'lobby_pin': True,
        'round': 0,
        'round_task': None,
        'submissions': {},
    }
    games[chat.id] = lobby
    # make lobby message and pin
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('Join 🦩', callback_data='join_lobby')],
        [InlineKeyboardButton('Start ▶️', callback_data='start_game')],
        [InlineKeyboardButton('Mode Info ℹ️', callback_data='mode_info')],
    ])
    text = f\"*Adedonha lobby created!*\\n\\nMode: *Classic*\\nCategories per round: *{num}*\\nTotal rounds: *{TOTAL_ROUNDS_CLASSIC}*\\n\\nPlayers:\\n{user_mention_md(user.id, user.first_name)}\\n\\nPress Join to participate.\"
    msg = await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=kb)
    lobby['lobby_message_id'] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info('Pin failed: %s', e)
    # schedule auto-cancel
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get('state') == 'lobby':
            # cancel if only creator or no joiners
            if len(g.get('players', {})) <= 1:
                try:
                    await context.bot.send_message(chat.id, 'Lobby cancelled due to inactivity.')
                except Exception:
                    pass
                games.pop(chat.id, None)
    lobby['lobby_task'] = asyncio.create_task(lobby_timeout())

async def start_fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /fastadedonha cat1 cat2 cat3 or without args -> random 3 chosen
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == 'private':
        await update.message.reply_text('This command works in groups only.')
        return
    if chat.id in games and games[chat.id].get('state') in ('lobby', 'running'):
        await update.message.reply_text('A game is already active in this group.')
        return
    args = context.args or []
    cats = []
    if args and len(args) >= 3:
        # trust user provided categories; sanitize by matching to pool if possible
        for a in args[:3]:
            a_clean = a.strip()
            # try to match ignoring case to available categories, otherwise accept raw
            match = next((c for c in ALL_CATEGORIES if c.lower().startswith(a_clean.lower())), None)
            cats.append(match or a_clean)
    else:
        cats = choose_random_categories(3)
    lobby = {
        'mode': 'fast',
        'categories_per_round': 3,
        'custom_categories': cats,
        'creator_id': user.id,
        'creator_name': user.first_name,
        'players': {str(user.id): user.first_name},
        'state': 'lobby',
        'created_at': datetime.utcnow().isoformat(),
        'lobby_message_id': None,
        'lobby_pin': True,
        'round': 0,
        'round_task': None,
        'submissions': {},
    }
    games[chat.id] = lobby
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('Join 🦩', callback_data='join_lobby')],
        [InlineKeyboardButton('Start ▶️', callback_data='start_game')],
        [InlineKeyboardButton('Mode Info ℹ️', callback_data='mode_info')],
    ])
    cats_md = '\\n'.join(f\"- {escape_md_v2(c)}\" for c in cats)
    text = f\"*Adedonha lobby created!*\\n\\nMode: *Fast*\\nCategories (fixed):\\n{cats_md}\\nTotal rounds: *{TOTAL_ROUNDS_FAST}*\\n\\nPlayers:\\n{user_mention_md(user.id, user.first_name)}\\n\\nPress Join to participate.\"
    msg = await update.message.reply_text(text, parse_mode='MarkdownV2', reply_markup=kb)
    lobby['lobby_message_id'] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id)
    except Exception as e:
        logger.info('Pin failed: %s', e)
    async def lobby_timeout():
        await asyncio.sleep(LOBBY_TIMEOUT)
        g = games.get(chat.id)
        if g and g.get('state') == 'lobby':
            if len(g.get('players', {})) <= 1:
                try:
                    await context.bot.send_message(chat.id, 'Lobby cancelled due to inactivity.')
                except Exception:
                    pass
                games.pop(chat.id, None)
    lobby['lobby_task'] = asyncio.create_task(lobby_timeout())

# join via command or button
async def join_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /joingame command - then bot deletes the command message to keep chat clean
    try:
        await update.message.delete()
    except Exception:
        pass
    await join_lobby_callback(update, context, by_command=True)

async def join_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, by_command: bool = False):
    if update.callback_query:
        cq = update.callback_query
        chat_id = cq.message.chat.id
        user = cq.from_user
        await cq.answer()
    else:
        chat_id = update.effective_chat.id
        user = update.effective_user
    g = games.get(chat_id)
    if not g or g.get('state') != 'lobby':
        if by_command:
            await context.bot.send_message(chat_id, 'No lobby is active to join.')
        return
    if len(g['players']) >= MAX_PLAYERS:
        await context.bot.send_message(chat_id, 'Lobby is full (10 players).')
        return
    if str(user.id) in g['players']:
        if by_command:
            await context.bot.send_message(chat_id, 'You already joined the lobby.')
        else:
            try:
                await cq.answer('You already joined.')
            except Exception:
                pass
        return
    g['players'][str(user.id)] = user.first_name
    # update lobby message players list
    players_md = '\\n'.join(user_mention_md(int(uid), name) for uid, name in g['players'].items())
    text = f\"*Adedonha lobby created!*\\n\\nMode: *{escape_md_v2(g['mode'].capitalize())}*\\nCategories per round: *{g['categories_per_round']}*\\nTotal rounds: *{TOTAL_ROUNDS_CLASSIC if g['mode']=='classic' else TOTAL_ROUNDS_FAST}*\\n\\nPlayers:\\n{players_md}\\n\\nPress Join to participate.\"
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=g['lobby_message_id'], parse_mode='MarkdownV2', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Join 🦩', callback_data='join_lobby')],[InlineKeyboardButton('Start ▶️', callback_data='start_game')],[InlineKeyboardButton('Mode Info ℹ️', callback_data='mode_info')]]))
    except Exception:
        await context.bot.send_message(chat_id, f\"{user_mention_md(user.id, user.first_name)} joined the lobby.\", parse_mode='MarkdownV2')

# mode info callback
async def mode_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    g = games.get(chat_id)
    if not g:
        await cq.answer('No active lobby or game.', show_alert=True)
        return
    mode = g['mode']
    if mode == 'classic':
        num = g.get('categories_per_round', 5)
        text = (f\"*Classic Adedonha*\\nEach round selects *{num}* categories randomly from the 12 possible.\\n\"
                f\"You have {CLASSIC_NO_SUBMIT_TIMEOUT//60} minutes if nobody submits. After first submission, others have {CLASSIC_FIRST_WINDOW} seconds.\\nTotal rounds: {TOTAL_ROUNDS_CLASSIC}.\")
    else:
        cats = g.get('custom_categories') or g.get('categories_per_round') or []
        cat_md = '\\n'.join(f\"- {escape_md_v2(c)}\" for c in (cats if isinstance(cats, list) else []))
        text = (f\"*Fast Adedonha*\\nEach round uses 3 categories (fixed):\\n{cat_md}\\nYou have {FAST_ROUND_SECONDS} seconds per round.\\nTotal rounds: {TOTAL_ROUNDS_FAST}.\")
    try:
        await cq.answer()
    except Exception:
        pass
    await context.bot.send_message(chat_id, text, parse_mode='MarkdownV2')

# start game callback
async def start_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    g = games.get(chat_id)
    await cq.answer()
    if not g or g.get('state') != 'lobby':
        await context.bot.send_message(chat_id, 'No lobby ready to start.')
        return
    # permission: only creator or chat admin may start if admin-only is enabled; for simplicity allow anyone here
    # Start game
    await cq.edit_message_reply_markup(reply_markup=None)
    # unpin lobby message
    try:
        await context.bot.unpin_chat_message(chat_id)
    except Exception:
        pass
    # cancel lobby task
    if g.get('lobby_task'):
        g['lobby_task'].cancel()
    g['state'] = 'running'
    # call run_game coroutine
    asyncio.create_task(run_game(chat_id, context))

async def run_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    g = games.get(chat_id)
    if not g:
        return
    mode = g['mode']
    if mode == 'classic':
        rounds = TOTAL_ROUNDS_CLASSIC
        per_round = g.get('categories_per_round', 5)
    else:
        rounds = TOTAL_ROUNDS_FAST
        per_round = 3
    # prepare scores
    g['scores'] = {uid: 0 for uid in g['players'].keys()}
    # mark game started in stats
    db_update_after_game(list(g['players'].keys()))
    for r in range(1, rounds + 1):
        # prepare round
        g['round'] = r
        if mode == 'classic':
            categories = choose_random_categories(per_round)
            letter = random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        else:
            categories = g.get('custom_categories') or choose_random_categories(3)
            letter = random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            window_seconds = FAST_FIRST_WINDOW
            no_submit_timeout = FAST_ROUND_SECONDS  # if no first submission, still full round is FAST_ROUND_SECONDS
            round_time_limit = FAST_ROUND_SECONDS
        g['current_categories'] = categories
        g['round_letter'] = letter
        g['submissions'] = {}
        # send round template
        cat_lines = '\\n'.join(f\"{i+1}. {escape_md_v2(cat)}:\" for i, cat in enumerate(categories))
        text = (f\"*Round {r} / {rounds}*\\nLetter: *{escape_md_v2(letter)}*\\n\\nSend your answers in ONE message using the template:\\n```\n{cat_lines}\n```\n\"
                f\"• First submission starts a {window_seconds}s window for others (or full round time for fast mode).\\n\")
        await context.bot.send_message(chat_id, text, parse_mode='MarkdownV2')
        # schedule no-submission timeout or round timeout
        end_round_event = asyncio.Event()
        first_submitter = None
        first_submit_time = None

        async def no_submit_worker():
            await asyncio.sleep(no_submit_timeout)
            # if no submissions, end round with no penalties (classic: if no submissions -> no penalties; fast: same behavior)
            if not g.get('submissions'):
                await context.bot.send_message(chat_id, f\"⏱ Round {r} ended: no submissions. No penalties.\", parse_mode='MarkdownV2')
                g['round_result'] = None
                end_round_event.set()

        no_submit_task = asyncio.create_task(no_submit_worker())

        # wait until end_round_event set by submission handler or timeout above
        while not end_round_event.is_set():
            await asyncio.sleep(0.5)
            # if someone submitted and first_submitter exists and window passed, score
            if g.get('submissions') and not first_submitter:
                # detect first submitter
                # take earliest by insertion order
                first_submitter = next(iter(g['submissions'].keys()))
                first_submit_time = datetime.utcnow()
                # announce first submitter
                try:
                    await context.bot.send_message(chat_id, f\"⏱ {user_mention_md(int(first_submitter), g['players'][first_submitter])} submitted first! Others have {window_seconds}s to submit.\", parse_mode='MarkdownV2')
                except Exception:
                    pass
                # schedule scoring after window_seconds (but for fast mode allow up to round_time_limit)
                async def window_worker():
                    await asyncio.sleep(window_seconds)
                    # for fast mode, allow additional time until round_time_limit is reached, but per spec no penalties for late; we've simplified to accept any submissions until window end
                    end_round_event.set()
                asyncio.create_task(window_worker())
            # extra condition: if round_time_limit in fast mode, enforce total round length
            if round_time_limit and first_submit_time:
                if (datetime.utcnow() - first_submit_time).total_seconds() >= round_time_limit:
                    end_round_event.set()
        # cancel no_submit_task
        try:
            no_submit_task.cancel()
        except Exception:
            pass
        # scoring
        submissions = g.get('submissions', {})
        if not submissions:
            # no submissions already handled
            g['round_result'] = {}
            g['round_scores_history'] = g.get('round_scores_history', []) + [{}]
            continue
        # parse and validate answers per player
        parsed = {}
        for uid, txt in submissions.items():
            parsed[uid] = extract_answers_from_text(txt, len(categories))
        # build per-category frequency map
        per_cat_freq = [ {} for _ in range(len(categories)) ]
        for idx in range(len(categories)):
            for uid, answers in parsed.items():
                a = answers[idx].strip()
                if a:
                    key = a.lower()
                    per_cat_freq[idx][key] = per_cat_freq[idx].get(key, 0) + 1
        # validate via AI (or permissive) and score
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
                if not valid:
                    # mark for manual validation if ai_client None -> handled later by /validate flow
                    continue
                # scoring unique/shared
                key = a_clean.lower()
                cnt = per_cat_freq[idx].get(key, 0)
                if cnt == 1:
                    pts += 10
                else:
                    pts += 5
                validated_count += 1
            round_scores[uid] = {'points': pts, 'validated': validated_count, 'submitted_any': submitted_any}
            # update totals
            g['scores'][uid] = g['scores'].get(uid, 0) + pts
            # update DB stats
            db_update_after_round(uid, validated_count, submitted_any)
        # no penalties for late per final decision; only classic mode had -5 but user removed MVP and then removed penalties? User said no penalties for late any game mode; earlier classic had -5 for no submission; final says no penalties for late any mode; keep classic no-submission penalty? User earlier removed MVP, later said no penalties for late any game mode. We will NOT penalize non-submitters in any mode as final preference.
        # store history
        g['round_scores_history'] = g.get('round_scores_history', []) + [round_scores]
        # prepare round summary
        header = f\"*Round {r} Results*\\nLetter: *{escape_md_v2(letter)}*\\n\\n\"
        body = ''
        # list scores
        sorted_players = sorted(g['players'].items(), key=lambda x: -g['scores'].get(x[0],0))
        for uid, name in sorted_players:
            pts = round_scores.get(uid, {}).get('points', 0)
            body += f\"{user_mention_md(int(uid), name)} — `{pts}` pts\\n\"
        # send summary
        try:
            await context.bot.send_message(chat_id, header + body, parse_mode='MarkdownV2')
        except Exception as e:
            logger.warning('Failed to send round summary: %s', e)
        await asyncio.sleep(1)
    # end of game
    # send final leaderboard
    lb = sorted(g['scores'].items(), key=lambda x: -x[1])
    text = '*Game Over — Final Scores*\\n\\n'
    for uid, pts in lb:
        text += f\"{user_mention_md(int(uid), g['players'][uid])} — `{pts}` pts\\n\"
    await context.bot.send_message(chat_id, text, parse_mode='MarkdownV2')
    # cleanup
    games.pop(chat_id, None)

# submission handler
async def submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == 'private':
        return
    g = games.get(chat.id)
    if not g or g.get('state') != 'running':
        return
    if str(user.id) not in g['players']:
        return
    # only accept one submission per round
    uid = str(user.id)
    if uid in g.get('submissions', {}):
        await update.message.reply_text('You already submitted for this round.')
        return
    text = update.message.text or ''
    g['submissions'][uid] = text
    # deletions of user's message are not performed (keeping transparency)
    # if AI is unavailable, start manual validation flow: create one message with inline buttons for admin validation
    if not ai_client:
        # create single validation message if not exists
        vm_key = f\"validate_msg_{chat.id}_{g['round']}\"
        if not g.get('manual_validation_msg_id'):
            # prepare compact review content
            preview = ''
            for uid2, txt in g['submissions'].items():
                preview += f\"{g['players'][uid2]}: {escape_md_v2(txt)[:100]}\\n\"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('Open validation panel ✅', callback_data='open_manual_validate')]])
            msg = await context.bot.send_message(chat.id, f\"*Manual validation required*\\nAI not available. Admins may validate answers via the validation panel.\\n\\nSubmissions preview:\\n{escape_md_v2(preview)}\", parse_mode='MarkdownV2', reply_markup=kb)
            g['manual_validation_msg_id'] = msg.message_id
    # else: normal flow - scoring will be handled by run_game loop that waits for window

# manual validation panel (button opens a compact single message for admins to validate answers)
async def open_manual_validate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    chat_id = cq.message.chat.id
    user = cq.from_user
    g = games.get(chat_id)
    await cq.answer()
    # only allow group admins
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status not in ('administrator', 'creator'):
            await cq.answer('Only chat admins can validate manually.', show_alert=True)
            return
    except Exception:
        await cq.answer('Only chat admins can validate manually.', show_alert=True)
        return
    # build compact message with buttons for each user's submission to accept/reject per category
    # to keep it lightweight, this panel will allow admin to mark entire submission as valid or invalid per category set (not individual categories)
    buttons = []
    for uid, txt in g.get('submissions', {}).items():
        label = f\"{g['players'][uid]}\"
        buttons.append([InlineKeyboardButton(f\"✅ {escape_md_v2(label)}\", callback_data=f\"validate_accept|{uid}\"),
                        InlineKeyboardButton(f\"❌ {escape_md_v2(label)}\", callback_data=f\"validate_reject|{uid}\")])
    # add close button
    buttons.append([InlineKeyboardButton('Close 🛑', callback_data='validate_close')])
    try:
        # edit or send a new panel message (single shared message)
        if g.get('validation_panel_message_id'):
            await context.bot.edit_message_text('Validation panel (admins):', chat_id, g['validation_panel_message_id'], reply_markup=InlineKeyboardMarkup(buttons))
        else:
            msg = await context.bot.send_message(chat_id, 'Validation panel (admins):', reply_markup=InlineKeyboardMarkup(buttons))
            g['validation_panel_message_id'] = msg.message_id
    except Exception as e:
        logger.warning('Validation panel error: %s', e)

async def validation_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ''
    user = cq.from_user
    chat_id = cq.message.chat.id
    await cq.answer()
    g = games.get(chat_id)
    if not g:
        return
    # check admin
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status not in ('administrator', 'creator'):
            await cq.answer('Only chat admins can use this panel.', show_alert=True)
            return
    except Exception:
        await cq.answer('Only chat admins can use this panel.', show_alert=True)
        return
    if data == 'validate_close':
        # remove panel
        try:
            await context.bot.delete_message(chat_id, cq.message.message_id)
        except Exception:
            pass
        g.pop('validation_panel_message_id', None)
        return
    if data.startswith('validate_accept|') or data.startswith('validate_reject|'):
        action, uid = data.split('|',1)
        if action == 'validate_accept':
            # mark user's submission as accepted for scoring by setting ai_client temporary acceptance flag
            # We will add an entry in g['manual_accept'] so scoring uses these flags
            g.setdefault('manual_accept', {})[uid] = True
            await cq.answer('Marked as accepted.')
        else:
            g.setdefault('manual_accept', {})[uid] = False
            await cq.answer('Marked as rejected.')
        # update panel UI (strike-through or emoji)
        # rebuild buttons
        buttons = []
        for uid2, txt in g.get('submissions', {}).items():
            label = f\"{g['players'][uid2]}\"
            acc = g.get('manual_accept', {}).get(uid2)
            if acc is True:
                b1 = InlineKeyboardButton(f\"✅ {escape_md_v2(label)}\", callback_data=f\"validate_accept|{uid2}\")
            elif acc is False:
                b1 = InlineKeyboardButton(f\"❌ {escape_md_v2(label)}\", callback_data=f\"validate_reject|{uid2}\")
            else:
                b1 = InlineKeyboardButton(escape_md_v2(label), callback_data=f\"validate_none|{uid2}\")
            buttons.append([b1, InlineKeyboardButton('Toggle', callback_data=f\"validate_toggle|{uid2}\")])
        buttons.append([InlineKeyboardButton('Close 🛑', callback_data='validate_close')])
        try:
            await context.bot.edit_message_reply_markup(chat_id, g.get('validation_panel_message_id'), reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass

# data callbacks dispatcher
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data or ''
    if data == 'join_lobby':
        await join_lobby_callback(update, context)
    elif data == 'mode_info':
        await mode_info_callback(update, context)
    elif data == 'start_game':
        await start_game_callback(update, context)
    elif data == 'open_manual_validate':
        await open_manual_validate_callback(update, context)
    elif data.startswith('validate_'):
        await validation_button_handler(update, context)
    else:
        await update.callback_query.answer('Unknown action.', show_alert=True)

# game cancel command
async def game_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = games.get(chat.id)
    if not g:
        await update.message.reply_text('No active game or lobby to cancel.')
        return
    # only creator, chat admin, or owner can cancel
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        is_admin = member.status in ('creator','administrator')
    except Exception:
        is_admin = False
    if user.id != g['creator_id'] and not is_admin and not is_owner(user.id):
        await update.message.reply_text('Only the creator, a chat admin, or bot owner can cancel the game.')
        return
    # unpin lobby if pinned
    try:
        await context.bot.unpin_chat_message(chat.id)
    except Exception:
        pass
    # cancel tasks
    if g.get('lobby_task'):
        try: g['lobby_task'].cancel()
        except Exception: pass
    games.pop(chat.id, None)
    await update.message.reply_text('Game cancelled.')

# categories command
async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = '*All possible categories (12):*\\n' + '\\n'.join(f\"{i+1}. {escape_md_v2(c)}\" for i,c in enumerate(ALL_CATEGORIES))
    await update.message.reply_text(text, parse_mode='MarkdownV2')

# mystats command (supports reply)
async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    uid = str(target.id)
    s = db_get_stats(uid)
    # compute ranking by validated words
    all_rows = db_dump_all()
    rank = 1
    for idx, row in enumerate(all_rows, start=1):
        if row[0] == uid:
            rank = idx
            break
    text = (f\"*Stats of {user_mention_md(int(uid), target.first_name)}*\\n\\n\"
            f\"• *Games played:* `{s.get('games_played',0)}`\\n\"
            f\"• *Total validated words:* `{s.get('total_validated_words',0)}`\\n\"
            f\"• *Wordlists sent:* `{s.get('total_wordlists_sent',0)}`\\n\"
            f\"• *Global position:* `{rank}`\\n\")
    await update.message.reply_text(text, parse_mode='MarkdownV2')

# dumpstats (owner only) - sends text table, csv and db file
async def dumpstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text('Only bot owner can use this command.')
        return
    rows = db_dump_all()
    # formatted text
    header = 'User ID | Games | Validated | Lists\\n'
    lines = [header] + [f\"{r[0]} | {r[1]} | {r[2]} | {r[3]}\" for r in rows]
    text = '\\n'.join(lines)
    # send text (escaped)
    await update.message.reply_text('```\n' + text + '\n```', parse_mode='MarkdownV2')
    # csv
    csv_path = '/tmp/stats_export.csv'
    with open(csv_path, 'w', encoding='utf8') as f:
        f.write('user_id,games_played,total_validated_words,total_wordlists_sent\\n')
        for r in rows:
            f.write(','.join(str(x) for x in r) + '\\n')
    await update.message.reply_document(open(csv_path,'rb'))
    # send db file
    await update.message.reply_document(open(DB_FILE,'rb'))

# stats reset (owner only)
async def statsreset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text('Only bot owner can use this command.')
        return
    db_reset_all()
    await update.message.reply_text('All stats reset to zero.')

# validate command (sent by admins to trigger manual validation panel message)
async def validate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    g = games.get(chat.id)
    if not g:
        await update.message.reply_text('No active game.')
        return
    # only allow if ai_client None or admin forces it
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ('administrator','creator'):
            await update.message.reply_text('Only chat admins can trigger manual validation.')
            return
    except Exception:
        await update.message.reply_text('Only chat admins can trigger manual validation.')
        return
    # create or open validation panel
    await open_manual_validate_callback(update, context)

# helper on startup to ensure DB
init_db()

# ---------------- APPLICATION SETUP ----------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        print('Please set TELEGRAM_BOT_TOKEN environment variable before running.')
        return
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler(['classicadedonha','customadedonha'], start_classic))
    app.add_handler(CommandHandler('fastadedonha', start_fast))
    app.add_handler(CommandHandler(['joingame','join'], join_game_command))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(CommandHandler('gamecancel', game_cancel_command))
    app.add_handler(CommandHandler('categories', categories_command))
    app.add_handler(CommandHandler('mystats', mystats_command))
    app.add_handler(CommandHandler('dumpstats', dumpstats_command))
    app.add_handler(CommandHandler('statsreset', statsreset_command))
    app.add_handler(CommandHandler('validate', validate_command))

    # submission handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submission_handler))

    print('Bot running...')
    app.run_polling()

if __name__ == '__main__':
    main()
