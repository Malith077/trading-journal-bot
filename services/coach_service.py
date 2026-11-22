"""
Shull coaching service.

Design constraint, and the reason this module exists at all: the model is NOT
trusted to remember Denise Shull's work. Her material is niche, and the nearest
attractor in any LLM's training data is conventional trading psychology
("eliminate emotion, be disciplined, trade mechanically") — which contradicts
Shull on her central claim. A model asked to "coach like Shull" from memory will
drift there while sounding confident.

So three things are enforced here:

  1. GROUNDING — the entire principles corpus is injected into every prompt.
     There is no retrieval step to miss the relevant principle, and the model is
     told to work only from the supplied text.
  2. CITATION — every observation must name the principle id it applies. An
     observation citing an id that isn't in the corpus is dropped, which makes
     confabulation visible instead of plausible.
  3. GUARDRAIL — output is scanned for the corpus's forbidden phrases before the
     user ever sees it. This is a deterministic string check, not a judgment
     call, and it runs at runtime as well as in evals.

None of this makes the coach correct. It makes it *checkable*, and it bounds the
failure modes to ones a test can see.
"""
import json
import re
from pathlib import Path

from services.llm_service import llm_service, LLMError
from config import DEEPSEEK_COACH_EFFORT

PRINCIPLES_PATH = Path(__file__).resolve().parent.parent / "coaching" / "shull_principles.json"

EMPTY_FEEDBACK = {
    "reading": "",
    "observations": [],
    "probe": "",
    "not_enough_signal": False,
}


class CoachError(RuntimeError):
    """Raised when coaching cannot be produced or cannot be trusted."""


