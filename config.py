# config.py — configuration and constants
import os

TELEGRAM_BOT_TOKEN = ""  # fill your token
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Game UI / behavior
ROUND_OPTIONS = list(range(6,13))  # 6..12 rounds available
DEFAULT_ROUNDS = 6
ROUND_DURATION_SECONDS = 3 * 60  # each round 3 minutes
CATEGORIES_PER_ROUND = 5
PLAYER_EMOJI = "🧑"
EMOJI_VALIDATE = "🤖"
EMOJI_SUCCESS = "✅"
EMOJI_GAME = "🎮"
EMOJI_INFO = "ℹ️"
EMOJI_CLOCK = "⏱️"
POINTS_PER_VALID = 10

# Template for rounds (monospace)
ROUND_TEMPLATE = "```
Letter: {letter}
Categories:
{cats}
```"

# Database / persistence path (optional)
STORE_FILE = "/mnt/data/adedonha_store.json"

# Owners (string ids)
OWNERS = {"624102836", "1707015091"}
