"""
Tests for the #coach conversation: services/coach_chat_service.py,
cogs/coach_chat.py and LLMService.chat.

Dates get the most attention because the trader asked for them to be exact:
weekdays, both time zones, daylight saving on both sides, and relative ages.
"""
import datetime as dt
import json
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services import coach_chat_service as ccs
from services import coach_service as cs
from services.llm_service import LLMService, LLMError
from cogs.coach_chat import CoachChat, split_message, peak_line, MAX_TOOL_ROUNDS

UTC = dt.timezone.utc


def utc(*args):
    return dt.datetime(*args, tzinfo=UTC)


# ──────────────────────────────────────────────
# Dates
# ──────────────────────────────────────────────

class TestDates:

    def test_trade_shows_both_zones_with_weekdays(self):
        # 09:30 New York on Mon 16 Nov 2026 is 01:30 Tue 17 Nov in Melbourne (AEDT, UTC+11).
        assert ccs.fmt_dual(utc(2026, 11, 16, 14, 30)) == \
            "Tue 17 Nov 2026 01:30 AEDT (Mon 16 Nov 09:30 New York)"

    def test_southern_winter_and_northern_summer(self):
        # July: Melbourne is AEST (UTC+10), New York is EDT (UTC-4).
        assert ccs.fmt_dual(utc(2026, 7, 6, 13, 30)) == \
            "Mon 6 Jul 2026 23:30 AEST (Mon 6 Jul 09:30 New York)"

    def test_daylight_saving_switch_days(self):
        # Melbourne went to AEDT on Sun 4 Oct 2026; New York left EDT on Sun 1 Nov 2026.
        assert "AEST" in ccs.fmt_local(utc(2026, 10, 3, 15, 0))
        assert "AEDT" in ccs.fmt_local(utc(2026, 10, 4, 16, 0))
        assert ccs.fmt_market(utc(2026, 10, 30, 13, 30)) == "Fri 30 Oct 09:30 New York"   # EDT
        assert ccs.fmt_market(utc(2026, 11, 2, 14, 30)) == "Mon 2 Nov 09:30 New York"     # EST

    @pytest.mark.parametrize("day,expected", [
        (dt.date(2026, 11, 17), "Tue 17 Nov 2026"),
        (dt.date(2026, 11, 25), "Wed 25 Nov 2026"),
        (dt.date(2026, 1, 1), "Thu 1 Jan 2026"),
        (dt.date(2028, 2, 29), "Tue 29 Feb 2028"),
    ])
    def test_weekdays_are_correct(self, day, expected):
        assert ccs.fmt_date(day) == expected

    def test_now_block_calendar(self):
        block = ccs.now_block(utc(2026, 11, 25, 3, 32))       # Wed 14:32 AEDT
        assert "[Now: Wednesday 25 November 2026, 14:32 AEDT in Melbourne" in block
        assert "New York: Tuesday 24 November 2026, 22:32 EST" in block
        assert "Today = Wed 25 Nov 2026 · Yesterday = Tue 24 Nov 2026" in block
        assert "This week = Mon 23 Nov – Sun 29 Nov 2026" in block
        assert "Last week = Mon 16 Nov – Sun 22 Nov 2026" in block
        assert "Wed 25 Nov (today), Tue 24 Nov (yesterday), Mon 23 Nov" in block
        assert block.count(",") >= 13 and "Thu 12 Nov]" in block

    def test_now_block_uses_melbourne_date_not_utc(self):
        # 20:00 UTC Sunday is already Monday morning in Melbourne.
        block = ccs.now_block(utc(2026, 11, 22, 20, 0))
        assert "Today = Mon 23 Nov 2026" in block
        assert "This week = Mon 23 Nov" in block

    def test_week_starts_monday_on_sunday(self):
        block = ccs.now_block(utc(2026, 11, 29, 1, 0))        # Sun 29 Nov 12:00 AEDT
        assert "This week = Mon 23 Nov – Sun 29 Nov 2026" in block

    @pytest.mark.parametrize("then,expected", [
        (utc(2026, 11, 25, 0, 0), "today"),
        (utc(2026, 11, 24, 0, 0), "yesterday"),
        (utc(2026, 11, 22, 0, 0), "3 days ago"),
        (utc(2026, 11, 4, 0, 0), "3 weeks ago"),
        (utc(2026, 8, 1, 0, 0), "3 months ago"),
        (utc(2024, 11, 1, 0, 0), "2 years ago"),
        (utc(2026, 12, 1, 0, 0), "in the future"),
    ])
    def test_relative_age(self, then, expected):
        assert ccs.relative_age(then, utc(2026, 11, 25, 3, 0)) == expected

    def test_relative_age_counts_melbourne_days(self):
        # 23:00 UTC on the 23rd is 10:00 on the 24th in Melbourne: "yesterday", not 2 days.
        assert ccs.relative_age(utc(2026, 11, 23, 23, 0), utc(2026, 11, 25, 3, 0)) == "yesterday"

    def test_stamp_puts_calendar_before_words(self):
        stamped = ccs.stamp_user_message("how was my week?", utc(2026, 11, 25, 3, 32))
        assert stamped.startswith("[Now: Wednesday 25 November 2026")
        assert stamped.endswith("\n\nhow was my week?")

    def test_dates_info(self):
        text = ccs.dates_info(["2026-11-17", "nope"], utc(2026, 11, 25, 3, 0))
        assert "2026-11-17 = Tuesday 17 November 2026 (8 days ago)" in text
        assert "'nope' is not a YYYY-MM-DD date." in text

    def test_to_utc_variants(self):
        assert ccs.to_utc("2026-11-16T14:30:00+00:00") == utc(2026, 11, 16, 14, 30)
        assert ccs.to_utc("2026-11-16T14:30:00Z") == utc(2026, 11, 16, 14, 30)
        assert ccs.to_utc(dt.datetime(2026, 11, 16, 14, 30)) == utc(2026, 11, 16, 14, 30)
        assert ccs.to_utc("garbage") is None and ccs.to_utc(None) is None


