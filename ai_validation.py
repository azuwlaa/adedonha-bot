# ai_validation.py — batch AI validation (one request per player per round)
import logging
from typing import List, Dict
from config import AI_MODEL, OPENAI_API_KEY, AI_MAX_RETRIES

logger = logging.getLogger(__name__)

ai_client = None
if OPENAI_API_KEY and not OPENAI_API_KEY.startswith("YOUR_"):
    try:
        from openai import OpenAI
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning("OpenAI client init failed: %s", e)
        ai_client = None

async def batch_validate(letter: str, categories: List[str], answers: List[str]) -> Dict[int, bool]:
    """
    Ask the AI once for the full list of answers for a single player.
    Returns a mapping index -> True/False.
    If AI is not configured or an error occurs, returns permissive True for items that start with the letter.
    """
    result = {}
    for i, a in enumerate(answers):
        if not a or not a.strip():
            result[i] = False
            continue
        if not a.strip()[0].isalpha() or a.strip()[0].upper() != letter.upper():
            result[i] = False
            continue
        result[i] = None

    if ai_client is None:
        for i, v in list(result.items()):
            if v is None:
                result[i] = True
        return result

    sb = []
    sb.append("You are a terse validator for the game Adedonha.")
    sb.append("Rules:")
    sb.append(f"- The answer must start with the letter '{letter}' (case-insensitive). ")
    sb.append(f"- It must belong to the category provided next to it.")
    sb.append("For each item return either YES or NO, in JSON form mapping indexes to true/false. Example: {'0': true, '1': false}")
    sb.append("Do not add explanations. Return only a single JSON object.")
    sb.append("Items:")
    for i, (cat, ans) in enumerate(zip(categories, answers)):
        sb.append(f"{i}. Category: {cat} — Answer: {ans}")
    prompt = "\n".join(sb)

    attempt = 0
    import time, re, json
    while attempt <= AI_MAX_RETRIES:
        try:
            attempt += 1
            resp = ai_client.responses.create(model=AI_MODEL, input=prompt, max_output_tokens=300)
            out = ""
            if getattr(resp, "output", None):
                for block in resp.output:
                    if isinstance(block, dict):
                        content = block.get("content")
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and "text" in item:
                                    out += item["text"]
                                elif isinstance(item, str):
                                    out += item
                        elif isinstance(content, str):
                            out += content
                    elif isinstance(block, str):
                        out += block
            out = out.strip()
            m = re.search(r"\{.*\}", out, re.S)
            parsed = None
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except Exception:
                    parsed = None
            if parsed is None:
                parsed = {}
                for line in out.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    m2 = re.match(r"(\d+)\D*(YES|NO)", line, re.I)
                    if m2:
                        idx = int(m2.group(1))
                        parsed[str(idx)] = m2.group(2).upper().startswith("Y")
            for i, v in list(result.items()):
                if v is not None:
                    continue
                ok = False
                if parsed is not None:
                    key = str(i)
                    if key in parsed:
                        ok = bool(parsed[key])
                result[i] = ok
            return result
        except Exception as e:
            logger.warning("AI validate attempt %s failed: %s", attempt, e)
            time.sleep(0.5)
            continue
    for i, v in list(result.items()):
        if v is None:
            result[i] = True
    return result
