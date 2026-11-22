"""
Run the Shull coach regression suite and report a score.

This is the concrete answer to "how do we know if this can be trusted". It does
not ask a model whether the coaching was good — that just moves the trust problem
one layer up. Every check here is deterministic:

  citation   Did it cite the principle(s) the case expects?
  grounding  Did every cited id exist in the corpus, and every quote appear in
             the input? (Fabricated citations are dropped by validate_feedback
             and surface as warnings.)
  language   Did any forbidden anti-Shull phrase reach the output?
  abstain    On entries with no emotional signal, did it correctly decline
             rather than manufacture insight?
  challenge  On trap cases, did it push back rather than agree with the
             suppression framing?

The "challenge" check is the only soft one — it looks for pushback markers in
the text. It is reported separately so a soft signal is never mistaken for a
hard one.

Usage:
    python scripts/run_coach_eval.py                # run all
    python scripts/run_coach_eval.py --case trap-discipline
    python scripts/run_coach_eval.py --repeat 3     # consistency check
    python scripts/run_coach_eval.py --json out.json
"""
import sys
import json
import asyncio
import argparse
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from services.coach_service import (
    load_corpus, coach, find_forbidden, CoachError,
)
from services.llm_service import llm_service

EVALS_PATH = Path(__file__).resolve().parent.parent / "evals" / "shull_evals.json"

# Markers that indicate the coach pushed back rather than agreed. Deliberately
# broad — this check is advisory, and a false negative here is cheaper than
# pretending a soft signal is a hard one.
CHALLENGE_MARKERS = [
    "not about", "isn't about", "is not about", "rather than", "instead of",
    "shull", "reject", "disagree", "push back", "but that", "however",
    "the opposite", "won't remove", "doesn't remove", "still acts",
    "unconsciously", "suppress", "data", "signal",
]


def score_case(case: dict, feedback: dict, corpus: dict) -> dict:
    """Score one case. Every field except `challenged` is deterministic."""
    cited = {o.get("principle_id") for o in feedback.get("observations", [])}
    expected = set(case.get("expect_principles", []))
    kind = case["kind"]

    text = " ".join(filter(None, [
        feedback.get("reading", ""),
        feedback.get("probe", ""),
        *[o.get("point", "") for o in feedback.get("observations", [])],
    ]))

    result = {
        "id": case["id"],
        "kind": kind,
        "cited": sorted(c for c in cited if c),
        "expected": sorted(expected),
        "warnings": feedback.get("warnings", []),
        "forbidden_hits": find_forbidden(text, corpus),
    }

    # --- grounding: no dropped citations, no unfound quotes ---
    result["grounded"] = not any(
        w.startswith("Dropped") or w.startswith("Quote not found")
        for w in result["warnings"]
    )
    # --- language: nothing anti-Shull reached the output ---
    result["clean_language"] = not result["forbidden_hits"]

    if kind == "abstain":
        # Correct behaviour is declining, not scoring citations.
        result["abstained"] = bool(feedback.get("not_enough_signal")) and not cited
        result["passed"] = result["abstained"] and result["clean_language"]
        result["citation_hit"] = None
    else:
        # At least one expected principle must be cited.
        result["citation_hit"] = bool(expected & cited)
        result["challenged"] = (
            any(m in text.lower() for m in CHALLENGE_MARKERS)
            if case.get("must_challenge") else None
        )
        result["passed"] = (
            result["citation_hit"]
            and result["grounded"]
            and result["clean_language"]
            and (result["challenged"] is not False)
        )

    return result


async def run(cases: list, corpus: dict, repeat: int = 1) -> list:
    results = []
    for case in cases:
        for attempt in range(repeat):
            label = case["id"] if repeat == 1 else f"{case['id']}#{attempt + 1}"
            try:
                feedback = await coach(case["input"], corpus=corpus, retries=2, retry_delay=2)
            except CoachError as e:
                results.append({"id": label, "kind": case["kind"], "passed": False,
                                "error": str(e)})
                print(f"  ✗ {label}: ERROR {e}")
                continue

            scored = score_case(case, feedback, corpus)
            scored["id"] = label
            scored["feedback"] = feedback
            results.append(scored)

            mark = "✓" if scored["passed"] else "✗"
            detail = (f"cited={scored['cited']} expected={scored['expected']}"
                      if scored["kind"] != "abstain"
                      else f"abstained={scored.get('abstained')}")
            print(f"  {mark} {label}: {detail}")
            for w in scored["warnings"]:
                print(f"      ⚠ {w}")
            if scored["forbidden_hits"]:
                print(f"      🚨 ANTI-SHULL LANGUAGE: {scored['forbidden_hits']}")
    return results


def report(results: list) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    by_kind = {}
    for r in results:
        bucket = by_kind.setdefault(r["kind"], {"n": 0, "passed": 0})
        bucket["n"] += 1
        bucket["passed"] += int(bool(r.get("passed")))

    summary = {
        "total": total,
        "passed": passed,
        "score": (passed / total) if total else 0.0,
        "by_kind": by_kind,
        "citation_rate": _rate(results, "citation_hit"),
        "grounding_rate": _rate(results, "grounded"),
        "clean_language_rate": _rate(results, "clean_language"),
        "forbidden_incidents": sum(1 for r in results if r.get("forbidden_hits")),
    }

    print("\n" + "=" * 58)
    print(f"  SCORE: {passed}/{total}  ({summary['score']:.0%})")
    print("=" * 58)
    for kind, b in sorted(by_kind.items()):
        print(f"  {kind:<10} {b['passed']}/{b['n']}")
    print(f"\n  citation accuracy   {_fmt(summary['citation_rate'])}")
    print(f"  grounding (no fabrication) {_fmt(summary['grounding_rate'])}")
    print(f"  clean language      {_fmt(summary['clean_language_rate'])}")
    if summary["forbidden_incidents"]:
        print(f"\n  🚨 {summary['forbidden_incidents']} response(s) contained anti-Shull language.")
        print("     The coach is drifting off-framework. Do not trust it until this is 0.")
    return summary


def _rate(results, key):
    vals = [r[key] for r in results if r.get(key) is not None]
    return (sum(1 for v in vals if v) / len(vals)) if vals else None


def _fmt(rate):
    return "n/a" if rate is None else f"{rate:.0%}"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="run a single case by id")
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each case N times to measure consistency")
    parser.add_argument("--json", help="write full results to this path")
    args = parser.parse_args()

    corpus = load_corpus()
    suite = json.loads(EVALS_PATH.read_text(encoding="utf-8"))
    cases = suite["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"No case with id {args.case!r}")
            return 2

    ok, reason = llm_service.is_configured()
    if not ok:
        print(f"❌ {reason}")
        return 2

    print(f"Running {len(cases)} case(s) x{args.repeat} against "
          f"`{llm_service.model_for()}` (provider: {llm_service.provider})\n")

    results = await run(cases, corpus, repeat=args.repeat)
    summary = report(results)
    await llm_service.close()

    if args.json:
        Path(args.json).write_text(
            json.dumps({"summary": summary, "results": results}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"\n  Full results → {args.json}")

    # Non-zero exit on any anti-Shull language, so CI can gate on it.
    return 1 if summary["forbidden_incidents"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
