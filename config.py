# config.py — edit tokens here

# ---------------- TOKENS ----------------
TELEGRAM_BOT_TOKEN = ""  # set your bot token here (string)
OPENAI_API_KEY = ""      # optional — leave empty to disable AI

# ---------------- OWNERS / ADMINS ----------------
OWNERS = {"624102836", "1707015091"}  # string IDs

# ---------------- GAME CONSTANTS ----------------
MAX_PLAYERS = 10
LOBBY_TIMEOUT = 5 * 60
CLASSIC_NO_SUBMIT_TIMEOUT = 3 * 60
CLASSIC_FIRST_WINDOW = 2
FAST_ROUND_SECONDS = 60
FAST_FIRST_WINDOW = 2
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

# Display emojis for round-end
EMOJI_WINNER = "🏆"
EMOJI_SECOND = "✨"
EMOJI_THIRD = "🍀"

# AI batch validation settings
AI_MAX_RETRIES = 2
AI_TIMEOUT_SECONDS = 10