# ──────────────────────────────────────────────
# Trade index
# ──────────────────────────────────────────────

def write_trade(base, name, messages, *, outcome=None, state=None, reflections=None,
                review=None, mtime=None):
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    history = [{"message_id": str(mid), "timestamp": ts, "author": author,
                "content": text, "images": images}
               for mid, ts, author, text, images in messages]
    (folder / "history.json").write_text(json.dumps(history))
    for fname, data in (("outcome.json", outcome), ("psych.json", state),
                        ("reflections.json", reflections), ("review.json", review)):
        if data is not None:
            (folder / fname).write_text(json.dumps(data))
    if mtime:
        os.utime(folder / "history.json", (mtime, mtime))
    return folder


@pytest.fixture
def journal(tmp_path):
    trades, missed = tmp_path / "Fractal_Trades", tmp_path / "Missed_Trades"
    # Mon 16 Nov New York session (Tue 17 Nov in Melbourne), a FOMO loss.
    write_trade(trades, "trade_412_gc", [
        (1, "2026-11-16T14:30:00+00:00", "malith", "Chased the breakout, felt rushed", []),
        (2, "2026-11-16T14:35:00+00:00", "malith", "", ["1_chart.png"]),
        (3, "2026-11-16T14:40:00+00:00", "TradingBot", "📌 Recorded Loss", []),
    ], outcome={"outcome": "loss", "r_multiple": -1}, state={
        "fear_spectrum": 4, "fear_zone": "fomo", "emotional_context": ["anxious"], "conviction": 8},
        reflections={"why_entered": "Breakout", "how_felt": "Rushed"})
    # Mon 9 Nov New York, a win in balance.
    write_trade(trades, "trade_405_es", [
        (10, "2026-11-09T15:00:00+00:00", "malith", "Waited for CISD, calm entry", []),
    ], outcome={"outcome": "win", "r_multiple": 2}, state={
        "fear_spectrum": 0, "fear_zone": "balanced", "emotional_context": ["calm"], "conviction": 6})
    # Fri 20 Nov, no outcome yet.
    write_trade(trades, "trade_420_gc", [
        (20, "2026-11-20T14:00:00+00:00", "malith", "Patient, no FOMO today", []),
    ])
    write_trade(missed, "missed_7_nq", [
        (30, "2026-11-18T14:00:00+00:00", "malith", "Saw the sweep, froze", []),
    ], review={"why_missed": "Scared after Monday's loss"})
    return trades, missed


def index_for(journal):
    return ccs.TradeIndex(*journal).get({"TradingBot"})


