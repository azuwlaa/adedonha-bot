# ai_validation.py — hybrid validation using local DB and OpenAI
import logging
from typing import List, Dict
import re, json, time
from config import AI_MODEL, OPENAI_API_KEY, AI_MAX_RETRIES
from db import check_word, save_word

logger = logging.getLogger(__name__)

ai_client = None
if OPENAI_API_KEY and not OPENAI_API_KEY.startswith("YOUR_"):
    try:
        from openai import OpenAI
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning("OpenAI init failed: %s", e)
        ai_client = None

async def batch_validate(letter: str, categories: List[str], answers: List[str]) -> Dict[int, bool]:
    """
    Returns mapping index -> True/False for each answer.
    Uses local DB first, then queries AI only for unknowns. Saves AI-approved to DB.
    """
    result = {}
    n = len(categories)
    # Pre-check basic criteria
    for i in range(n):
        a = answers[i].strip() if i < len(answers) else ""
        if not a:
            result[i] = False
            continue
        if not a[0].isalpha() or a[0].upper() != letter.upper():
            result[i] = False
            continue
        result[i] = None  # undecided

    # Check local DB
    indexes_to_ask = []
    for i in range(n):
        if result[i] is None:
            found = await check_word(answers[i], categories[i], letter)
            if found:
                result[i] = True
            else:
                indexes_to_ask.append(i)

    # If no AI client, accept remaining as True (permissive fallback)
    if ai_client is None:
        for i in indexes_to_ask:
            result[i] = True
            # save to DB for future
            try:
                await save_word(answers[i], categories[i], letter)
            except Exception:
                pass
        return result

    if not indexes_to_ask:
        return result

    # Build prompt only for needed items
    sb = []
    sb.append("You are a terse validator for the game Adedonha.")
    sb.append("Rules:")
    sb.append(f"- The answer must start with the letter '{letter}' (case-insensitive). ")
    sb.append("- It must belong to the category provided.")
    sb.append("For each item return either YES or NO in JSON form mapping indexes to true/false. Example: {\"0\": true, \"1\": false}")
    sb.append("Do not add explanations.")
    sb.append("Items:")
    for i in indexes_to_ask:
        sb.append(f"{i}. Category: {categories[i]} — Answer: {answers[i]}")
    prompt = "\n".join(sb)

    attempt = 0
    parsed = None
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
            # Map parsed to result
            for i in indexes_to_ask:
                ok = False
                if parsed is not None:
                    key = str(i)
                    if key in parsed:
                        ok = bool(parsed[key])
                result[i] = ok
                # Save positives to local DB
                if ok:
                    try:
                        await save_word(answers[i], categories[i], letter)
                    except Exception:
                        pass
            return result
        except Exception as e:
            logger.warning("AI validate attempt %s failed: %s", attempt, e)
            time.sleep(0.5)
            continue

    # final fallback: accept remaining and save
    for i in indexes_to_ask:
        result[i] = True
        try:
            await save_word(answers[i], categories[i], letter)
        except Exception:
            pass
    return result
