# ai_validation.py — handles word validation (stub)
import asyncio
import config
from db import is_known_word

async def validate_words(words):
    """Validate list of words. If OPENAI_API_KEY is set, this could call OpenAI.
    For now we use a local lookup: alphabetic & present in db -> valid.
    Returns dict: word -> (is_real_word:bool, reason:str)
    """
    results = {}
    for w in words:
        ww = w.strip()
        if not ww:
            results[w] = (False, "empty")
            continue
        if not ww.replace(' ','').isalpha():
            results[w] = (False, "not alphabetic")
            continue
        # local DB check
        if is_known_word(ww):
            results[w] = (True, "found in local db")
        else:
            # treat unknown as possibly real but unverified
            results[w] = (True, "assumed valid (not in local db)")
    await asyncio.sleep(0.8)  # simulate some processing
    return results
