# ai.py - AI client and validation helper
import logging
from .utils import OPENAI_API_KEY, AI_MODEL
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

logger = logging.getLogger(__name__)

ai_client = None
if OPENAI_API_KEY and not OPENAI_API_KEY.startswith("YOUR_") and OpenAI is not None:
    try:
        ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning("OpenAI client init failed: %s. Bot will fall back to manual admin validation.", e)
        ai_client = None

async def ai_validate(category: str, answer: str, letter: str) -> bool:
    if not answer:
        return False
    if not answer[0].isalpha() or answer[0].upper() != letter.upper():
        return False
    if not ai_client:
        # permissive fallback so gameplay continues; admins can manually validate
        return True
    prompt = f\"\"\"You are a terse validator for the game Adedonha.
Rules:
- The answer must start with the letter '{letter}' (case-insensitive).
- It must correctly belong to the category: '{category}'.
Respond with only YES or NO.
Answer: {answer}
\"\"\"
    try:
        resp = ai_client.responses.create(model=AI_MODEL, input=prompt, max_output_tokens=6)
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
        out = out.strip().upper()
        return out.startswith("YES")
    except Exception as e:
        logger.warning("AI validation error: %s", e)
        return True
