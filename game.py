# game.py — game engine and state
import random
import asyncio
from typing import Dict, List
from datetime import datetime
import html as _html

from config import ALL_CATEGORIES, TOTAL_ROUNDS_CLASSIC, TOTAL_ROUNDS_FAST, CLASSIC_FIRST_WINDOW, CLASSIC_NO_SUBMIT_TIMEOUT, FAST_FIRST_WINDOW, FAST_ROUND_SECONDS, EMOJI_WINNER, EMOJI_SECOND, EMOJI_THIRD
from ai_validation import batch_validate
from db import update_after_round, update_after_game, save_word

# games storage: chat_id -> game dict
games: Dict[int, Dict] = {}

def escape_html(text: str) -> str:
    if text is None:
        return ""
    return _html.escape(str(text), quote=False)

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
        answers.append("")
    return answers[:count]

async def start_game(chat_id: int, context):
    g = games.get(chat_id)
    if not g:
        return
    mode = g.get("mode", "classic")
    rounds = TOTAL_ROUNDS_CLASSIC if mode in ("classic", "custom") else TOTAL_ROUNDS_FAST
    g["scores"] = {uid: 0 for uid in g["players"].keys()}
    await update_after_game(list(g["players"].keys()))

    for r in range(1, rounds + 1):
        g["round"] = r
        if mode == "classic":
            categories = ["Name", "Object", "Animal", "Plant", "Country"]
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        elif mode == "custom":
            categories = g.get("categories_pool", ALL_CATEGORIES)
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = CLASSIC_FIRST_WINDOW
            no_submit_timeout = CLASSIC_NO_SUBMIT_TIMEOUT
            round_time_limit = None
        else:
            categories = g.get("fixed_categories", [])
            letter = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            window_seconds = FAST_FIRST_WINDOW
            no_submit_timeout = FAST_ROUND_SECONDS
            round_time_limit = FAST_ROUND_SECONDS

        g["current_categories"] = categories
        g["round_letter"] = letter
        g["submissions"] = {}
        g["manual_accept"] = {}

        pre_block = "\n".join(f"{i+1}. {c}:" for i, c in enumerate(categories))
        intro = (f"Round {r} / {rounds}\nLetter: {letter}\n\n" +
                 f"<pre>{pre_block}</pre>\n\n" +
                 f"Send your answers in ONE MESSAGE using the template above (first {len(categories)} answers will be used).\n")
        intro += f"First submission starts a {window_seconds}s window for others."
        await context.bot.send_message(chat_id, intro, parse_mode="HTML")

        # schedule no submit timeout
        end_event = asyncio.Event()
        first_submitter = None
        first_submit_time = None

        async def no_submit_worker():
            await asyncio.sleep(no_submit_timeout)
            if not g.get("submissions"):
                try:
                    await context.bot.send_message(chat_id, f"⏱ Round {r} ended: no submissions. No penalties.")
                except Exception:
                    pass
                end_event.set()
        no_submit_task = asyncio.create_task(no_submit_worker())

        # wait loop - submissions are collected via submission_handler
        while not end_event.is_set():
            await asyncio.sleep(0.5)
            if g.get("submissions") and not first_submitter:
                first_submitter = next(iter(g["submissions"].keys()))
                first_submit_time = datetime.utcnow()
                try:
                    await context.bot.send_message(chat_id, f"⏱ {g['players'][first_submitter]} submitted first! Others have {window_seconds}s to submit.")
                except Exception:
                    pass
                async def window_worker():
                    await asyncio.sleep(window_seconds)
                    end_event.set()
                asyncio.create_task(window_worker())
            if round_time_limit and first_submit_time:
                if (datetime.utcnow() - first_submit_time).total_seconds() >= round_time_limit:
                    end_event.set()

        try:
            no_submit_task.cancel()
        except Exception:
            pass

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
            # batch validate per player
            valid_map = await batch_validate(g["round_letter"], categories, answers)
            pts = 0
            validated_count = 0
            submitted_any = any(a.strip() for a in answers)
            for idx, a in enumerate(answers):
                a_clean = a.strip()
                if not a_clean:
                    continue
                if a_clean[0].upper() != g["round_letter"].upper():
                    continue
                valid = valid_map.get(idx, False)
                # manual accept override
                man = g.get("manual_accept", {}).get(uid)
                if man is True:
                    valid = True
                if not valid:
                    continue
                key = a_clean.lower()
                cnt = per_cat_freq[idx].get(key, 0)
                if cnt == 1:
                    pts += 10
                else:
                    pts += 5
                # save to local DB for future offline validation
                try:
                    await save_word(a_clean, categories[idx], g["round_letter"])
                except Exception:
                    pass
                validated_count += 1
            round_scores[uid] = {"points": pts, "validated": validated_count, "submitted_any": submitted_any}
            g["scores"][uid] = g["scores"].get(uid, 0) + pts
            await update_after_round(uid, validated_count, submitted_any)

        g["round_scores_history"] = g.get("round_scores_history", []) + [round_scores]

        # summary message with emojis
        header = f"<b>🏁 Round {r} Results</b>\nLetter: <b>{g['round_letter']}</b>\n\n"
        body = ""
        sorted_players = sorted(g["players"].items(), key=lambda x: -g["scores"].get(x[0],0))
        for pos, (uid, name) in enumerate(sorted_players, start=1):
            pts = g["scores"].get(uid, 0)
            if pos == 1:
                emoji = EMOJI_WINNER
            elif pos == 2:
                emoji = EMOJI_SECOND
            elif pos == 3:
                emoji = EMOJI_THIRD
            else:
                emoji = ""
            body += f"{emoji} {name} — <code>{pts}</code>\n"
        try:
            await context.bot.send_message(chat_id, header + body, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id, header + body)

        await asyncio.sleep(1)

    # final leaderboard
    lb = sorted(g["scores"].items(), key=lambda x: -x[1])
    text = "<b>Game Over — Final Scores</b>\n\n"
    for uid, pts in lb:
        text += f"{g['players'][uid]} — <code>{pts}</code>\n"
    await context.bot.send_message(chat_id, text, parse_mode="HTML")
    games.pop(chat_id, None)
