# game.py — core engine and state management
import random, asyncio
from datetime import datetime
from typing import Dict, Any
import config, db, ai_validation

# games runtime cache: chat_id -> game dict
games: Dict[int, Dict[str, Any]] = {}

# Default categories (can be overridden by modes in original code)
DEFAULT_CATEGORIES = [
    "Name", "Country", "Animal", "Food", "Color",
    "City", "Thing", "Movie", "Sport", "Plant"
]

def _persist(chat_id):
    g = games.get(chat_id)
    if g:
        db.set_game(chat_id, g)

def new_lobby(chat_id: int, creator_id: int, creator_name: str, rounds: int=None, mode="classic"):
    rounds = rounds or config.DEFAULT_ROUNDS
    g = {
        "chat_id": chat_id,
        "mode": mode,
        "creator_id": creator_id,
        "creator_name": creator_name,
        "state": "lobby",
        "created_at": datetime.utcnow().isoformat(),
        "rounds_total": rounds,
        "round_current": 0,
        "categories_per_round": config.CATEGORIES_PER_ROUND,
        "players": {str(creator_id): {"name": creator_name, "score": 0}},
        "submissions": {},  # round -> player_id -> answers dict
        "letter": random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        "lobby_message_id": None,
        "lobby_task": None,
        "started_at": None,
    }
    games[chat_id] = g
    _persist(chat_id)
    return g

def join_lobby(chat_id: int, user_id: int, user_name: str):
    g = games.get(chat_id) or db.get_game(chat_id)
    if not g or g.get("state") != "lobby":
        return False, "No active lobby."
    pid = str(user_id)
    if pid in g["players"]:
        return False, "Already joined."
    g["players"][pid] = {"name": user_name, "score": 0}
    _persist(chat_id)
    return True, "Joined."

def start_game(chat_id: int):
    g = games.get(chat_id) or db.get_game(chat_id)
    if not g:
        return False, "No lobby"
    if g.get("state") != "lobby":
        return False, "Game already started"
    g["state"] = "running"
    g["round_current"] = 1
    g["letter"] = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    g["started_at"] = datetime.utcnow().isoformat()
    _persist(chat_id)
    return True, g

def cancel_game(chat_id: int):
    games.pop(chat_id, None)
    db.del_game(chat_id)
    return True

def extract_answers_from_text(text: str, categories_per_round: int=None):
    """
    Parse a user's message into a list/dict of answers.
    Accepts one answer per line. Returns dict category->answer using DEFAULT_CATEGORIES order.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    cpr = categories_per_round or config.CATEGORIES_PER_ROUND
    answers = {}
    for i in range(cpr):
        cat = DEFAULT_CATEGORIES[i]
        answers[cat] = lines[i] if i < len(lines) else ""
    return answers

async def advance_round(chat_id: int, bot):
    """
    Validate current round submissions, award points, persist, and advance to next round or finish.
    """
    g = games.get(chat_id)
    if not g:
        return
    r = g.get("round_current", 0)
    submissions = g.get("submissions", {}).get(str(r), {})
    round_results = {}
    # Validate each player's submission
    for pid, answers in submissions.items():
        res = ai_validation.batch_validate(g.get("letter",""), answers)
        points = 0
        details = {}
        for cat, info in res.items():
            details[cat] = info
            if info.get("valid"):
                points += config.POINTS_PER_VALID
        # update score
        if pid in g["players"]:
            g["players"][pid]["score"] = g["players"][pid].get("score",0) + points
        round_results[pid] = {"points": points, "details": details}
    # persist round results
    db.update_after_round(chat_id, r, round_results)
    # Update game state
    if g["round_current"] >= g["rounds_total"]:
        g["state"] = "finished"
        db.update_after_game(chat_id, {"final_scores": {p: info["score"] for p,info in g["players"].items()}})
    else:
        g["round_current"] += 1
        g["letter"] = random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    _persist(chat_id)
    return round_results

def get_scores(chat_id: int):
    g = games.get(chat_id) or db.get_game(chat_id)
    if not g:
        return {}
    return {p: {"name": info.get("name"), "score": info.get("score",0)} for p,info in g.get("players",{}).items()}
