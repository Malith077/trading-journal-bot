"""
The #coach conversation: dates, the trade index, the coach's lookup tools, and
its memory.

Kept free of Discord so every piece can be tested directly. The cog in
cogs/coach_chat.py only wires messages in and replies out.

Three design rules run through this module:

  1. DATES ARE COMPUTED, NEVER INFERRED. Models are unreliable at weekday
     arithmetic, so every date the coach sees arrives already labelled with
     its weekday, in Melbourne time and New York (exchange) time, plus how long
     ago it was. The current date goes in the user's turn, not the system
     prompt, so the system prompt stays byte-identical and DeepSeek's prefix
     cache keeps hitting.
  2. NO CLAIMS WITHOUT LOOKUPS. The coach has tools onto the trader's own data
     and is told to use them rather than recall or guess.
  3. MEMORY IS REPLAYED EXACTLY. With tools in thinking mode, DeepSeek requires
     every earlier assistant message's reasoning_content to be sent back
     unchanged, so whole exchanges are stored as the API saw them and replayed
     verbatim; older ones are folded into a written summary instead.
"""
import datetime as dt
import difflib
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import aiofiles

from config import (
    COACH_CHAT_DIR,
    DEEPSEEK_PRICE_PER_M,
    INSIGHTS_PATH,
    LOCAL_TZ,
    MARKET_TZ,
    MISSED_DIR,
    MISSED_INSIGHTS_PATH,
    TRADES_DIR,
)
from services import coach_service as cs
from services import psych_service as ps
from services.json_utils import write_json_atomic

UTC = dt.timezone.utc
LOCAL = ZoneInfo(LOCAL_TZ)
MARKET = ZoneInfo(MARKET_TZ)

# Each tool result is capped so one broad lookup can't swamp the context.
MAX_TOOL_RESULT_CHARS = 6000
MAX_TRADE_CHARS = 12000

# Memory: replay recent exchanges verbatim, fold older ones into a summary.
KEEP_RECENT = 8
FOLD_AT = 12
MAX_REPLAY = 16

INDEX_TTL_SECONDS = 60
CHANNEL_RE = re.compile(r"^(trade|missed)_(\d+)_(.+)$")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ──────────────────────────────────────────────
# Dates
# ──────────────────────────────────────────────

