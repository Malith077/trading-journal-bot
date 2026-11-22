"""
Trading psychology layer based on Denise Shull's work (Market Mind Games /
The ReThink Group).

The organising premise is Shull's: emotions are *data*, not noise. The goal is
never to suppress a feeling or to gate the trader out of the market for being
"too emotional" — it is to name the emotional context precisely enough that it
can be logged, correlated with results, and recognised next time. As Shull puts
it, it is not about controlling emotions, it is about controlling behaviour.

Three nested contexts, outermost to innermost:

  fC   Feelings context   — physical/bodily state (tired, hungry, wired).
  eC   Emotional context  — the current emotional state colouring judgment.
  F-eC Fractal emotional  — the recurring story from far earlier in life that
       context              replays under success/failure pressure.

Two further Shull constructs are modelled here:

  * The fear spectrum — "where do I fall between fear of losing and fear of
    missing out?" A single signed axis, because the two fears pull opposite ways
    and most errors live at one extreme or the other.
  * Intuition vs impulse — intuition is quiet, arrives from nowhere, and rests
    on tacit pattern recognition; impulse is urgent and compelling. They feel
    similar in the moment and are separated by interrogation, not by vibe.

NOTE ON NAMING: Shull's "fractal emotional context" has nothing to do with the
TTrades Fractal Model this bot journals. The collision is coincidental; F-eC
here always means the psychological construct.
"""
import json
import datetime
from pathlib import Path

# ──────────────────────────────────────────────
# fC — Feelings context (physical state)
# ──────────────────────────────────────────────
# Shull treats bodily state as first-class decision data: tiredness and hunger
# move risk perception before any chart is read.

FEELINGS_CONTEXT = [
    ("sharp", "Rested and sharp", "🟢"),
    ("steady", "Functional, nothing notable", "⚪"),
    ("tired", "Tired / low energy", "🟡"),
    ("wired", "Wired / over-caffeinated", "🟠"),
    ("unwell", "Physically unwell", "🔴"),
    ("strained", "Carrying stress from outside the market", "🔴"),
]

# ──────────────────────────────────────────────
# eC — Emotional context
# ──────────────────────────────────────────────
# Granular labels on purpose. "Good"/"bad" is not data; "defensive" is.
# `valence` marks which states historically precede degraded decisions, but it
# is descriptive only — no state blocks a trade.

EMOTIONAL_CONTEXT = [
    ("calm", "Calm", "🧘", "steady"),
    ("confident", "Confident", "💪", "steady"),
    ("curious", "Curious", "🔎", "steady"),
    ("detached", "Detached / flat", "😶", "steady"),
    ("eager", "Eager", "⚡", "activated"),
    ("impatient", "Impatient", "⏱️", "activated"),
    ("euphoric", "Euphoric", "🚀", "activated"),
    ("anxious", "Anxious", "😰", "activated"),
    ("frustrated", "Frustrated", "😤", "activated"),
    ("angry", "Angry", "🔥", "activated"),
    ("vengeful", "Wanting it back", "🎯", "activated"),
    ("deflated", "Deflated", "🫠", "activated"),
    ("defensive", "Defensive", "🛡️", "activated"),
    ("bored", "Bored", "🥱", "activated"),
]

EMOTION_LABELS = {key: label for key, label, _, _ in EMOTIONAL_CONTEXT}
EMOTION_EMOJI = {key: emoji for key, _, emoji, _ in EMOTIONAL_CONTEXT}
EMOTION_VALENCE = {key: valence for key, _, _, valence in EMOTIONAL_CONTEXT}
FEELINGS_LABELS = {key: label for key, label, _ in FEELINGS_CONTEXT}
FEELINGS_EMOJI = {key: emoji for key, _, emoji in FEELINGS_CONTEXT}

# ──────────────────────────────────────────────
# The fear spectrum
# ──────────────────────────────────────────────
# −5 = pure fear of losing · 0 = balanced · +5 = pure fear of missing out.

FEAR_MIN, FEAR_MAX = -5, 5

FEAR_ZONES = [
    ("loss_fear", "Fear of losing", "🛑", -5, -2),
    ("balanced", "Balanced", "⚖️", -1, 1),
    ("fomo", "Fear of missing out", "🏃", 2, 5),
]


def clamp_fear(value: int) -> int:
    """Clamp a fear-spectrum reading into range."""
    return max(FEAR_MIN, min(FEAR_MAX, int(value)))