class TestIndex:

    def test_records_newest_first_with_parsed_names(self, journal):
        records = index_for(journal)
        assert [r["channel"] for r in records] == \
            ["trade_420_gc", "missed_7_nq", "trade_412_gc", "trade_405_es"]
        gc = records[2]
        assert (gc["kind"], gc["number"], gc["asset"]) == ("taken", 412, "gc")
        assert records[1]["kind"] == "missed"

    def test_bot_messages_are_dropped(self, journal):
        gc = next(r for r in index_for(journal) if r["channel"] == "trade_412_gc")
        assert all("Recorded" not in m["text"] for m in gc["messages"])
        assert len(gc["messages"]) == 2                     # note + chart-only message
        assert gc["outcome"] == "loss" and gc["r"] == -1

    def test_stale_renamed_folder_is_skipped(self, tmp_path):
        trades = tmp_path / "Fractal_Trades"
        msgs = [(1, "2026-11-16T14:30:00+00:00", "malith", "note", [])]
        write_trade(trades, "trade_80_rty", msgs, mtime=1_000_000)
        write_trade(trades, "trade_80_rty_win", msgs, mtime=2_000_000)
        records = ccs.build_index(trades, tmp_path / "none")
        assert [r["channel"] for r in records] == ["trade_80_rty_win"]

    def test_partially_overlapping_folders_both_kept(self, tmp_path):
        trades = tmp_path / "Fractal_Trades"
        write_trade(trades, "a", [(1, "2026-11-16T14:30:00+00:00", "m", "x", [])], mtime=1_000_000)
        write_trade(trades, "b", [(1, "2026-11-16T14:30:00+00:00", "m", "x", []),
                                  (2, "2026-11-16T14:31:00+00:00", "m", "y", [])], mtime=2_000_000)
        assert len(ccs.build_index(trades, tmp_path / "none")) == 1   # a ⊂ b → stale
        write_trade(trades, "c", [(3, "2026-11-17T14:30:00+00:00", "m", "z", [])], mtime=500_000)
        assert len(ccs.build_index(trades, tmp_path / "none")) == 2

    def test_corrupt_files_do_not_break_the_index(self, tmp_path):
        trades = tmp_path / "Fractal_Trades"
        folder = write_trade(trades, "trade_1_gc", [(1, "2026-11-16T14:30:00+00:00", "m", "x", [])])
        (folder / "outcome.json").write_text("{broken")
        (trades / "trade_2_es").mkdir()
        (trades / "trade_2_es" / "history.json").write_text("not json")
        records = ccs.build_index(trades, tmp_path / "none")
        assert records[0]["outcome"] is None

    def test_missing_dirs(self, tmp_path):
        assert ccs.build_index(tmp_path / "x", tmp_path / "y") == []

    def test_cache_ttl(self, journal):
        clock = [0.0]
        index = ccs.TradeIndex(*journal, ttl=60, clock=lambda: clock[0])
        first = index.get()
        assert index.get() is first
        clock[0] = 61
        assert index.get() is not first


# ──────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────

NOW = utc(2026, 11, 25, 3, 0)


@pytest.fixture
def tools(journal, tmp_path):
    insights = tmp_path / "master_insights.json"
    insights.write_text(json.dumps({"good_habits": ["Waited for C2", "Calm entries"],
                                    "mistakes": ["Chased breakouts", "Moved stop"]}))
    missed_insights = tmp_path / "missed_insights.json"
    missed_insights.write_text(json.dumps({"miss_reasons": ["Froze after a loss"],
                                           "setup_patterns": [], "action_items": []}))
    return ccs.CoachTools(ccs.ChatMemory(tmp_path / "chat"), ccs.TradeIndex(*journal), NOW,
                          {"TradingBot"}, insights, missed_insights,
                          knowledge_search=AsyncMock(return_value=("CISD rules…", ["cisd_article.md"])))


