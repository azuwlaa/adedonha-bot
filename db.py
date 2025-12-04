# db.py — simple JSON-backed storage and helpers used by game engine
import json, os
from typing import Dict, Any

STORE_FILE = "/mnt/data/adedonha_store.json"

_state = {"games": {}, "words": {}}  # words can be used as known-good cache

def load_store():
    global _state
    try:
        if os.path.exists(STORE_FILE):
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
    except Exception:
        _state = {"games": {}, "words": {}}

def save_store():
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_game(chat_id):
    load_store()
    return _state["games"].get(str(chat_id))

def set_game(chat_id, data):
    load_store()
    _state["games"][str(chat_id)] = data
    save_store()

def del_game(chat_id):
    load_store()
    _state["games"].pop(str(chat_id), None)
    save_store()

def save_word(word, valid=True):
    load_store()
    _state["words"][word.lower()] = {"valid": bool(valid)}
    save_store()

def is_known_word(word):
    load_store()
    return _state["words"].get(word.lower())

# helpers expected by original code (no-op safe implementations)
def update_after_round(chat_id, round_num, round_results):
    # called by game engine to persist after a round
    g = get_game(chat_id) or {}
    g.setdefault("history",[]).append({"round": round_num, "results": round_results})
    set_game(chat_id, g)

def update_after_game(chat_id, final_results):
    # called at game end
    g = get_game(chat_id) or {}
    g["final_results"] = final_results
    set_game(chat_id, g)
