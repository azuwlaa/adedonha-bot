# ai_validation.py — validation utilities.
# Provides batch_validate(letter, answers) which returns detailed info per category.
import re
from typing import Dict, Any
import config, db

def is_real_word_local(word: str) -> bool:
    # Basic heuristic: letters, hyphens, spaces; length >= 2
    if not word or not isinstance(word, str):
        return False
    w = word.strip()
    if len(w) < 2:
        return False
    return bool(re.fullmatch(r"[A-Za-z\-\' ]{2,}", w))

def batch_validate(letter: str, answers: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """
    Validate a dict of category->word for the given starting letter.
    Returns dict category -> {word, valid (bool), reason}
    """
    res = {}
    letter = (letter or "").strip().lower()[:1]
    for cat, word in answers.items():
        w = (word or "").strip()
        entry = {"word": w, "valid": False, "reason": ""}
        if not w:
            entry["reason"] = "empty"
        else:
            # Check known cache first
            known = db.is_known_word(w)
            if known is not None:
                # If cached, use it but still check starting letter
                if w[0].lower() != letter:
                    entry["reason"] = f"wrong_letter (expected '{letter}')"
                else:
                    entry["valid"] = bool(known.get("valid", False))
                    entry["reason"] = "ok_cached" if entry["valid"] else "not_word_cached"
            else:
                # local heuristic
                if not is_real_word_local(w):
                    entry["reason"] = "not_word_local"
                elif w[0].lower() != letter:
                    entry["reason"] = f"wrong_letter (expected '{letter}')"
                else:
                    entry["valid"] = True
                    entry["reason"] = "ok_local"
                    # save to cache for future
                    db.save_word(w, valid=True)
        res[cat] = entry
    return res