def to_utc(value) -> dt.datetime | None:
    """A datetime or ISO string as an aware UTC datetime. Naive input is UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def fmt_date(day: dt.date, year: bool = True) -> str:
    """Tue 17 Nov 2026"""
    text = f"{WEEKDAYS[day.weekday()]} {day.day} {day.strftime('%b')}"
    return f"{text} {day.year}" if year else text


def fmt_long_date(day: dt.date) -> str:
    """Tuesday 17 November 2026"""
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')} {day.year}"


def fmt_local(moment, year: bool = True) -> str:
    """Tue 17 Nov 2026 01:30 AEDT, in Melbourne time."""
    local = to_utc(moment).astimezone(LOCAL)
    return f"{fmt_date(local.date(), year)} {local:%H:%M} {local.tzname()}"


def fmt_market(moment, year: bool = False) -> str:
    """Mon 16 Nov 09:30 New York"""
    ny = to_utc(moment).astimezone(MARKET)
    return f"{fmt_date(ny.date(), year)} {ny:%H:%M} New York"


def fmt_dual(moment) -> str:
    """Tue 17 Nov 2026 01:30 AEDT (Mon 16 Nov 09:30 New York)"""
    return f"{fmt_local(moment)} ({fmt_market(moment)})"


def local_date(moment) -> dt.date:
    return to_utc(moment).astimezone(LOCAL).date()


def relative_age(moment, now) -> str:
    """How long ago, counted in Melbourne calendar days."""
    days = (local_date(now) - local_date(moment)).days
    if days < 0:
        return "in the future"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    if days < 365:
        return f"{days // 30} months ago"
    years = days // 365
    return "1 year ago" if years == 1 else f"{years} years ago"


def now_block(now) -> str:
    """
    The calendar the coach reasons from, put at the top of every user turn.

    Spells out today, yesterday, both weeks and the last fortnight with their
    weekdays, so "last Monday" is a lookup rather than arithmetic.
    """
    now = to_utc(now)
    local = now.astimezone(LOCAL)
    ny = now.astimezone(MARKET)
    today = local.date()
    week_start = today - dt.timedelta(days=today.weekday())
    last_week_start = week_start - dt.timedelta(days=7)

    recent = []
    for offset in range(14):
        day = today - dt.timedelta(days=offset)
        label = fmt_date(day, year=False)
        if offset == 0:
            label += " (today)"
        elif offset == 1:
            label += " (yesterday)"
        recent.append(label)

    return "\n".join([
        f"[Now: {fmt_long_date(today)}, {local:%H:%M} {local.tzname()} in Melbourne"
        f" · New York: {fmt_long_date(ny.date())}, {ny:%H:%M} {ny.tzname()}]",
        f"[Today = {fmt_date(today)} · Yesterday = {fmt_date(today - dt.timedelta(days=1))}]",
        f"[This week = {fmt_date(week_start, False)} – {fmt_date(week_start + dt.timedelta(days=6))}"
        f" · Last week = {fmt_date(last_week_start, False)} – "
        f"{fmt_date(last_week_start + dt.timedelta(days=6))}]",
        f"[Last 14 days: {', '.join(recent)}]",
    ])


def stamp_user_message(text: str, sent_at) -> str:
    """What gets stored and replayed for a user turn: the calendar, then their words."""
    return f"{now_block(sent_at)}\n\n{text}"


def parse_day(value) -> dt.date:
    """YYYY-MM-DD → date. Raises ValueError with a message the model can act on."""
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{value!r} is not a YYYY-MM-DD date.")


def zone_for(name: str | None) -> ZoneInfo:
    return MARKET if (name or "").lower().replace(" ", "_") in ("new_york", "ny", "market") else LOCAL


def day_in(moment, tz: ZoneInfo) -> dt.date:
    return to_utc(moment).astimezone(tz).date()


def dates_info(dates, now) -> str:
    lines = []
    for value in dates or []:
        try:
            day = parse_day(value)
        except ValueError as e:
            lines.append(str(e))
            continue
        noon = dt.datetime.combine(day, dt.time(12), tzinfo=LOCAL)
        lines.append(f"{day.isoformat()} = {fmt_long_date(day)} ({relative_age(noon, now)})")
    return "\n".join(lines) or "No dates given."


# ──────────────────────────────────────────────
# Trade index
# ──────────────────────────────────────────────

def _read_json_sync(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _parse_channel(name: str):
    match = CHANNEL_RE.match(name)
    if not match:
        return None, None
    return int(match.group(2)), match.group(3).lower()


def build_index(trades_dir=None, missed_dir=None, bot_names=()) -> list[dict]:
    """
    One record per journal channel on disk, newest trade first.

    A renamed channel leaves its old folder behind holding the same messages.
    Folders are visited newest-synced first, and one whose messages were all
    already seen is skipped as a stale copy.
    """
    bot_names = set(bot_names or ())
    folders = []
    for kind, base in (("taken", Path(trades_dir or TRADES_DIR)),
                       ("missed", Path(missed_dir or MISSED_DIR))):
        if not base.exists():
            continue
        for folder in base.iterdir():
            history = folder / "history.json"
            if folder.is_dir() and history.exists():
                folders.append((history.stat().st_mtime, kind, folder))
    folders.sort(key=lambda f: f[0], reverse=True)

    seen: set[str] = set()
    records = []
    for _, kind, folder in folders:
        history = _read_json_sync(folder / "history.json", [])
        entries = [m for m in history if isinstance(m, dict)]
        ids = {str(m["message_id"]) for m in entries if m.get("message_id")}
        if ids and ids <= seen:
            continue
        seen |= ids

        messages = []
        for m in entries:
            if m.get("author") in bot_names:
                continue
            text = (m.get("content") or "").strip()
            images = len(m.get("images") or [])
            if not text and not images:
                continue
            messages.append({"time": to_utc(m.get("timestamp")), "text": text, "images": images})
        messages.sort(key=lambda m: m["time"] or dt.datetime.min.replace(tzinfo=UTC))

        times = [m["time"] for m in messages if m["time"]] or \
                [t for t in (to_utc(m.get("timestamp")) for m in entries) if t]
        number, asset = _parse_channel(folder.name)
        outcome = _read_json_sync(folder / "outcome.json", {})
        records.append({
            "channel": folder.name,
            "kind": kind,
            "number": number,
            "asset": asset,
            "first": min(times) if times else None,
            "last": max(times) if times else None,
            "messages": messages,
            "search_text": " ".join(m["text"] for m in messages).lower(),
            "reflections": _read_json_sync(folder / "reflections.json", {}),
            "state": _read_json_sync(folder / "psych.json", {}),
            "outcome": outcome.get("outcome"),
            "r": outcome.get("r_multiple") if isinstance(outcome.get("r_multiple"), (int, float)) else None,
            "review": _read_json_sync(folder / "review.json", {}),
        })

    records.sort(key=lambda r: r["first"] or dt.datetime.min.replace(tzinfo=UTC), reverse=True)
    return records


class TradeIndex:
    """build_index with a short cache: one reply may call several tools."""

    def __init__(self, trades_dir=None, missed_dir=None, ttl=INDEX_TTL_SECONDS, clock=time.monotonic):
        self.trades_dir = trades_dir
        self.missed_dir = missed_dir
        self.ttl = ttl
        self.clock = clock
        self._records = None
        self._built_at = 0.0

    def get(self, bot_names=()) -> list[dict]:
        if self._records is None or self.clock() - self._built_at > self.ttl:
            self._records = build_index(self.trades_dir, self.missed_dir, bot_names)
            self._built_at = self.clock()
        return self._records


# ──────────────────────────────────────────────
# Rendering trades
# ──────────────────────────────────────────────

def _fear_text(state: dict) -> str | None:
    fear = state.get("fear_spectrum")
    if not isinstance(fear, (int, float)):
        return None
    return f"fear {int(fear):+d} ({state.get('fear_zone') or ps.fear_zone(int(fear))})"


def _outcome_text(record: dict) -> str:
    if not record["outcome"]:
        return "outcome not recorded"
    text = ps.OUTCOME_LABELS.get(record["outcome"], record["outcome"]).lower()
    return f"{text} {record['r']:+g}R" if record["r"] is not None else text


def summarise_trade(record: dict, now) -> str:
    """One line per trade for search results."""
    parts = [record["channel"], record["kind"]]
    if record["first"]:
        parts.append(f"{fmt_dual(record['first'])}, {relative_age(record['first'], now)}")
    parts.append(_outcome_text(record))
    state = record["state"]
    if _fear_text(state):
        parts.append(_fear_text(state))
    if state.get("emotional_context"):
        parts.append("emotions: " + ", ".join(state["emotional_context"]))
    if state.get("conviction") is not None:
        parts.append(f"conviction {state['conviction']}/10")
    notes = sum(1 for m in record["messages"] if m["text"])
    charts = sum(m["images"] for m in record["messages"])
    parts.append(f"{notes} notes, {charts} charts")
    if record["reflections"]:
        parts.append("reflection form ✓")
    line = " · ".join(parts)
    first_note = next((m["text"] for m in record["messages"] if m["text"]), "")
    if first_note:
        line += f"\n    “{first_note[:160]}{'…' if len(first_note) > 160 else ''}”"
    return line


def render_trade(record: dict, now) -> str:
    """Everything known about one trade, for read_trade."""
    lines = [f"# {record['channel']} ({record['kind']} trade)"]
    if record["first"]:
        lines.append(f"Started: {fmt_dual(record['first'])} — {relative_age(record['first'], now)}")
    if record["last"] and record["last"] != record["first"]:
        lines.append(f"Last note: {fmt_dual(record['last'])}")
    lines.append(f"Outcome: {_outcome_text(record)}")

    context = ps.format_state_context(record["state"],
                                      {"outcome": record["outcome"], "r_multiple": record["r"]}
                                      if record["outcome"] else None)
    if context:
        lines.append(context.strip())

    reflections = record["reflections"]
    if reflections:
        lines.append("\nReflection form:")
        for key, label in (("why_entered", "Why I entered"), ("how_felt", "How I felt"),
                           ("what_learned", "What I learned"), ("reminds_me_of", "What it reminded me of")):
            if reflections.get(key):
                lines.append(f"- {label}: {reflections[key]}")

    review = record["review"]
    if review:
        lines.append("\nMissed-trade review:")
        for key, value in review.items():
            if value:
                lines.append(f"- {key.replace('_', ' ')}: {value}")

    lines.append("\nJournal notes:")
    for m in record["messages"]:
        stamp = f"[{fmt_local(m['time'], year=False)} / {fmt_market(m['time'])}]" if m["time"] else "[undated]"
        charts = f" (+{m['images']} chart{'s' if m['images'] > 1 else ''})" if m["images"] else ""
        lines.append(f"{stamp} {m['text'] or '(chart only)'}{charts}")
    return cap("\n".join(lines), MAX_TRADE_CHARS)


def cap(text: str, limit: int | None = None) -> str:
    limit = limit or MAX_TOOL_RESULT_CHARS
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + f"\n… [cut at {limit} characters — narrow the lookup to see more]"


# ──────────────────────────────────────────────
# Filtering and stats
# ──────────────────────────────────────────────

def filter_trades(records, *, asset=None, kind=None, date_from=None, date_to=None,
                  date_tz=None, weekday=None, outcome=None, keywords=None,
                  fear_min=None, fear_max=None, emotion=None) -> list[dict]:
    tz = zone_for(date_tz)
    start = parse_day(date_from) if date_from else None
    end = parse_day(date_to) if date_to else None
    wanted_day = None
    if weekday:
        key = str(weekday).strip()[:3].title()
        if key not in WEEKDAYS:
            raise ValueError(f"{weekday!r} is not a weekday.")
        wanted_day = WEEKDAYS.index(key)
    words = [w for w in re.split(r"\s+", (keywords or "").lower()) if w]

    result = []
    for r in records:
        if kind and kind != "all" and r["kind"] != kind:
            continue
        if asset and (r["asset"] or "") != str(asset).lower().strip():
            continue
        if start or end or wanted_day is not None:
            if not r["first"]:
                continue
            day = day_in(r["first"], tz)
            if start and day < start:
                continue
            if end and day > end:
                continue
            if wanted_day is not None and day.weekday() != wanted_day:
                continue
        if outcome:
            if outcome == "none":
                if r["outcome"]:
                    continue
            elif r["outcome"] != outcome:
                continue
        if words and not all(w in r["search_text"] for w in words):
            continue
        fear = r["state"].get("fear_spectrum")
        if fear_min is not None and not (isinstance(fear, (int, float)) and fear >= fear_min):
            continue
        if fear_max is not None and not (isinstance(fear, (int, float)) and fear <= fear_max):
            continue
        if emotion and emotion.lower() not in [e.lower() for e in r["state"].get("emotional_context") or []]:
            continue
        result.append(r)
    return result


def _rate_text(bucket: dict) -> str:
    rate = bucket.get("win_rate")
    text = f"{bucket['n']} trades, {bucket['wins']}W/{bucket['losses']}L/{bucket['breakeven']}BE"
    if rate is not None:
        text += f", win rate {rate:.0%}"
    if bucket.get("avg_r") is not None:
        text += f", avg {bucket['avg_r']:+.2f}R"
    return text


def trade_stats(records) -> str:
    """Counts and rates over a filtered set, by outcome, asset and weekday."""
    if not records:
        return "No trades match those filters."

    decided = [r for r in records if r["outcome"]]
    lines = [f"{len(records)} trades match; {len(decided)} have an outcome recorded with /outcome."]
    if records[-1]["first"] and records[0]["first"]:
        lines.append(f"Range: {fmt_local(records[-1]['first'])} → {fmt_local(records[0]['first'])}")

    def tally(rows):
        bucket = ps._tally()
        for r in rows:
            ps._record(bucket, r["outcome"], r["r"])
        return ps._finalise(bucket)

    if decided:
        lines.append(f"Overall: {_rate_text(tally(decided))}")

    def grouped(title, key_fn, order=None):
        groups: dict = {}
        for r in records:
            key = key_fn(r)
            if key is not None:
                groups.setdefault(key, []).append(r)
        if not groups:
            return
        lines.append(f"\n{title}:")
        keys = order if order else sorted(groups, key=lambda k: -len(groups[k]))
        for key in keys:
            if key not in groups:
                continue
            rows = groups[key]
            outcomes = [r for r in rows if r["outcome"]]
            text = _rate_text(tally(outcomes)) if outcomes else "no outcomes recorded"
            lines.append(f"- {key}: {len(rows)} trades — {text}")

    grouped("By asset", lambda r: r["asset"])
    grouped("By weekday, New York session date",
            lambda r: WEEKDAYS[day_in(r["first"], MARKET).weekday()] if r["first"] else None, WEEKDAYS)
    grouped("By weekday, Melbourne date",
            lambda r: WEEKDAYS[day_in(r["first"], LOCAL).weekday()] if r["first"] else None, WEEKDAYS)

    entries = [{**r["state"], "outcome": r["outcome"], "r_multiple": r["r"]}
               for r in decided if r["state"]]
    if entries:
        corr = ps.correlate(entries)
        lines.append(f"\nPsychology vs outcome ({len(entries)} trades with both /state and /outcome):")
        for title, key in (("Fear zone", "by_fear_zone"), ("Emotion", "by_emotion"),
                           ("Intuition/impulse", "by_signal"), ("Physical feeling", "by_feelings")):
            for name, bucket in sorted(corr[key].items(), key=lambda kv: -kv[1]["n"]):
                lines.append(f"- {title} {name}: {_rate_text(bucket)}")
        conviction = corr["conviction"]
        if conviction["n"]:
            verdict = {True: "high conviction does beat low",
                       False: "high conviction does NOT beat low",
                       None: "not enough decided trades at both ends to say"}[conviction["predictive"]]
            lines.append(f"- Conviction ({conviction['n']} trades): {verdict}")
            for name, bucket in conviction["buckets"].items():
                if bucket["n"]:
                    lines.append(f"  - {name}: {_rate_text(bucket)}")
    else:
        lines.append("\nNo trades in this set have both a /state reading and an /outcome, "
                     "so psychology can't be checked against results here.")
    return cap("\n".join(lines))


# ──────────────────────────────────────────────
# Memory
# ──────────────────────────────────────────────

EMPTY_MEMORY = {"summary": "", "summarized_through": 0, "notes": [], "next_note_id": 1,
                "peak_notice_until": None}


class ChatMemory:
    """
    conversation.jsonl — every exchange, append-only, never pruned.
    memory.json        — running summary, coach notes, small bits of state.
    """

    def __init__(self, directory=None):
        self.dir = Path(directory or COACH_CHAT_DIR)
        self.conversation_path = self.dir / "conversation.jsonl"
        self.memory_path = self.dir / "memory.json"

    async def load(self) -> dict:
        try:
            async with aiofiles.open(self.memory_path, "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        return {**json.loads(json.dumps(EMPTY_MEMORY)), **(data if isinstance(data, dict) else {})}

    async def save(self, memory: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        await write_json_atomic(self.memory_path, memory)

    async def exchanges(self) -> list[dict]:
        if not self.conversation_path.exists():
            return []
        async with aiofiles.open(self.conversation_path, "r", encoding="utf-8") as f:
            content = await f.read()
        result = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("messages"), list):
                result.append(record)
        return result

    async def append(self, exchange: dict) -> dict:
        """Store an exchange, numbering it after the last one on disk."""
        existing = await self.exchanges()
        exchange = {**exchange, "id": (existing[-1]["id"] + 1) if existing else 1}
        self.dir.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self.conversation_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(exchange, ensure_ascii=False) + "\n")
        return exchange

    async def unsummarized(self) -> list[dict]:
        memory = await self.load()
        return [e for e in await self.exchanges() if e["id"] > memory["summarized_through"]]

    async def replay(self) -> list[dict]:
        """The API messages of recent exchanges, exactly as they were sent."""
        messages = []
        for exchange in (await self.unsummarized())[-MAX_REPLAY:]:
            messages.extend(exchange["messages"])
        return messages

    async def add_note(self, text: str, now) -> dict:
        memory = await self.load()
        note = {"id": memory["next_note_id"], "text": text.strip(), "created": to_utc(now).isoformat()}
        memory["notes"].append(note)
        memory["next_note_id"] += 1
        await self.save(memory)
        return note

    async def remove_note(self, note_id: int) -> dict | None:
        memory = await self.load()
        for note in memory["notes"]:
            if note["id"] == note_id:
                memory["notes"].remove(note)
                await self.save(memory)
                return note
        return None


def api_message(response: dict, drop_tool_calls: bool = False) -> dict:
    """
    An assistant reply in the exact shape to store and send back.

    reasoning_content is kept: DeepSeek rejects a tools request whose earlier
    assistant turns lost it. Empty optional fields are left out.
    """
    message = {"role": "assistant", "content": response.get("content") or ""}
    if response.get("reasoning_content"):
        message["reasoning_content"] = response["reasoning_content"]
    if response.get("tool_calls") and not drop_tool_calls:
        message["tool_calls"] = response["tool_calls"]
    return message


def build_fold_prompt(summary: str, exchanges: list[dict]) -> str:
    """Prompt that rolls old exchanges into the running summary."""
    transcript = []
    for e in exchanges:
        moment = to_utc(e.get("time"))
        when = fmt_local(moment) if moment else "undated"
        transcript.append(f"[{when}]\nTrader: {e.get('user', '')}\nCoach: {e.get('reply', '')}")
    return f"""You maintain the long-term memory of a trading coach.

