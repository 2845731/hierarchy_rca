"""Parse one final JSON value; Markdown fences are formatting, not extra answers."""
import json
import re

from .minimax_client import final_answer


def parse_model_json(text):
    cleaned = final_answer(text)
    fenced = re.fullmatch(r"```(?:json)?\s*\n?([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    while True:
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Some otherwise valid MiniMax JSON uses the JavaScript spelling
            # \' inside a double-quoted string. Apostrophes need no JSON escape.
            # Correct only the decoder-identified redundant escape: valid JSON
            # (including literal backslashes) and all other errors stay intact.
            if exc.msg != "Invalid \\escape" or cleaned[exc.pos:exc.pos + 2] != "\\'":
                raise
            cleaned = cleaned[:exc.pos] + cleaned[exc.pos + 1:]