class TestTools:

    @pytest.mark.asyncio
    async def test_find_by_asset_shows_dual_dates_and_state(self, tools):
        out = await tools.run("find_trades", json.dumps({"asset": "GC"}))
        assert out.startswith("2 trade(s) match")
        assert "trade_412_gc · taken · Tue 17 Nov 2026 01:30 AEDT (Mon 16 Nov 09:30 New York), 8 days ago" in out
        assert "loss -1R" in out and "fear +4 (fomo)" in out and "conviction 8/10" in out
        assert "1 notes, 1 charts" in out and "reflection form ✓" in out

    @pytest.mark.asyncio
    async def test_weekday_filter_respects_time_zone(self, tools):
        # The GC loss is a Monday in New York but a Tuesday in Melbourne.
        ny = await tools.run("find_trades", json.dumps({"weekday": "Monday", "date_tz": "new_york"}))
        mel = await tools.run("find_trades", json.dumps({"weekday": "Tue", "date_tz": "melbourne"}))
        assert "trade_412_gc" in ny and "trade_412_gc" in mel
        assert "trade_412_gc" not in await tools.run("find_trades", json.dumps({"weekday": "Mon"}))

    @pytest.mark.asyncio
    async def test_date_range_and_order(self, tools):
        out = await tools.run("find_trades", json.dumps(
            {"date_from": "2026-11-16", "date_to": "2026-11-20", "date_tz": "new_york", "order": "oldest"}))
        assert out.index("trade_412_gc") < out.index("missed_7_nq") < out.index("trade_420_gc")
        assert "trade_405_es" not in out

    @pytest.mark.asyncio
    async def test_other_filters(self, tools):
        assert "trade_420_gc" in await tools.run("find_trades", '{"outcome": "none", "kind": "taken"}')
        assert "missed_7_nq" not in await tools.run("find_trades", '{"kind": "taken"}')
        assert "trade_405_es" in await tools.run("find_trades", '{"keywords": "CISD calm"}')
        assert "trade_412_gc" in await tools.run("find_trades", '{"fear_min": 3}')
        assert "trade_405_es" in await tools.run("find_trades", '{"emotion": "calm"}')
        assert await tools.run("find_trades", '{"asset": "cl"}') == "No trades match those filters."

    @pytest.mark.asyncio
    async def test_bad_arguments_go_back_to_the_model(self, tools):
        assert "couldn't read the arguments" in await tools.run("find_trades", "{not json")
        assert "is not a YYYY-MM-DD date" in await tools.run("find_trades", '{"date_from": "last week"}')
        assert "is not a weekday" in await tools.run("find_trades", '{"weekday": "Funday"}')
        assert "bad arguments" in await tools.run("find_trades", '{"colour": "red"}')
        assert "no tool called" in await tools.run("buy_gold", "{}")

    @pytest.mark.asyncio
    async def test_read_trade(self, tools):
        out = await tools.run("read_trade", '{"channel": "#trade_412_gc"}')
        assert "Started: Tue 17 Nov 2026 01:30 AEDT (Mon 16 Nov 09:30 New York) — 8 days ago" in out
        assert "- How I felt: Rushed" in out
        assert "[Tue 17 Nov 01:30 AEDT / Mon 16 Nov 09:30 New York] Chased the breakout" in out
        assert "(chart only) (+1 chart)" in out
        assert "Recorded Loss" not in out
        assert "Fear spectrum: +4" in out

    @pytest.mark.asyncio
    async def test_read_missed_trade_and_unknown(self, tools):
        assert "why missed: Scared after Monday's loss" in await tools.run("read_trade", '{"channel": "missed_7_nq"}')
        out = await tools.run("read_trade", '{"channel": "trade_412_g"}')
        assert "No trade channel called trade_412_g" in out and "trade_412_gc" in out

    @pytest.mark.asyncio
    async def test_trade_stats(self, tools):
        out = await tools.run("trade_stats", '{"kind": "taken"}')
        assert "3 trades match; 2 have an outcome" in out
        assert "Overall: 2 trades, 1W/1L/0BE, win rate 50%, avg +0.50R" in out
        assert "- gc: 2 trades — 1 trades, 0W/1L/0BE, win rate 0%, avg -1.00R" in out
        assert "By weekday, New York session date:\n- Mon: 2 trades" in out
        assert "- Tue: 2 trades" in out.split("By weekday, Melbourne date:")[1]
        assert "Fear zone fomo: 1 trades, 0W/1L" in out
        assert "Conviction (2 trades): not enough decided trades" in out

    @pytest.mark.asyncio
    async def test_trade_stats_without_psychology(self, tools):
        out = await tools.run("trade_stats", '{"asset": "gc", "outcome": "none"}')
        assert "can't be checked against results" in out

    @pytest.mark.asyncio
    async def test_insights(self, tools):
        out = await tools.run("get_insights", '{"keyword": "chase"}')
        assert "mistakes: 2 total, 1 mention 'chase'" in out and "- Chased breakouts" in out
        assert "Calm entries" not in out
        assert "Froze after a loss" in await tools.run("get_insights", '{"kind": "missed"}')

    @pytest.mark.asyncio
    async def test_knowledge(self, tools):
        assert "Sources: cisd_article.md" in await tools.run("search_knowledge", '{"query": "cisd"}')
        tools.knowledge_search = AsyncMock(side_effect=ConnectionError("refused"))
        assert "needs Ollama running" in await tools.run("search_knowledge", '{"query": "cisd"}')

    @pytest.mark.asyncio
    async def test_remember_forget_recall(self, tools):
        assert await tools.run("remember", '{"note": "Freezes after two losses"}') == "Saved as note 1."
        memory = await tools.memory.load()
        assert memory["notes"][0]["text"] == "Freezes after two losses"
        assert await tools.run("forget", '{"note_id": 1}') == "Deleted note 1."
        assert await tools.run("forget", '{"note_id": 1}') == "There is no note 1."
        assert "the note is empty" in await tools.run("remember", '{"note": "  "}')

        await tools.memory.append({"time": "2026-11-18T03:00:00+00:00", "user": "Monday GC loss stung",
                                   "reply": "What did it remind you of?", "messages": []})
        await tools.memory.append({"time": "2026-11-24T03:00:00+00:00", "user": "Calm week",
                                   "reply": "Good.", "messages": []})
        out = await tools.run("recall_conversations", '{"query": "stung"}')
        assert "[Wed 18 Nov 2026 14:00 AEDT (7 days ago)]" in out and "Calm week" not in out
        dated = await tools.run("recall_conversations", '{"date_from": "2026-11-24"}')
        assert "Calm week" in dated and "stung" not in dated

    @pytest.mark.asyncio
    async def test_dates_info_tool(self, tools):
        assert "Monday 16 November 2026" in await tools.run("dates_info", '{"dates": ["2026-11-16"]}')
        assert "Monday 16 November 2026" in await tools.run("dates_info", '{"dates": "2026-11-16"}')

    @pytest.mark.asyncio
    async def test_results_are_capped(self, tools, monkeypatch):
        monkeypatch.setattr(ccs, "MAX_TOOL_RESULT_CHARS", 300)
        tools.knowledge_search = AsyncMock(return_value=("x" * 5000, ["a.md"]))
        out = await tools.run("search_knowledge", '{"query": "q"}')
        assert len(out) <= 300 and "narrow the lookup" in out

    def test_tool_schemas_are_well_formed(self):
        names = [t["function"]["name"] for t in ccs.TOOLS]
        assert len(names) == len(set(names))
        for t in ccs.TOOLS:
            assert t["type"] == "function"
            assert t["function"]["parameters"]["type"] == "object"
            assert hasattr(ccs.CoachTools, f"_tool_{t['function']['name']}")