Below is the existing summary of earlier conversations, then newer conversations to fold in.
Write the updated summary: under 400 words, plain text, oldest to newest.

Keep: dates as weekday + date (e.g. "Tue 17 Nov 2026"); what the trader was dealing with;
patterns the coach pointed out and the evidence (trade channels); commitments the trader
made and whether they followed through; open questions to come back to.
Drop: small talk, repetition, and anything that only mattered in the moment.

EXISTING SUMMARY:
{summary or "(none yet)"}

NEWER CONVERSATIONS:
{chr(10).join(transcript)}

Updated summary:"""


# ──────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────

COACH_PERSONA = """You are this trader's performance coach, in an ongoing conversation in a Discord channel.
You work in Denise Shull's framework: the numbered principles below are your source of truth.

WHO YOU ARE
- A coach, not an assistant. You don't just answer what's asked; you work out what's going on.
- Understand before advising. When the picture is unclear, ask ONE good question rather than giving advice.
- Notice patterns across trades and across conversations, and name them, with the evidence.
- When the trader's story doesn't match their own data, say so plainly and show the trades.
- Follow up on commitments in your notes ("Last week you said you'd wait for the hourly C2 — how did that go?").
- Warm but direct. No flattery, no padding, no lectures.
- Replies are Discord messages: usually under 200 words, conversational, no headings.
  Use a short list only when comparing several trades.