def fear_zone(value: int) -> str:
    """Map a fear-spectrum reading to its zone key."""
    value = clamp_fear(value)
    for key, _, _, low, high in FEAR_ZONES:
        if low <= value <= high:
            return key
    return "balanced"


def fear_label(value: int) -> str:
    """Human-readable label with the signed reading, e.g. '🏃 Fear of missing out (+3)'."""
    value = clamp_fear(value)
    zone = fear_zone(value)
    for key, label, emoji, _, _ in FEAR_ZONES:
        if key == zone:
            return f"{emoji} {label} ({value:+d})"
    return str(value)


def fear_bar(value: int) -> str:
    """A small text gauge of where the reading sits on the spectrum."""
    value = clamp_fear(value)
    slots = ["·"] * (FEAR_MAX - FEAR_MIN + 1)
    slots[value - FEAR_MIN] = "●"
    return f"loss ⟵ {''.join(slots)} ⟶ fomo"


# ──────────────────────────────────────────────
# Intuition vs impulse
# ──────────────────────────────────────────────
# Shull's separation: intuition is quiet and pattern-based; impulse is urgent
# and usually about restoring a feeling. Each probe is phrased so that True
# points toward impulse, keeping the scoring direction obvious.

IMPULSE_PROBES = [
    ("urgent", "It arrived with urgency — I felt I had to act *now*"),
    ("unfamiliar", "I can't point to a setup I've traded before that looks like this"),
    ("recover", "I'm trying to get back something I just lost, or catch up"),
    ("body_tense", "My body is tense — jaw, chest, shoulders, gut"),
    ("social", "Someone else's position or opinion is pulling me into it"),
]

IMPULSE_PROBE_KEYS = [key for key, _ in IMPULSE_PROBES]

SIGNAL_INTUITION = "intuition"
SIGNAL_MIXED = "mixed"
SIGNAL_IMPULSE = "impulse"

SIGNAL_LABELS = {
    SIGNAL_INTUITION: "🧭 Reads as intuition",
    SIGNAL_MIXED: "🌗 Mixed signal",
    SIGNAL_IMPULSE: "⚠️ Reads as impulse",
}

SIGNAL_GUIDANCE = {
    SIGNAL_INTUITION: (
        "Quiet, familiar, and not about recovering anything. Shull would call this "
        "tacit pattern recognition — the accumulated reps below conscious awareness. "
        "Trust it *and* still log it, so you can check later whether it earned the trust."
    ),
    SIGNAL_MIXED: (
        "Some genuine pattern recognition, some agitation. Name which probes fired "
        "before sizing — a mixed read is not a veto, it is a reason to know what you "
        "are carrying."
    ),
    SIGNAL_IMPULSE: (
        "Urgency, unfamiliarity, or the pull to recover something. Shull's point is not "
        "that you must sit out — it is that this feeling is information about *you*, not "
        "about the market. Feel it fully before deciding, rather than trading it away."
    ),
}


def classify_signal(probes: dict) -> tuple[str, int]:
    """
    Score the impulse probes and classify the idea.

    `probes` maps probe keys to booleans; unknown keys are ignored and missing
    ones count as False. Returns (classification, number_of_probes_fired).
    """
    fired = sum(1 for key in IMPULSE_PROBE_KEYS if probes.get(key))

    if fired == 0:
        return SIGNAL_INTUITION, fired
    if fired <= 2:
        return SIGNAL_MIXED, fired
    return SIGNAL_IMPULSE, fired


# ──────────────────────────────────────────────
# Outcomes
# ──────────────────────────────────────────────
# Emotions only become data once they can be checked against results, so a
# minimal outcome record sits alongside each trade.

OUTCOMES = [
    ("win", "Win", "✅"),
    ("loss", "Loss", "❌"),
    ("breakeven", "Breakeven / scratch", "➖"),
]

OUTCOME_LABELS = {key: label for key, label, _ in OUTCOMES}
OUTCOME_EMOJI = {key: emoji for key, _, emoji in OUTCOMES}


# ──────────────────────────────────────────────
# State records
# ──────────────────────────────────────────────