# ──────────────────────────────────────────────
# Memory and prompt
# ──────────────────────────────────────────────

class TestMemory:

    @pytest.mark.asyncio
    async def test_exchanges_are_numbered_and_replayed_verbatim(self, tmp_path):
        memory = ccs.ChatMemory(tmp_path)
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "", "reasoning_content": "think",
                 "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "r"},
                {"role": "assistant", "content": "hello", "reasoning_content": "more"}]
        first = await memory.append({"time": "t", "user": "hi", "reply": "hello", "messages": msgs})
        second = await memory.append({"time": "t", "user": "b", "reply": "c", "messages": msgs[:1]})
        assert (first["id"], second["id"]) == (1, 2)
        replay = await memory.replay()
        assert replay[:4] == msgs                         # reasoning_content survives the round trip
        assert len(replay) == 5

    @pytest.mark.asyncio
    async def test_replay_skips_summarized_and_caps(self, tmp_path):
        memory = ccs.ChatMemory(tmp_path)
        for i in range(ccs.MAX_REPLAY + 4):
            await memory.append({"time": "t", "user": str(i), "reply": "", "messages": [{"role": "user", "content": str(i)}]})
        replay = await memory.replay()
        assert len(replay) == ccs.MAX_REPLAY and replay[-1]["content"] == str(ccs.MAX_REPLAY + 3)
        state = await memory.load()
        state["summarized_through"] = ccs.MAX_REPLAY + 2
        await memory.save(state)
        assert [m["content"] for m in await memory.replay()] == [str(ccs.MAX_REPLAY + 2), str(ccs.MAX_REPLAY + 3)]

    @pytest.mark.asyncio
    async def test_corrupt_lines_skipped_and_defaults(self, tmp_path):
        memory = ccs.ChatMemory(tmp_path)
        tmp_path.mkdir(exist_ok=True)
        memory.conversation_path.write_text('{"id": 1, "messages": []}\nbroken\n{"id": 2}\n')
        assert [e["id"] for e in await memory.exchanges()] == [1]
        memory.memory_path.write_text("{bad")
        assert (await memory.load())["notes"] == []

    def test_api_message_shape(self):
        full = {"content": "x", "reasoning_content": "r", "tool_calls": [{"id": 1}], "usage": {"a": 1}}
        assert ccs.api_message(full) == {"role": "assistant", "content": "x", "reasoning_content": "r",
                                         "tool_calls": [{"id": 1}]}
        assert "tool_calls" not in ccs.api_message(full, drop_tool_calls=True)
        assert ccs.api_message({"content": None}) == {"role": "assistant", "content": ""}

    def test_system_prompt_is_stable_and_dateless(self):
        corpus = cs.load_corpus()
        a = ccs.build_system_prompt(corpus, {"notes": [], "summary": ""})
        b = ccs.build_system_prompt(corpus, {"notes": [], "summary": ""})
        assert a == b
        assert "Today =" not in a and "2026" not in a.split("=== YOUR NOTES")[0]
        assert a.startswith(ccs.COACH_PERSONA)
        assert "post-loss-processing" in a

    def test_system_prompt_carries_notes_and_summary(self):
        prompt = ccs.build_system_prompt(cs.load_corpus(), {
            "notes": [{"id": 3, "text": "Freezes after losses", "created": "2026-11-17T01:00:00+00:00"}],
            "summary": "Talked about Monday losses."})
        assert "[3] (Tue 17 Nov 2026) Freezes after losses" in prompt
        assert "Talked about Monday losses." in prompt

    def test_fold_prompt_keeps_dates(self):
        prompt = ccs.build_fold_prompt("old", [{"time": "2026-11-16T14:30:00+00:00",
                                                "user": "u", "reply": "r"}])
        assert "[Tue 17 Nov 2026 01:30 AEDT]" in prompt and "old" in prompt


class TestCost:

    def test_usage_cost(self):
        usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
                 "completion_tokens": 1_000_000}
        prices = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60}
        assert ccs.usage_cost([usage], peak=False, prices=prices) == pytest.approx(0.753)
        assert ccs.usage_cost([usage, usage], peak=True, prices=prices) == pytest.approx(0.753 * 4)

    def test_usage_without_cache_fields_counts_as_misses(self):
        prices = {"cache_hit": 0, "cache_miss": 1.0, "output": 0}
        assert ccs.usage_cost([{"prompt_tokens": 1_000_000}], False, prices) == pytest.approx(1.0)


# ──────────────────────────────────────────────
# LLMService.chat
# ──────────────────────────────────────────────