EVIDENCE
- Never state facts about the trader's trades, results or history from memory or assumption.
  Look them up with your tools in this turn, then cite the channel and its weekday + date,
  e.g. "trade_412_gc (Mon 16 Nov, New York)".
- If the data isn't there (no /state reading, no /outcome recorded), say so instead of guessing.
- The data is as of the last sync of the trade channels; very recent notes may not be in it yet.
- You can't see chart images; you only know a note had charts attached.

DATES — get these exactly right
- Every trader message starts with a [Now: …] calendar. It is the only source of today's date.
- Resolve "today", "yesterday", "last Monday", "this week" from that calendar. Never work out
  a weekday yourself — use the calendar, or call dates_info.
- The trader lives in Melbourne. The trades are futures on US sessions, so a New York morning
  is the Melbourne night: one trade has two dates. Tool results show both. When the trader
  talks about a "Monday" trade they usually mean the New York session day — check, and use
  date_tz="new_york" for session days.
- Always give a weekday with a date. Say how long ago when it helps ("8 days ago").

MEMORY
- Notes below are what you chose to remember. Use remember for durable things: patterns,
  goals, commitments, life context affecting their trading. Not for trivia or one-off facts.
- Use forget when a note is wrong or no longer true.
- The summary covers older conversations; use recall_conversations for detail beyond it.

