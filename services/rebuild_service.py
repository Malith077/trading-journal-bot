"""
File handling for /rebuild_all.

The rule this module exists to enforce: a rebuild deletes only what can be
rebuilt from Discord. Deletion works from an ALLOWLIST — `history.json` and the
chart images sync downloads, named `<message_id>_<filename>` — so any file not
on it survives, including user-authored ones this code has never heard of:
reflections, psych state, outcomes, missed-trade reviews, checklist state.

It also only touches folders whose channel still exists in Discord. A folder
whose channel was deleted holds the only remaining copy of that trade, so it
is left exactly as it is.
"""
import json
import re
import shutil
import datetime as dt
from pathlib import Path

HISTORY_FILE = "history.json"
# Sync saves attachments as f"{message.id}_{attachment.filename}".
DOWNLOADED_IMAGE_RE = re.compile(r"^\d+_.+\.(png|jpe?g|webp)$", re.IGNORECASE)


def is_derived(path: Path) -> bool:
    """True only for files sync recreates from Discord."""
    return path.name == HISTORY_FILE or bool(DOWNLOADED_IMAGE_RE.match(path.name))


def read_tracker(path: Path) -> int:
    try:
        content = Path(path).read_text().strip()
    except (FileNotFoundError, OSError):
        return 0
    return int(content) if content.isdigit() else 0


def count_messages(base_dir: Path) -> int:
    """Unique message ids across every history.json under a downloads tree."""
    ids = set()
    if not Path(base_dir).exists():
        return 0
    for history in Path(base_dir).glob(f"*/{HISTORY_FILE}"):
        try:
            for entry in json.loads(history.read_text(encoding="utf-8")):
                ids.add(str(entry.get("message_id")))
        except (json.JSONDecodeError, OSError, AttributeError):
            continue
    return len(ids)


def classify_folders(base_dir: Path, live_channels: set[str]) -> tuple[list[Path], list[Path]]:
    """Split channel folders into (live, orphaned) by whether the channel still exists."""
    live, orphaned = [], []
    if not Path(base_dir).exists():
        return live, orphaned
    for folder in sorted(p for p in Path(base_dir).iterdir() if p.is_dir()):
        (live if folder.name in live_channels else orphaned).append(folder)
    return live, orphaned


def count_preserved(base_dirs: list[Path]) -> int:
    """How many non-derived files exist — everything a rebuild must not touch."""
    total = 0
    for base in base_dirs:
        if Path(base).exists():
            total += sum(1 for f in Path(base).glob("*/*") if f.is_file() and not is_derived(f))
    return total


def backup(dest: Path, files: list[Path], kb_dir: Path, download_dirs: list[Path]) -> Path:
    """
    Snapshot everything the rebuild will overwrite or delete.

    Charts are skipped: they're large, and every chart that gets deleted
    belongs to a live channel, so sync downloads it again. history.json is
    small and is backed up for every folder, live or orphaned.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    for f in files:
        if Path(f).exists():
            shutil.copy2(f, dest / Path(f).name)
    if Path(kb_dir).exists():
        shutil.copytree(kb_dir, dest / Path(kb_dir).name)
    for base in download_dirs:
        base = Path(base)
        for history in base.glob(f"*/{HISTORY_FILE}"):
            target = dest / "downloads" / base.name / history.parent.name / HISTORY_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(history, target)
    return dest


def wipe_derived(base_dir: Path, live_channels: set[str]) -> int:
    """Delete derived files in live channel folders only. Returns files removed."""
    removed = 0
    live, _ = classify_folders(base_dir, live_channels)
    for folder in live:
        for f in folder.iterdir():
            if f.is_file() and is_derived(f):
                f.unlink()
                removed += 1
    return removed


def backup_name(now: dt.datetime) -> str:
    return f"rebuild-{now.strftime('%Y%m%d-%H%M%S')}"