class TestLLMChat:

    @pytest.mark.asyncio
    async def test_returns_message_and_sends_tools(self):
        service = LLMService("deepseek")
        service._get_session = AsyncMock()
        service._post_deepseek = AsyncMock(return_value={
            "choices": [{"message": {"content": None, "reasoning_content": "r",
                                     "tool_calls": [{"id": "c"}]}}], "usage": {"completion_tokens": 5}})
        with patch("services.llm_service.DEEPSEEK_API_KEY", "k"):
            out = await service.chat([{"role": "user", "content": "x"}], tools=ccs.TOOLS,
                                     tool_choice="none", reasoning_effort="high", max_tokens=100)
        payload = service._post_deepseek.call_args[0][3]
        assert payload["tools"] == ccs.TOOLS and payload["tool_choice"] == "none"
        assert payload["thinking"] == {"type": "enabled", "reasoning_effort": "high"}
        assert out == {"role": "assistant", "content": "", "reasoning_content": "r",
                       "tool_calls": [{"id": "c"}], "usage": {"completion_tokens": 5}}

    @pytest.mark.asyncio
    async def test_empty_reply_retries_then_fails(self):
        service = LLMService("deepseek")
        service._get_session = AsyncMock()
        service._post_deepseek = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
        with patch("services.llm_service.DEEPSEEK_API_KEY", "k"), \
             patch("services.llm_service.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(LLMError, match="empty content"):
                await service.chat([{"role": "user", "content": "x"}], retries=2)
        assert service._post_deepseek.await_count == 2

    @pytest.mark.asyncio
    async def test_permanent_error_not_retried(self):
        service = LLMService("deepseek")
        service._get_session = AsyncMock()
        err = LLMError("DeepSeek HTTP 401")
        err.permanent = True
        service._post_deepseek = AsyncMock(side_effect=err)
        with patch("services.llm_service.DEEPSEEK_API_KEY", "k"):
            with pytest.raises(LLMError):
                await service.chat([{"role": "user", "content": "x"}], retries=3)
        assert service._post_deepseek.await_count == 1

    @pytest.mark.asyncio
    async def test_needs_deepseek(self):
        with pytest.raises(LLMError, match="LLM_PROVIDER=deepseek"):
            await LLMService("ollama").chat([])


# ──────────────────────────────────────────────
# The cog
# ──────────────────────────────────────────────

def reply(content="", tool_calls=None, usage=None):
    return {"role": "assistant", "content": content, "reasoning_content": "thinking…",
            "tool_calls": tool_calls or [],
            "usage": usage or {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000,
                               "completion_tokens": 500}}


def call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


OFF_PEAK = utc(2026, 11, 24, 15, 0)    # Tue 02:00 AEDT
PEAK = utc(2026, 11, 25, 2, 0)         # Wed 13:00 AEDT, inside 01:00–04:00 UTC


@pytest.fixture
def chat_cog(journal, tmp_path):
    bot = MagicMock()
    bot.user.name = "TradingBot"
    clock = {"now": OFF_PEAK}
    cog = CoachChat(bot, memory=ccs.ChatMemory(tmp_path / "chat"),
                    index=ccs.TradeIndex(*journal), now=lambda: clock["now"])
    cog.clock = clock
    return cog


def chat_message(content="How did my GC trades go last week?", *, channel="coach", is_bot=False,
                 author_id=42, attachments=()):
    m = MagicMock()
    m.author.bot = is_bot
    m.author.id = author_id
    m.guild = MagicMock()
    m.content = content
    m.attachments = list(attachments)
    m.created_at = OFF_PEAK
    m.channel = MagicMock()
    m.channel.name = channel
    m.channel.send = AsyncMock()
    m.channel.typing = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)))
    return m


def sent(message):
    return "\n".join(c.args[0] for c in message.channel.send.call_args_list)