LIMITS
- Never give buy/sell calls, price predictions or position sizes. Coach the trader, not the trade.
- Never tell the trader to remove, control, master or suppress emotion, or that they need more
  discipline. In this framework feelings are data: help them read what the feeling is saying.
- Cite principle ids in square brackets, e.g. [post-loss-processing], when you apply one."""


def build_system_prompt(corpus: dict, memory: dict) -> str:
    """
    Stable prefix first (persona and principles never change between turns),
    then notes and summary, which change rarely. The date is deliberately not
    here: it would change every request and defeat prefix caching.
    """
    notes = memory.get("notes") or []
    note_lines = [f"[{n['id']}] ({fmt_date(local_date(n['created']))}) {n['text']}" for n in notes] \
        or ["(none yet)"]
    return (
        f"{COACH_PERSONA}\n\n"
        f"=== SHULL PRINCIPLES ===\n{cs._render_principles(corpus)}\n=== END PRINCIPLES ===\n\n"
        f"=== YOUR NOTES ON THIS TRADER ===\n" + "\n".join(note_lines) + "\n\n"
        f"=== EARLIER CONVERSATIONS (summary) ===\n{memory.get('summary') or '(none yet)'}"
    )


# ──────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────

_DATE_PROPS = {
    "date_from": {"type": "string", "description": "Earliest date, YYYY-MM-DD, inclusive."},
    "date_to": {"type": "string", "description": "Latest date, YYYY-MM-DD, inclusive."},
    "date_tz": {"type": "string", "enum": ["melbourne", "new_york"],
                "description": "Which calendar the dates and weekday refer to. new_york = the "
                               "trading session day. Default melbourne."},
}
_FILTER_PROPS = {
    "asset": {"type": "string", "description": "Asset as in the channel name, e.g. gc, es, nq."},
    "kind": {"type": "string", "enum": ["taken", "missed", "all"],
             "description": "taken = trades placed, missed = setups not taken. Default all."},
    **_DATE_PROPS,
    "weekday": {"type": "string", "description": "Only trades on this weekday (Mon..Sun), in date_tz."},
    "outcome": {"type": "string", "enum": ["win", "loss", "breakeven", "none"],
                "description": "none = no outcome recorded."},
    "keywords": {"type": "string", "description": "Words that must all appear in the notes."},
    "fear_min": {"type": "integer", "description": "Min fear spectrum, -5 (fear of losing) to +5 (FOMO)."},
    "fear_max": {"type": "integer", "description": "Max fear spectrum."},
    "emotion": {"type": "string", "description": "An emotion key logged with /state."},
}


def _tool(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
    }}


TOOLS = [
    _tool("find_trades", "Search the trader's journal channels. Returns one line per trade with "
          "both Melbourne and New York dates, outcome and logged emotional state.",
          {**_FILTER_PROPS,
           "limit": {"type": "integer", "description": "Max results, default 15, max 40."},
           "order": {"type": "string", "enum": ["newest", "oldest"]}}),
    _tool("read_trade", "Everything recorded for one trade channel: every note with its time, "
          "the reflection form, /state reading, outcome and missed-trade review.",
          {"channel": {"type": "string", "description": "Channel name, e.g. trade_412_gc."}},
          ["channel"]),
    _tool("trade_stats", "Counts, win rates and average R over a filtered set of trades, by asset "
          "and weekday (New York and Melbourne), plus logged psychology against outcomes.",
          dict(_FILTER_PROPS)),
    _tool("get_insights", "The habits and mistakes extracted from all journal entries (taken), or "
          "the reasons, setups and action items from missed trades. Undated aggregates.",
          {"kind": {"type": "string", "enum": ["taken", "missed"]},
           "keyword": {"type": "string", "description": "Only insights containing this text."},
           "limit": {"type": "integer", "description": "Max items per category, default 25."}}),
    _tool("search_knowledge", "Search the trader's strategy knowledge base (their own articles on "
          "their model and rules).",
          {"query": {"type": "string"}}, ["query"]),
    _tool("recall_conversations", "Search earlier conversations with the trader, by words and/or dates.",
          {"query": {"type": "string", "description": "Words to look for; omit to list by date."},
           **{k: v for k, v in _DATE_PROPS.items() if k != "date_tz"},
           "limit": {"type": "integer", "description": "Max results, default 6."}}),
    _tool("remember", "Save a durable note about the trader for future conversations.",
          {"note": {"type": "string"}}, ["note"]),
    _tool("forget", "Delete one of your notes by its id.",
          {"note_id": {"type": "integer"}}, ["note_id"]),
    _tool("dates_info", "Weekday and how long ago for one or more dates.",
          {"dates": {"type": "array", "items": {"type": "string"}, "description": "YYYY-MM-DD dates."}},
          ["dates"]),
]

FILTER_KEYS = set(_FILTER_PROPS)


class CoachTools:
    """Runs the coach's tool calls. Every result is a string for the model."""

    def __init__(self, memory: ChatMemory, index: TradeIndex, now, bot_names=(),
                 insights_path=None, missed_insights_path=None, knowledge_search=None):
        self.memory = memory
        self.index = index
        self.now = to_utc(now)
        self.bot_names = bot_names
        self.insights_path = Path(insights_path or INSIGHTS_PATH)
        self.missed_insights_path = Path(missed_insights_path or MISSED_INSIGHTS_PATH)
        self.knowledge_search = knowledge_search

    async def run(self, name: str, raw_args) -> str:
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: couldn't read the arguments for {name} ({e}). Send valid JSON."

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"Error: there is no tool called {name}."
        try:
            return cap(await handler(**args))
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            print(f"[coach_chat] tool {name} failed: {e}")
            return f"Error: {name} failed ({e})."

    def _records(self):
        return self.index.get(self.bot_names)

    def _filtered(self, args):
        unknown = set(args) - FILTER_KEYS
        if unknown:
            raise TypeError(f"unexpected {', '.join(sorted(unknown))}")
        return filter_trades(self._records(), **args)

    async def _tool_find_trades(self, limit=15, order="newest", **filters):
        matches = self._filtered(filters)
        if order == "oldest":
            matches = list(reversed(matches))
        limit = max(1, min(int(limit or 15), 40))
        if not matches:
            return "No trades match those filters."
        shown = matches[:limit]
        header = f"{len(matches)} trade(s) match; showing {len(shown)}, {order or 'newest'} first."
        return header + "\n" + "\n".join(summarise_trade(r, self.now) for r in shown)

    async def _tool_read_trade(self, channel):
        records = self._records()
        name = str(channel).strip().lstrip("#")
        for r in records:
            if r["channel"] == name:
                return render_trade(r, self.now)
        close = difflib.get_close_matches(name, [r["channel"] for r in records], n=5)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        return f"No trade channel called {name}.{hint}"

    async def _tool_trade_stats(self, **filters):
        return trade_stats(self._filtered(filters))

    async def _tool_get_insights(self, kind="taken", keyword=None, limit=25):
        path = self.missed_insights_path if kind == "missed" else self.insights_path
        data = _read_json_sync(path, {})
        if not data:
            return "No insights have been extracted yet."
        limit = max(1, min(int(limit or 25), 60))
        needle = (keyword or "").lower().strip()
        lines = []
        for category, items in data.items():
            if not isinstance(items, list):
                continue
            matching = [i for i in items if isinstance(i, str) and needle in i.lower()]
            label = category.replace("_", " ")
            lines.append(f"\n{label}: {len(items)} total"
                         + (f", {len(matching)} mention '{keyword}'" if needle else "")
                         + f" (showing up to {limit}, latest first)")
            lines.extend(f"- {i}" for i in list(reversed(matching))[:limit])
        return "\n".join(lines).strip()

    async def _tool_search_knowledge(self, query):
        if self.knowledge_search is None:
            return "The knowledge base isn't available."
        try:
            context, sources = await self.knowledge_search(query)
        except Exception as e:
            return f"The knowledge base couldn't be searched right now ({e}). It needs Ollama running."
        if not context:
            return "Nothing in the knowledge base matches that."
        return f"Sources: {', '.join(sources)}\n\n{context}"

    async def _tool_recall_conversations(self, query=None, date_from=None, date_to=None, limit=6):
        start = parse_day(date_from) if date_from else None
        end = parse_day(date_to) if date_to else None
        words = [w for w in (query or "").lower().split() if w]
        hits = []
        for e in reversed(await self.memory.exchanges()):
            when = to_utc(e.get("time"))
            if (start or end) and not when:
                continue
            if start and local_date(when) < start:
                continue
            if end and local_date(when) > end:
                continue
            text = f"{e.get('user', '')} {e.get('reply', '')}".lower()
            if words and not any(w in text for w in words):
                continue
            hits.append(e)
            if len(hits) >= max(1, min(int(limit or 6), 15)):
                break
        if not hits:
            return "No earlier conversation matches that."
        blocks = []
        for e in hits:
            when = to_utc(e.get("time"))
            stamp = f"{fmt_local(when)} ({relative_age(when, self.now)})" if when else "undated"
            blocks.append(f"[{stamp}]\nTrader: {e.get('user', '')[:500]}\nCoach: {e.get('reply', '')[:500]}")
        return "\n\n".join(blocks)

    async def _tool_remember(self, note):
        if not str(note).strip():
            raise ValueError("the note is empty")
        saved = await self.memory.add_note(str(note), self.now)
        return f"Saved as note {saved['id']}."

    async def _tool_forget(self, note_id):
        removed = await self.memory.remove_note(int(note_id))
        return f"Deleted note {note_id}." if removed else f"There is no note {note_id}."

    async def _tool_dates_info(self, dates):
        if isinstance(dates, str):
            dates = [dates]
        return dates_info(dates, self.now)


# ──────────────────────────────────────────────
# Cost
# ──────────────────────────────────────────────

def usage_cost(usages, peak: bool, prices=None) -> float:
    """USD for a set of DeepSeek usage blocks. Peak pricing is double."""
    prices = prices or DEEPSEEK_PRICE_PER_M
    total = 0.0
    for u in usages:
        hit = u.get("prompt_cache_hit_tokens")
        miss = u.get("prompt_cache_miss_tokens")
        if hit is None and miss is None:
            hit, miss = 0, u.get("prompt_tokens", 0)
        total += ((hit or 0) * prices["cache_hit"] + (miss or 0) * prices["cache_miss"]
                  + u.get("completion_tokens", 0) * prices["output"]) / 1_000_000
    return total * (2 if peak else 1)
