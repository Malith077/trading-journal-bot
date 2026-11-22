"""
Parsing helpers for model responses.

Models are asked for strict JSON but don't always comply, so extraction is
deliberately forgiving. Shared by every cog that asks an LLM for structured
output.
"""
import os
import re
import json
from pathlib import Path


def extract_json(raw: str, fallback: dict) -> dict:
    """
    Robustly extract a JSON object from a model response.

    Tries three methods in order:
      1. Direct parse (model returned clean JSON)
      2. Regex extraction (JSON buried in surrounding text)
      3. A copy of `fallback` (response was completely unparseable)

    The fallback is always copied, so callers can safely pass a shared constant
    and mutate what they get back.
    """
    cleaned = (raw or "").replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Prefer the largest brace-delimited span so nested objects survive.
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Fall back to the first non-greedy object, which handles trailing prose
    # that would otherwise swallow a closing brace.
    match = re.search(r'\{.*?\}', cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    print(f"[WARN] Could not parse JSON from model response. Using empty fallback.\nRaw: {(raw or '')[:300]}")
    return json.loads(json.dumps(fallback))  # deep copy — never alias the caller's constant


async def write_json_atomic(path, data) -> None:
    """
    Write JSON via a temp file and rename, so a crash or restart mid-write can
    never leave a half-written file behind. Used wherever insights are saved
    incrementally during a long run.
    """
    import aiofiles
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, indent=4, ensure_ascii=False))
    os.replace(tmp, path)
