# utils.py - constants and small helpers
import random
import html as _html
from typing import List, Optional

# ---------------- CONFIG (set your tokens here) ----------------
TELEGRAM_BOT_TOKEN = ""  # set before running
OPENAI_API_KEY = ""      # optional - leave empty to use manual admin validation

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
    "City",
    "Country",
    "State",
    "Food",
    "Color",
    "Movie/Series/TV Show",
    "Place",
    "Fruit",
    "Profession",
    "Adjective",
]

# Shared in-memory games state (chat_id -> game dict)
games = {}  # this will be imported and mutated by handlers/game

# ---------------- UTIL FUNCTIONS ----------------
def escape_html(text: Optional[str]) -> str:
    if text is None:
        return ""
    return _html.escape(str(text), quote=False)

def user_mention_html(uid: int, name: str) -> str:
    # produces: <a href="tg://user?id=UID">Name</a>
    return f'<a href="tg://user?id={uid}">{escape_html(name)}</a>'

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