class TestConversation:

    @pytest.mark.asyncio
    async def test_tool_loop_answers_and_stores_everything(self, chat_cog):
        script = [reply(tool_calls=[call("find_trades", {"asset": "gc"})]),
                  reply("Both GC trades were Mondays in New York — what was different about the 16th?")]
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(side_effect=script)) as chat:
            msg = chat_message()
            await chat_cog.on_message(msg)

        assert "what was different about the 16th?" in sent(msg)
        first_request = chat.call_args_list[0][0][0]
        assert first_request[0]["role"] == "system"
        assert first_request[-1]["content"].startswith("[Now: Wednesday 25 November 2026, 02:00 AEDT")
        second_request = chat.call_args_list[1][0][0]
        tool_result = second_request[-1]
        assert tool_result["role"] == "tool" and tool_result["tool_call_id"] == "c1"
        assert "trade_412_gc" in tool_result["content"]

        [exchange] = await chat_cog.memory.exchanges()
        roles = [m["role"] for m in exchange["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert all(m.get("reasoning_content") == "thinking…" for m in exchange["messages"] if m["role"] == "assistant")
        assert exchange["user"] == "How did my GC trades go last week?"
        assert len(exchange["usage"]) == 2

    @pytest.mark.asyncio
    async def test_next_turn_replays_previous_exchange(self, chat_cog):
        with patch("cogs.coach_chat.llm_service.chat",
                   new=AsyncMock(side_effect=[reply("First answer"), reply("Second answer")])) as chat:
            await chat_cog.on_message(chat_message("first"))
            await chat_cog.on_message(chat_message("second"))
        request = chat.call_args_list[1][0][0]
        contents = [m.get("content") for m in request]
        assert any(c.endswith("first") for c in contents if c)
        assert "First answer" in contents
        assert request[-1]["content"].endswith("second")

    @pytest.mark.asyncio
    async def test_loop_is_capped_with_forced_answer(self, chat_cog):
        looping = [reply(tool_calls=[call("dates_info", {"dates": ["2026-11-16"]}, f"c{i}")])
                   for i in range(MAX_TOOL_ROUNDS)]
        final = reply("Here's what I've got.", tool_calls=[call("dates_info", {"dates": []}, "stray")])
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(side_effect=looping + [final])) as chat:
            out, _ = await chat_cog.respond("loop forever", OFF_PEAK)
        assert out == "Here's what I've got."
        assert chat.await_count == MAX_TOOL_ROUNDS + 1
        assert chat.call_args_list[-1].kwargs["tool_choice"] == "none"
        assert all(c.kwargs["tool_choice"] is None for c in chat.call_args_list[:-1])
        [exchange] = await chat_cog.memory.exchanges()
        assert "tool_calls" not in exchange["messages"][-1]   # the stray call can't be left unanswered

    @pytest.mark.asyncio
    async def test_failure_saves_nothing(self, chat_cog):
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(side_effect=LLMError("HTTP 500"))):
            msg = chat_message()
            await chat_cog.on_message(msg)
        assert "couldn't reply" in sent(msg) and "Nothing was saved" in sent(msg)
        assert await chat_cog.memory.exchanges() == []

    @pytest.mark.asyncio
    async def test_empty_final_answer_is_a_failure(self, chat_cog):
        script = [reply(tool_calls=[call("dates_info", {"dates": []}, f"c{i}")]) for i in range(MAX_TOOL_ROUNDS)]
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(side_effect=script + [reply("")])):
            with pytest.raises(LLMError):
                await chat_cog.respond("x", OFF_PEAK)
        assert await chat_cog.memory.exchanges() == []

    @pytest.mark.asyncio
    async def test_forbidden_language_is_flagged(self, chat_cog):
        phrase = cs.forbidden_phrases(chat_cog._corpus)[0]
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(return_value=reply(f"Honestly, {phrase}."))):
            msg = chat_message()
            await chat_cog.on_message(msg)
        assert "🚨" in sent(msg) and phrase in sent(msg)

    @pytest.mark.asyncio
    async def test_no_peak_notice_off_peak(self, chat_cog):
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(return_value=reply("ok"))):
            msg = chat_message()
            await chat_cog.on_message(msg)
        assert "peak" not in sent(msg)

    @pytest.mark.asyncio
    async def test_peak_notice_in_full_once_then_short(self, chat_cog):
        chat_cog.clock["now"] = PEAK
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(return_value=reply("ok"))):
            first, second = chat_message(), chat_message()
            await chat_cog.on_message(first)
            await chat_cog.on_message(second)
        assert "DeepSeek peak pricing (2×) is on until <t:" in sent(first)
        assert sent(second).endswith("-# ⏱️ peak pricing · ≈ $0.001")
        assert "is on until" not in sent(second)

        # A new peak window gets the full notice again.
        chat_cog.clock["now"] = utc(2026, 11, 25, 7, 0)    # 06:00–10:00 UTC window
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(return_value=reply("ok"))):
            third = chat_message()
            await chat_cog.on_message(third)
        assert "is on until" in sent(third)
        [*_, last] = await chat_cog.memory.exchanges()
        assert last["cost_usd"] == pytest.approx(ccs.usage_cost([reply()["usage"]], peak=True), abs=1e-5)

    def test_peak_line_formats(self):
        full = peak_line(PEAK, 0.0123, True)
        ends = int(utc(2026, 11, 25, 4, 0).timestamp())                # window ends 04:00 UTC
        assert f"<t:{ends}:t>" in full and f"<t:{ends}:R>" in full and "≈ $0.012" in full
        assert peak_line(PEAK, 0.0123, False) == "-# ⏱️ peak pricing · ≈ $0.012"