def build_state(
    *,
    feelings: str | None = None,
    emotions: list[str] | None = None,
    fear: int = 0,
    conviction: int | None = None,
    probes: dict | None = None,
    reminds_me_of: str | None = None,
    note: str | None = None,
    channel: str | None = None,
    author: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Assemble a normalised psychological state record."""
    probes = probes or {}
    signal, fired = classify_signal(probes)

    return {
        "timestamp": timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "author": author,
        "channel": channel,
        # fC
        "feelings_context": feelings,
        # eC
        "emotional_context": sorted(set(emotions or [])),
        # F-eC — the "what does this remind me of?" probe
        "fractal_context": reminds_me_of,
        "fear_spectrum": clamp_fear(fear),
        "fear_zone": fear_zone(fear),
        "conviction": conviction,
        "impulse_probes": {key: bool(probes.get(key)) for key in IMPULSE_PROBE_KEYS},
        "signal": signal,
        "probes_fired": fired,
        "note": note,
    }


async def append_state(log_path, state: dict) -> None:
    """Append one state record to the JSONL log."""
    import aiofiles

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(log_path, "a", encoding="utf-8") as f:
        await f.write(json.dumps(state, ensure_ascii=False) + "\n")


async def read_states(log_path) -> list[dict]:
    """Read every state record from the JSONL log, skipping corrupt lines."""
    import aiofiles

    log_path = Path(log_path)
    if not log_path.exists():
        return []

    states = []
    async with aiofiles.open(log_path, "r", encoding="utf-8") as f:
        content = await f.read()

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            states.append(record)
    return states


# ──────────────────────────────────────────────
# Correlation — turning logged emotion into evidence
# ──────────────────────────────────────────────

def _rate(wins: int, decided: int) -> float | None:
    """Win rate over decided (non-breakeven) trades, or None if there are none."""
    return (wins / decided) if decided else None


def _tally() -> dict:
    return {"n": 0, "wins": 0, "losses": 0, "breakeven": 0, "r_sum": 0.0, "r_n": 0}


def _record(bucket: dict, outcome: str, r_multiple) -> None:
    bucket["n"] += 1
    if outcome == "win":
        bucket["wins"] += 1
    elif outcome == "loss":
        bucket["losses"] += 1
    elif outcome == "breakeven":
        bucket["breakeven"] += 1
    if isinstance(r_multiple, (int, float)):
        bucket["r_sum"] += float(r_multiple)
        bucket["r_n"] += 1


def _finalise(bucket: dict) -> dict:
    decided = bucket["wins"] + bucket["losses"]
    return {
        **bucket,
        "decided": decided,
        "win_rate": _rate(bucket["wins"], decided),
        "avg_r": (bucket["r_sum"] / bucket["r_n"]) if bucket["r_n"] else None,
    }


def correlate(entries: list[dict]) -> dict:
    """
    Correlate logged psychological state against trade outcomes.

    Each entry is a merged record carrying at least `outcome`, plus whichever of
    `fear_zone`, `emotional_context`, `signal`, `feelings_context`, `conviction`
    and `r_multiple` were logged. Entries without an outcome are ignored —
    they have nothing to correlate against yet.

    Returns per-dimension tallies plus a conviction calibration table.
    """
    overall = _tally()
    by_fear_zone: dict[str, dict] = {}
    by_emotion: dict[str, dict] = {}
    by_signal: dict[str, dict] = {}
    by_feelings: dict[str, dict] = {}
    conviction_pairs: list[tuple[int, str]] = []

    for entry in entries:
        outcome = entry.get("outcome")
        if outcome not in OUTCOME_LABELS:
            continue

        r_multiple = entry.get("r_multiple")
        _record(overall, outcome, r_multiple)

        zone = entry.get("fear_zone")
        if zone is None and entry.get("fear_spectrum") is not None:
            zone = fear_zone(entry["fear_spectrum"])
        if zone:
            _record(by_fear_zone.setdefault(zone, _tally()), outcome, r_multiple)

        for emotion in entry.get("emotional_context") or []:
            _record(by_emotion.setdefault(emotion, _tally()), outcome, r_multiple)

        if entry.get("signal"):
            _record(by_signal.setdefault(entry["signal"], _tally()), outcome, r_multiple)

        if entry.get("feelings_context"):
            _record(by_feelings.setdefault(entry["feelings_context"], _tally()), outcome, r_multiple)

        conviction = entry.get("conviction")
        if isinstance(conviction, (int, float)) and outcome in ("win", "loss"):
            conviction_pairs.append((int(conviction), outcome))

    return {
        "overall": _finalise(overall),
        "by_fear_zone": {k: _finalise(v) for k, v in by_fear_zone.items()},
        "by_emotion": {k: _finalise(v) for k, v in by_emotion.items()},
        "by_signal": {k: _finalise(v) for k, v in by_signal.items()},
        "by_feelings": {k: _finalise(v) for k, v in by_feelings.items()},
        "conviction": calibrate_conviction(conviction_pairs),
    }


# Conviction buckets. Shull's question is whether felt certainty tracks reality;
# if it does not, conviction is noise the trader is sizing on.
CONVICTION_BUCKETS = [("low", 1, 4), ("medium", 5, 7), ("high", 8, 10)]


def calibrate_conviction(pairs: list[tuple[int, str]]) -> dict:
    """
    Check whether felt conviction predicts being right.

    `pairs` is a list of (conviction 1-10, outcome) for decided trades only.
    Returns per-bucket win rates plus a `predictive` verdict: True when the
    high bucket beats the low bucket, False when it does not, None when there
    is not enough evidence in both buckets to say.
    """
    buckets = {name: _tally() for name, _, _ in CONVICTION_BUCKETS}

    for conviction, outcome in pairs:
        for name, low, high in CONVICTION_BUCKETS:
            if low <= conviction <= high:
                _record(buckets[name], outcome, None)
                break

    finalised = {name: _finalise(bucket) for name, bucket in buckets.items()}

    low_rate = finalised["low"]["win_rate"]
    high_rate = finalised["high"]["win_rate"]
    # Require a couple of decided trades at each end before claiming a pattern.
    if (low_rate is None or high_rate is None
            or finalised["low"]["decided"] < 2 or finalised["high"]["decided"] < 2):
        predictive = None
    else:
        predictive = high_rate > low_rate

    return {
        "buckets": finalised,
        "predictive": predictive,
        "n": len(pairs),
    }


# ──────────────────────────────────────────────
# Prompt context
# ──────────────────────────────────────────────

def format_state_context(state: dict, outcome: dict | None = None) -> str:
    """
    Render a logged psychological state as prompt context for the analyst model.

    Returns "" when there is no state, so callers can concatenate unconditionally.
    """
    if not state:
        return ""

    lines = ["\nTrader's Psychological State (Shull framework):"]

    fc = state.get("feelings_context")
    if fc:
        lines.append(f"- fC (physical): {FEELINGS_LABELS.get(fc, fc)}")

    emotions = state.get("emotional_context") or []
    if emotions:
        named = ", ".join(EMOTION_LABELS.get(e, e) for e in emotions)
        lines.append(f"- eC (emotional): {named}")

    fear = state.get("fear_spectrum")
    if fear is not None:
        lines.append(
            f"- Fear spectrum: {fear:+d} on a -5 (fear of losing) to +5 (fear of missing out) axis"
        )

    signal = state.get("signal")
    if signal:
        fired = state.get("probes_fired", 0)
        lines.append(
            f"- Intuition/impulse read: {signal} ({fired}/{len(IMPULSE_PROBES)} impulse probes fired)"
        )

    conviction = state.get("conviction")
    if conviction is not None:
        lines.append(f"- Conviction at the time: {conviction}/10")

    fractal = state.get("fractal_context")
    if fractal:
        lines.append(f"- F-eC, what it reminded them of: {fractal}")

    note = state.get("note")
    if note:
        lines.append(f"- Their note: {note}")

    if outcome and outcome.get("outcome"):
        result = OUTCOME_LABELS.get(outcome["outcome"], outcome["outcome"])
        r_multiple = outcome.get("r_multiple")
        suffix = f" at {r_multiple:+g}R" if isinstance(r_multiple, (int, float)) else ""
        lines.append(f"- Actual result: {result}{suffix}")

    return "\n".join(lines) + "\n"


# Shared instruction block so every analyst prompt speaks Shull consistently.
SHULL_ANALYST_GUIDANCE = """
PSYCHOLOGICAL FRAMING (Denise Shull, Market Mind Games):
- Emotions are DATA, not noise. Never write "be less emotional", "stay disciplined",
  or "remove emotion" — Shull explicitly rejects that framing. The aim is controlling
  behaviour, not controlling feelings.
- When the trader's own words reveal an emotional driver, name it PRECISELY
  ("wanting the last loss back", "fear of being wrong after two red days")
  rather than vaguely ("was emotional", "lacked discipline").
- Locate the behaviour on the fear spectrum where relevant: fear of losing at one
  end, fear of missing out at the other.
- Intuition is quiet and rests on familiar patterns; impulse is urgent and usually
  about recovering something. Do not conflate them.
"""
