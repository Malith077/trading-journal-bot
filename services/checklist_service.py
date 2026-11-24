"""
The trade entry checklist, maintained from a Discord channel.

You post the checklist in #trade_checklist, one item per line, and edit or
delete those messages to change it. The bot reads the channel and saves the
result to LIVE_CHECKLIST_PATH, which takes precedence over the default
trade_checklist.json in the repo.

The live list is a runtime file, not a repo file: it is gitignored and excluded
from deploy's rsync, so a deploy never resets your edits back to the default.
"""
import json
import os
import re
from pathlib import Path

from config import BASE_DIR

CHECKLIST_CHANNEL_NAME = "trade_checklist"
LIVE_CHECKLIST_PATH = BASE_DIR / "trade_checklist_live.json"

# Discord allows 25 components per message, and every item is a button.
MAX_ITEMS = 25

# Leading list markers people naturally type: "-", "*", "•", "1.", "2)",
# "[ ]", "[x]", and the ⬜/✅ the checklist itself displays.
_MARKER = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)]|\[[ xX]?\]|[⬜✅☐☑✔])\s*")


def parse_items(texts) -> tuple[list[str], int]:
    """
    Turn channel messages into checklist items.

    Every non-empty line is one item, in posting order; list markers and code
    fences are stripped and exact duplicates dropped. Returns (items, overflow)
    where overflow counts items beyond MAX_ITEMS, which are left out.
    """
    items = []
    for text in texts:
        for line in (text or "").splitlines():
            if line.strip().startswith("```"):
                continue
            item = _MARKER.sub("", line, count=1).strip()
            if item and item not in items:
                items.append(item)
    return items[:MAX_ITEMS], max(0, len(items) - MAX_ITEMS)


def save_live(items: list[str], path: Path | None = None) -> None:
    """Atomically write the live checklist."""
    path = Path(path or LIVE_CHECKLIST_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(items, indent=4, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
