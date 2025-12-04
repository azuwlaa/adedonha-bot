# game.py - main game loop and scoring
import asyncio
import random
import re
from datetime import datetime
from typing import Dict, List

from .utils import (
    games,
    escape_html,
    user_mention_html,
    ALL_CATEGORIES,
    PLAYER_EMOJI,
    CLASSIC_FIRST_WINDOW,
    CLASSIC_NO_SUBMIT_TIMEOUT,
    FAST_FIRST_WINDOW,
    FAST_ROUND_SECONDS,
    TOTAL_ROUNDS_CLASSIC,
    TOTAL_ROUNDS_FAST,
)
from .database import db_update_after_round, db_update_after_game
from .ai import ai_validate

async def run_game(chat_id: int, context):
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
    await db_update_after_game(list(g["players"].keys()))
    
    for r in range(1, rounds + 1):
        g["round"] = r
        if mode == "classic":
            categories = ["Name", "Object", "Animal", "Plant", "Country"]
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

        cat_lines_plain = "\n".join(f"{i+1}. {escape_html(c)}:" for i, c in enumerate(categories))
        letter_html = f"<b>{escape_html(letter)}</b>"
        pre_block = "\n".join(f"{i+1}. {escape_html(c)}:" for i, c in enumerate(categories))
        intro = (f"Round {r} / {rounds}\nLetter: {letter_html}\n\n" +
                f"<pre>{pre_block}</pre>\n\n" +
                f"Send your answers in ONE MESSAGE using the template above (first {len(categories)} answers will be used).\n")
        intro += f"First submission starts a {window_seconds}s window for others (fast mode total round {FAST_ROUND_SECONDS}s)."
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
                g["round_scores_history"] = g.get("round_scores_history", []) + [{}]
                end_event.set()
        no_submit_task = asyncio.create_task(no_submit_worker())
        # wait loop - submissions are collected via submission_handler in handlers.py
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
            parsed[uid] = []
            # determine expected count
            expected = len(categories)
            # reuse simple extraction: split lines
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            for line in lines[:expected]:
                if ":" in line:
                    parsed[uid].append(line.split(":",1)[1].strip())
                else:
                    parsed[uid].append(line.strip())
            while len(parsed[uid]) < expected:
                parsed[uid].append("")
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
                if not valid:
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
            await db_update_after_round(uid, validated_count, submitted_any)
        g["round_scores_history"] = g.get("round_scores_history", []) + [round_scores]
        # summary message
        header = f"<b>Round {r} Results</b>\nLetter: <b>{escape_html(letter)}</b>\n\n"
        body = ""
        sorted_players = sorted(g["players"].items(), key=lambda x: -g["scores"].get(x[0],0))
        for uid, name in sorted_players:
            pts = round_scores.get(uid, {}).get("points", 0)
            body += f"{user_mention_html(int(uid), name)} — <code>{pts}</code>\n"
        try:
            await context.bot.send_message(chat_id, header + body, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id, header + body)
        await asyncio.sleep(1)
    # final leaderboard
    lb = sorted(g["scores"].items(), key=lambda x: -x[1])
    text = "<b>Game Over — Final Scores</b>\n\n"
    for uid, pts in lb:
        text += f"{user_mention_html(int(uid), g['players'][uid])} — <code>{pts}</code>\n"
    await context.bot.send_message(chat_id, text, parse_mode="HTML")
    # cleanup
    games.pop(chat_id, None)