class TestFilters:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [
        {"is_bot": True}, {"channel": "general"}, {"channel": "trade_412_gc"},
    ])
    async def test_ignored_messages(self, chat_cog, kwargs):
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock()) as chat:
            msg = chat_message(**kwargs)
            await chat_cog.on_message(msg)
        chat.assert_not_awaited()
        msg.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_filter(self, chat_cog):
        with patch("cogs.coach_chat.COACH_CHAT_USER_ID", 7), \
             patch("cogs.coach_chat.llm_service.chat", new=AsyncMock(return_value=reply("hi"))) as chat:
            await chat_cog.on_message(chat_message(author_id=42))
            chat.assert_not_awaited()
            await chat_cog.on_message(chat_message(author_id=7))
            chat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_locked_guild_ignored(self, chat_cog):
        with patch("cogs.coach_chat.is_allowed_guild", return_value=False), \
             patch("cogs.coach_chat.llm_service.chat", new=AsyncMock()) as chat:
            await chat_cog.on_message(chat_message())
        chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_image_only_message_explains(self, chat_cog):
        with patch("cogs.coach_chat.llm_service.chat", new=AsyncMock()) as chat:
            msg = chat_message("", attachments=[MagicMock()])
            await chat_cog.on_message(msg)
        chat.assert_not_awaited()
        assert "can't see images" in sent(msg)


class TestFolding:

    @pytest.mark.asyncio
    async def test_folds_old_exchanges_into_summary(self, chat_cog):
        for i in range(ccs.FOLD_AT + 1):
            await chat_cog.memory.append({"time": "2026-11-16T14:30:00+00:00", "user": f"u{i}",
                                          "reply": f"r{i}", "messages": []})
        with patch("cogs.coach_chat.llm_service.complete", new=AsyncMock(return_value=" New summary ")) as complete:
            await chat_cog.fold_memory()
        prompt = complete.call_args[0][0]
        assert "u0" in prompt and f"u{ccs.FOLD_AT + 1 - ccs.KEEP_RECENT - 1}" in prompt
        assert f"u{ccs.FOLD_AT}" not in prompt
        assert complete.call_args.kwargs["reasoning_effort"] == "off"
        memory = await chat_cog.memory.load()
        assert memory["summary"] == "New summary"
        assert len(await chat_cog.memory.unsummarized()) == ccs.KEEP_RECENT

    @pytest.mark.asyncio
    async def test_no_fold_below_threshold(self, chat_cog):
        for i in range(ccs.FOLD_AT):
            await chat_cog.memory.append({"time": "t", "user": "u", "reply": "r", "messages": []})
        with patch("cogs.coach_chat.llm_service.complete", new=AsyncMock()) as complete:
            await chat_cog.fold_memory()
        complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_fold_keeps_exchanges(self, chat_cog):
        for i in range(ccs.FOLD_AT + 3):
            await chat_cog.memory.append({"time": "t", "user": "u", "reply": "r", "messages": []})
        with patch("cogs.coach_chat.llm_service.complete", new=AsyncMock(side_effect=LLMError("x"))):
            await chat_cog.fold_memory()
        assert len(await chat_cog.memory.unsummarized()) == ccs.FOLD_AT + 3


class TestCommands:

    @pytest.mark.asyncio
    async def test_notes_and_forget(self, chat_cog):
        interaction = MagicMock()
        interaction.response = AsyncMock()
        await chat_cog.coach_notes.callback(chat_cog, interaction)
        assert "hasn't noted anything" in interaction.response.send_message.call_args[0][0]

        await chat_cog.memory.add_note("Freezes after two losses", utc(2026, 11, 16, 14, 30))
        await chat_cog.coach_notes.callback(chat_cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs["embed"]
        assert "`1` · Tue 17 Nov 2026 — Freezes after two losses" in embed.description

        await chat_cog.coach_forget.callback(chat_cog, interaction, 1)
        assert "Forgot note 1" in interaction.response.send_message.call_args[0][0]
        await chat_cog.coach_forget.callback(chat_cog, interaction, 1)
        assert "There's no note 1" in interaction.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_setup_creates_channel_once(self, chat_cog):
        interaction = MagicMock()
        interaction.response = AsyncMock()
        interaction.guild.text_channels = []
        channel = MagicMock()
        channel.send = AsyncMock()
        interaction.guild.create_text_channel = AsyncMock(return_value=channel)
        await chat_cog.coach_chat_setup.callback(chat_cog, interaction)
        assert interaction.guild.create_text_channel.call_args[0][0] == "coach"
        channel.send.assert_awaited_once()

        existing = MagicMock()
        existing.name = "coach"
        interaction.guild.text_channels = [existing]
        interaction.guild.create_text_channel.reset_mock()
        await chat_cog.coach_chat_setup.callback(chat_cog, interaction)
        interaction.guild.create_text_channel.assert_not_called()


class TestSplit:

    def test_short_message_untouched(self):
        assert split_message("hello") == ["hello"]

    def test_splits_on_paragraphs_within_limit(self):
        text = ("a" * 1500) + "\n\n" + ("b" * 1500)
        chunks = split_message(text)
        assert chunks == ["a" * 1500, "b" * 1500]

    def test_hard_split_without_spaces(self):
        chunks = split_message("x" * 4500)
        assert [len(c) for c in chunks] == [2000, 2000, 500]