def load_corpus(path=None) -> dict:
    """Load the principles corpus. Raises if it is missing or malformed."""
    path = Path(path or PRINCIPLES_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            corpus = json.load(f)
    except FileNotFoundError:
        raise CoachError(f"Principles corpus not found at {path}")
    except json.JSONDecodeError as e:
        raise CoachError(f"Principles corpus is not valid JSON: {e}")

    if not corpus.get("principles"):
        raise CoachError("Principles corpus contains no principles.")
    return corpus


def principle_ids(corpus: dict) -> set:
    return {p["id"] for p in corpus["principles"]}


def forbidden_phrases(corpus: dict) -> list:
    return corpus.get("forbidden_language", {}).get("phrases", [])


def find_forbidden(text: str, corpus: dict) -> list[str]:
    """
    Return every forbidden phrase present in `text`.

    Deterministic substring matching on a normalised copy — no model judgment.
    Apostrophe variants are folded so "don't" and "don’t" both match.
    """
    if not text:
        return []
    normalised = re.sub(r"\s+", " ", text.lower().replace("’", "'"))
    return [p for p in forbidden_phrases(corpus) if p in normalised]


def _render_principles(corpus: dict) -> str:
    """Render the corpus for injection — the model's only permitted source."""
    blocks = []
    for p in corpus["principles"]:
        blocks.append(
            f"[{p['id']}] {p['title']}\n"
            f"  PRINCIPLE: {p['principle']}\n"
            f"  REJECTS: {p['rejects']}\n"
            f"  TYPICAL CUES: {', '.join(p['cues'])}"
        )
    return "\n\n".join(blocks)


def build_prompt(entry: str, corpus: dict, context: str = "") -> str:
    """Build the coaching prompt. The corpus is supplied in full, every time."""
    return f"""You are a trading performance coach applying ONE specific framework:
Denise Shull's, as set out in the numbered principles below.

THESE PRINCIPLES ARE YOUR ONLY SOURCE OF TRUTH.
Do not draw on any other trading-psychology material you may know. In particular,
do NOT drift toward the conventional "eliminate emotion / be more disciplined /
trade mechanically" school — Shull explicitly rejects it, and advice of that kind
is a failure, not a fallback. If these principles do not speak to the entry, say so
rather than reaching for generic advice.

=== PRINCIPLES ===
{_render_principles(corpus)}
=== END PRINCIPLES ===

The trader wrote this journal entry:
\"\"\"
{entry}
\"\"\"
{context}
TASK — respond in json with exactly this shape:
{{"reading": "2-3 sentences reflecting back the emotional context you can actually see in their words. Describe, don't prescribe.",
  "observations": [
    {{"principle_id": "must be one of the ids in square brackets above",
      "quote": "the trader's own words that triggered this, verbatim and short",
      "point": "one or two sentences applying that principle to what they wrote"}}
  ],
  "probe": "a single question that opens the next layer — often the F-eC probe, but only where it fits",
  "not_enough_signal": false}}

RULES:
- Every observation MUST carry a principle_id copied exactly from the list above.
  Never invent an id. If you cannot ground an observation in a listed principle,
  leave it out.
- The `quote` must appear verbatim in the trader's entry. Do not paraphrase it.
- At most 3 observations. Fewer is better than padded.
- If the entry is too thin to read anything from (a bare price note, a chart with
  no commentary), set "not_enough_signal": true, return an empty observations list,
  and do not manufacture insight.
- Never tell the trader to remove, control, master or suppress emotion, and never
  tell them they need more discipline. Name what the feeling is telling them instead.
- Address the trader as "you". Be direct and specific. No preamble, no praise padding."""


def validate_feedback(feedback: dict, entry: str, corpus: dict) -> tuple[dict, list[str]]:
    """
    Enforce the grounding rules on model output.

    Returns (cleaned_feedback, warnings). Observations citing an unknown principle
    are dropped — a fabricated citation is exactly the failure this is here to
    catch, and silently keeping the text would hide it.

    Quote fidelity is checked but only warned on: models legitimately normalise
    whitespace and case, and dropping an otherwise-good observation over a
    smart quote would cost more than it saves.
    """
    warnings = []
    valid_ids = principle_ids(corpus)
    normalised_entry = re.sub(r"\s+", " ", (entry or "").lower().replace("’", "'"))

    kept = []
    for obs in feedback.get("observations") or []:
        if not isinstance(obs, dict):
            warnings.append("Dropped a malformed observation.")
            continue

        pid = obs.get("principle_id")
        if pid not in valid_ids:
            warnings.append(f"Dropped observation citing unknown principle {pid!r}.")
            continue

        quote = (obs.get("quote") or "").strip()
        if quote:
            normalised_quote = re.sub(r"\s+", " ", quote.lower().replace("’", "'"))
            if normalised_quote not in normalised_entry:
                warnings.append(f"Quote not found verbatim in entry: {quote[:60]!r}")

        kept.append(obs)

    cleaned = {**EMPTY_FEEDBACK, **feedback, "observations": kept}

    # Runtime guardrail: scan everything the trader would actually read.
    surfaces = [cleaned.get("reading", ""), cleaned.get("probe", "")]
    surfaces += [o.get("point", "") for o in kept]
    hits = find_forbidden(" ".join(s for s in surfaces if s), corpus)
    if hits:
        warnings.append(f"Anti-Shull language detected: {', '.join(sorted(set(hits)))}")

    cleaned["warnings"] = warnings
    cleaned["forbidden_hits"] = sorted(set(hits))
    return cleaned, warnings


# Entries shorter than this are almost always a price note or a one-word tag —
# not enough for a reading, and coaching them produces noise.
MIN_ENTRY_CHARS = 60


def is_coachable(entry: str) -> bool:
    """Whether an entry carries enough text to be worth a coaching call."""
    return bool(entry) and len(entry.strip()) >= MIN_ENTRY_CHARS


async def coach(entry: str, *, context: str = "", corpus: dict | None = None, **kwargs) -> dict:
    """
    Produce grounded Shull feedback on a journal entry.

    Returns the validated feedback dict, including `warnings` and
    `forbidden_hits` so the caller can surface trust problems rather than
    swallow them. Raises CoachError if the model call fails outright.
    """
    corpus = corpus or load_corpus()
    prompt = build_prompt(entry, corpus, context)

    kwargs.setdefault("reasoning_effort", DEEPSEEK_COACH_EFFORT)
    try:
        raw = await llm_service.complete(prompt, json_mode=True, **kwargs)
    except LLMError as e:
        raise CoachError(str(e)) from e

    from services.json_utils import extract_json
    parsed = extract_json(raw, EMPTY_FEEDBACK)
    cleaned, _ = validate_feedback(parsed, entry, corpus)
    return cleaned
