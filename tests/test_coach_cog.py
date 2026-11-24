"""
Unit tests for cogs/coach.py

Covers the auto-coaching listener (channel filter, cooldown, toggle, threshold),
the grading log that feeds /coach_trust, and embed rendering — including that a
drifted response is shown as a red trust warning rather than presented as advice.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import ui

from cogs.coach import (
    Coach, GradeView, build_feedback_embed, read_grades, log_grade,
    COACH_COOLDOWN_SECONDS,
)
from services import coach_service as cs


@pytest.fixture
def corpus():
    return cs.load_corpus()


@pytest.fixture
def cog():
    return Coach(MagicMock())


@pytest.fixture
def interaction():
    i = AsyncMock()
    i.response = AsyncMock()
    i.followup = AsyncMock()
    i.channel = MagicMock()
    i.channel.name = "trade_1_gc"
    i.user = "trader"
    return i


def make_message(content, category="Fractal_Trades", is_bot=False, guild=True, channel_id=1):
    m = MagicMock()
    m.author.bot = is_bot
    m.guild = MagicMock() if guild else None
    m.content = content
    m.channel = MagicMock()
    m.channel.id = channel_id
    m.channel.name = "trade_1_gc"
    m.channel.send = AsyncMock()
    m.channel.typing = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)))
    if category is None:
        m.channel.category = None
    else:
        m.channel.category = MagicMock()
        m.channel.category.name = category
    return m


SUBSTANTIVE = ("Stopped out and went straight back in on the next candle to make it "
               "back, then lost that one too. I had to get it back before the close.")

FEEDBACK = {
    "reading": "You went looking for the money rather than the setup.",
    "observations": [{"principle_id": "post-loss-processing",
                      "quote": "straight back in",
                      "point": "Acting instead of feeling."}],
    "probe": "What were you feeling in the second before you clicked?",
    "not_enough_signal": False,
    "warnings": [],
    "forbidden_hits": [],
}


# ──────────────────────────────────────────────
# Auto-coaching listener
# ──────────────────────────────────────────────

class TestAutoCoaching:

    @pytest.mark.asyncio
    async def test_coaches_substantive_journal_entry(self, cog):
        msg = make_message(SUBSTANTIVE)
        with patch("cogs.coach.cs.coach", new=AsyncMock(return_value=FEEDBACK)), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(msg)
        msg.channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self, cog):
        msg = make_message(SUBSTANTIVE, is_bot=True)
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.on_message(msg)
        c.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_dms(self, cog):
        msg = make_message(SUBSTANTIVE, guild=False)
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.on_message(msg)
        c.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_non_journal_channels(self, cog):
        msg = make_message(SUBSTANTIVE, category="General")
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.on_message(msg)
        c.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_channels_without_category(self, cog):
        msg = make_message(SUBSTANTIVE, category=None)
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.on_message(msg)
        c.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_works_in_missed_trades(self, cog):
        msg = make_message(SUBSTANTIVE, category="Missed_Trades2")
        with patch("cogs.coach.cs.coach", new=AsyncMock(return_value=FEEDBACK)), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(msg)
        msg.channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_short_entries(self, cog):
        """A price note shouldn't burn a model call."""
        msg = make_message("ES 5810.25")
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.on_message(msg)
        c.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_the_toggle(self, cog):
        msg = make_message(SUBSTANTIVE)
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c, \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=False)):
            await cog.on_message(msg)
        c.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_burst(self, cog):
        """Journalling arrives in bursts; one reading per burst."""
        coach_mock = AsyncMock(return_value=FEEDBACK)
        with patch("cogs.coach.cs.coach", new=coach_mock), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(make_message(SUBSTANTIVE))
            await cog.on_message(make_message(SUBSTANTIVE))
            await cog.on_message(make_message(SUBSTANTIVE))
        assert coach_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_cooldown_is_per_channel(self, cog):
        coach_mock = AsyncMock(return_value=FEEDBACK)
        with patch("cogs.coach.cs.coach", new=coach_mock), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(make_message(SUBSTANTIVE, channel_id=1))
            await cog.on_message(make_message(SUBSTANTIVE, channel_id=2))
        assert coach_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_cooldown_expires(self, cog):
        coach_mock = AsyncMock(return_value=FEEDBACK)
        with patch("cogs.coach.cs.coach", new=coach_mock), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(make_message(SUBSTANTIVE))
            cog._last_coached[1] -= (COACH_COOLDOWN_SECONDS + 1)
            await cog.on_message(make_message(SUBSTANTIVE))
        assert coach_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_coaching_failure_is_swallowed(self, cog):
        """A model outage must not break the journal channel."""
        msg = make_message(SUBSTANTIVE)
        with patch("cogs.coach.cs.coach", new=AsyncMock(side_effect=cs.CoachError("down"))), \
             patch.object(cog, "_auto_enabled", AsyncMock(return_value=True)):
            await cog.on_message(msg)
        msg.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_when_corpus_failed_to_load(self):
        with patch("cogs.coach.cs.load_corpus", side_effect=cs.CoachError("missing")):
            broken = Coach(MagicMock())
        assert broken.corpus is None
        msg = make_message(SUBSTANTIVE)
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await broken.on_message(msg)
        c.assert_not_awaited()


# ──────────────────────────────────────────────
# Embed rendering
# ──────────────────────────────────────────────

class TestFeedbackEmbed:

    def test_shows_principle_id_on_every_observation(self, corpus):
        embed = build_feedback_embed(FEEDBACK, corpus)
        assert any("[post-loss-processing]" in f.name for f in embed.fields)

    def test_includes_quote_and_point(self, corpus):
        embed = build_feedback_embed(FEEDBACK, corpus)
        body = " ".join(f.value for f in embed.fields)
        assert "straight back in" in body
        assert "Acting instead of feeling" in body

    def test_medium_confidence_is_marked(self, corpus):
        """The reader should know which citations rest on synthesis."""
        medium = next(p["id"] for p in corpus["principles"] if p["confidence"] == "medium")
        fb = {**FEEDBACK, "observations": [
            {"principle_id": medium, "quote": "q", "point": "p"}]}
        embed = build_feedback_embed(fb, corpus)
        assert any("~" in f.name for f in embed.fields)

    def test_high_confidence_is_not_marked(self, corpus):
        embed = build_feedback_embed(FEEDBACK, corpus)
        assert not any("~" in f.name for f in embed.fields)

    def test_abstention_renders_plainly(self, corpus):
        fb = {"reading": "", "observations": [], "probe": "",
              "not_enough_signal": True, "warnings": [], "forbidden_hits": []}
        embed = build_feedback_embed(fb, corpus)
        assert "Nothing to read" in embed.title

    def test_drift_is_shown_as_a_red_warning(self, corpus):
        """A drifted response must be flagged, never presented as advice."""
        fb = {**FEEDBACK, "forbidden_hits": ["remove emotion"]}
        embed = build_feedback_embed(fb, corpus)
        assert embed.color == discord.Color.red()
        warning = next(f for f in embed.fields if "Trust warning" in f.name)
        assert "remove emotion" in warning.value
        assert "unreliable" in warning.value

    def test_caps_observations_at_three(self, corpus):
        fb = {**FEEDBACK, "observations": [
            {"principle_id": "fc-physical", "quote": "q", "point": "p"} for _ in range(6)]}
        embed = build_feedback_embed(fb, corpus)
        assert len([f for f in embed.fields if f.name.startswith("[")]) == 3


# ──────────────────────────────────────────────
# Grading
# ──────────────────────────────────────────────

def grade_button(view, needle):
    for child in view.children:
        if isinstance(child, ui.Button) and child.label and needle in child.label:
            return child
    raise AssertionError("button not found")


class TestGrading:

    @pytest.mark.asyncio
    async def test_thumbs_up_logged(self, cog, tmp_path):
        log = tmp_path / "coach_log.jsonl"
        view = GradeView(cog, {"entry": "e", "cited": ["fc-physical"]})
        i = AsyncMock()
        with patch("cogs.coach.COACH_LOG_PATH", log):
            await grade_button(view, "Useful").callback(i)

        records = await read_grades(log)
        assert records[0]["useful"] is True

    @pytest.mark.asyncio
    async def test_thumbs_down_logged(self, cog, tmp_path):
        log = tmp_path / "coach_log.jsonl"
        view = GradeView(cog, {"entry": "e", "cited": []})
        i = AsyncMock()
        with patch("cogs.coach.COACH_LOG_PATH", log):
            await grade_button(view, "Off the mark").callback(i)

        records = await read_grades(log)
        assert records[0]["useful"] is False

    @pytest.mark.asyncio
    async def test_double_grading_rejected(self, cog, tmp_path):
        log = tmp_path / "coach_log.jsonl"
        view = GradeView(cog, {"entry": "e"})
        with patch("cogs.coach.COACH_LOG_PATH", log):
            await grade_button(view, "Useful").callback(AsyncMock())
            await grade_button(view, "Off the mark").callback(AsyncMock())
        assert len(await read_grades(log)) == 1

    @pytest.mark.asyncio
    async def test_read_grades_skips_corruption(self, tmp_path):
        log = tmp_path / "coach_log.jsonl"
        log.write_text('{"useful": true}\nbroken\n\n{"useful": false}\n')
        assert len(await read_grades(log)) == 2

    @pytest.mark.asyncio
    async def test_read_grades_missing_file(self, tmp_path):
        assert await read_grades(tmp_path / "nope.jsonl") == []


# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────

class TestCommands:

    @pytest.mark.asyncio
    async def test_coach_command(self, cog, interaction):
        with patch("cogs.coach.cs.coach", new=AsyncMock(return_value=FEEDBACK)):
            await cog.coach_cmd.callback(cog, interaction, SUBSTANTIVE)
        assert interaction.followup.send.call_args.kwargs["embed"]

    @pytest.mark.asyncio
    async def test_coach_command_rejects_thin_input(self, cog, interaction):
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.coach_cmd.callback(cog, interaction, "ES 5810")
        c.assert_not_awaited()
        assert "too short" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_coach_command_reports_failure(self, cog, interaction):
        with patch("cogs.coach.cs.coach", new=AsyncMock(side_effect=cs.CoachError("no key"))):
            await cog.coach_cmd.callback(cog, interaction, SUBSTANTIVE)
        assert "no key" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_auto_toggle_persists(self, cog, interaction, tmp_path):
        settings = tmp_path / "coach_settings.json"
        with patch("cogs.coach.COACH_SETTINGS_PATH", settings):
            await cog.coach_auto.callback(cog, interaction, False)
            assert json.loads(settings.read_text())["auto"] is False
            assert await cog._auto_enabled() is False

            await cog.coach_auto.callback(cog, interaction, True)
            assert await cog._auto_enabled() is True

    @pytest.mark.asyncio
    async def test_trust_report_without_grades(self, cog, interaction, tmp_path):
        with patch("cogs.coach.COACH_LOG_PATH", tmp_path / "none.jsonl"):
            await cog.coach_trust.callback(cog, interaction)

        embed = interaction.followup.send.call_args.kwargs["embed"]
        fields = {f.name: f.value for f in embed.fields}
        assert any("No readings graded yet" in v for v in fields.values())
        # The caveat that no test can close must always be present.
        assert any("not transcribed from her book" in v.lower() for v in fields.values())

    @pytest.mark.asyncio
    async def test_trust_report_with_grades(self, cog, interaction, tmp_path):
        log = tmp_path / "coach_log.jsonl"
        await log_grade(log, {"useful": True, "warnings": [], "forbidden_hits": []})
        await log_grade(log, {"useful": True, "warnings": [], "forbidden_hits": []})
        await log_grade(log, {"useful": False,
                              "warnings": ["Dropped observation citing unknown principle 'x'."],
                              "forbidden_hits": ["remove emotion"]})

        with patch("cogs.coach.COACH_LOG_PATH", log):
            await cog.coach_trust.callback(cog, interaction)

        fields = {f.name: f.value for f in interaction.followup.send.call_args.kwargs["embed"].fields}
        useful_field = next(v for k, v in fields.items() if "useful to you" in k)
        assert "**2/3**" in useful_field
        assert "Fabricated citations caught and dropped: **1**" in useful_field
        assert "Anti-Shull language incidents: **1**" in useful_field

    @pytest.mark.asyncio
    async def test_trust_report_when_corpus_broken(self, interaction):
        with patch("cogs.coach.cs.load_corpus", side_effect=cs.CoachError("missing")):
            broken = Coach(MagicMock())
        await broken.coach_trust.callback(broken, interaction)
        assert "disabled" in interaction.followup.send.call_args[0][0]


# ──────────────────────────────────────────────
# Channel review (🧠 Shull Review button / /shull_review)
# ──────────────────────────────────────────────

from cogs.coach import gather_trade_entry, MAX_REVIEW_CHARS


def history_of(messages):
    """channel.history(...) stand-in: an async iterator over the given messages."""
    async def agen():
        for m in messages:
            yield m
    return MagicMock(side_effect=lambda **kwargs: agen())


def msg(content, is_bot=False):
    m = MagicMock()
    m.author.bot = is_bot
    m.content = content
    return m


def review_channel(messages, name="trade_7_gc", category="Fractal_Trades", channel_id=7):
    ch = MagicMock()
    ch.id = channel_id
    ch.name = name
    ch.history = history_of(messages)
    ch.category = MagicMock()
    ch.category.name = category
    return ch


def review_interaction(channel):
    i = MagicMock()
    i.channel = channel
    i.guild = MagicMock()
    i.response = AsyncMock()
    i.followup = AsyncMock()
    return i


class TestGatherTradeEntry:

    @pytest.mark.asyncio
    async def test_reads_own_messages_in_order_and_skips_bots(self, tmp_path):
        ch = review_channel([msg("checklist", is_bot=True), msg("First thought."),
                             msg(""), msg("Second thought.")])
        entry, _ = await gather_trade_entry(ch, tmp_path)
        assert "checklist" not in entry
        assert entry.index("First thought.") < entry.index("Second thought.")

    @pytest.mark.asyncio
    async def test_includes_reflection_form(self, tmp_path):
        folder = tmp_path / "trade_7_gc"
        folder.mkdir()
        (folder / "reflections.json").write_text(json.dumps({
            "why_entered": "CISD on the 5m", "how_felt": "Rushed, tight chest",
            "what_learned": "Wait", "reminds_me_of": None}))
        entry, _ = await gather_trade_entry(review_channel([msg("Notes.")]), tmp_path)
        assert "REFLECTION FORM" in entry
        assert "How I felt: Rushed, tight chest" in entry
        assert "reminded" not in entry  # empty answer left out, not rendered as None

    @pytest.mark.asyncio
    async def test_coachable_judged_on_own_words_not_labels(self, tmp_path):
        _, coachable = await gather_trade_entry(review_channel([msg("ES 5810")]), tmp_path)
        assert coachable is False
        _, coachable = await gather_trade_entry(review_channel([msg(SUBSTANTIVE)]), tmp_path)
        assert coachable is True

    @pytest.mark.asyncio
    async def test_reflection_alone_is_enough(self, tmp_path):
        folder = tmp_path / "trade_7_gc"
        folder.mkdir()
        (folder / "reflections.json").write_text(json.dumps({"how_felt": SUBSTANTIVE}))
        entry, coachable = await gather_trade_entry(review_channel([]), tmp_path)
        assert coachable is True
        assert "TRADE NOTES" not in entry

    @pytest.mark.asyncio
    async def test_long_channels_keep_the_latest_writing(self, tmp_path):
        ch = review_channel([msg("OLDEST " + "x" * MAX_REVIEW_CHARS), msg("NEWEST")])
        entry, _ = await gather_trade_entry(ch, tmp_path)
        assert "NEWEST" in entry and "OLDEST" not in entry
        assert len(entry) < MAX_REVIEW_CHARS + 100


class TestChannelReview:

    @pytest.mark.asyncio
    async def test_reviews_whole_channel_and_posts_gradeable_reading(self, cog, tmp_path):
        i = review_interaction(review_channel([msg(SUBSTANTIVE), msg("And it still stung.")]))
        with patch("cogs.coach.TRADES_DIR", tmp_path), \
             patch("cogs.coach.cs.coach", new=AsyncMock(return_value=FEEDBACK)) as c:
            await cog.review_channel(i)

        entry = c.call_args[0][0]
        assert SUBSTANTIVE in entry and "And it still stung." in entry
        i.response.defer.assert_awaited_once()
        kwargs = i.followup.send.call_args.kwargs
        assert kwargs["embed"].title == "🧠 Shull Reading"
        assert isinstance(kwargs["view"], GradeView)
        assert kwargs["view"].record["channel"] == "trade_7_gc"

    @pytest.mark.asyncio
    async def test_passes_logged_psych_state_as_context(self, cog, tmp_path):
        folder = tmp_path / "trade_7_gc"
        folder.mkdir()
        (folder / "psych.json").write_text(json.dumps({"fear_spectrum": 4}))
        i = review_interaction(review_channel([msg(SUBSTANTIVE)]))
        with patch("cogs.coach.TRADES_DIR", tmp_path), \
             patch("cogs.coach.cs.coach", new=AsyncMock(return_value=FEEDBACK)) as c:
            await cog.review_channel(i)
        assert "+4" in c.call_args.kwargs["context"]

    @pytest.mark.asyncio
    async def test_thin_channel_is_not_sent_to_the_model(self, cog, tmp_path):
        i = review_interaction(review_channel([msg("ES short")]))
        with patch("cogs.coach.TRADES_DIR", tmp_path), \
             patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.review_channel(i)
        c.assert_not_awaited()
        assert "Not enough written" in i.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_failure_is_reported_and_lock_released(self, cog, tmp_path):
        ch = review_channel([msg(SUBSTANTIVE)])
        i = review_interaction(ch)
        with patch("cogs.coach.TRADES_DIR", tmp_path), \
             patch("cogs.coach.cs.coach", new=AsyncMock(side_effect=cs.CoachError("timeout"))):
            await cog.review_channel(i)
        assert "timeout" in i.followup.send.call_args[0][0]
        assert ch.id not in cog._reviewing

    @pytest.mark.asyncio
    async def test_double_press_is_refused_while_running(self, cog):
        ch = review_channel([msg(SUBSTANTIVE)])
        cog._reviewing.add(ch.id)
        i = review_interaction(ch)
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.review_channel(i)
        c.assert_not_awaited()
        assert "already running" in i.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_refused_outside_journal_channels(self, cog):
        i = review_interaction(review_channel([msg(SUBSTANTIVE)], category="General"))
        with patch("cogs.coach.cs.coach", new=AsyncMock()) as c:
            await cog.shull_review.callback(cog, i)
        c.assert_not_awaited()
        assert "trade channel" in i.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_refused_when_corpus_broken(self):
        with patch("cogs.coach.cs.load_corpus", side_effect=cs.CoachError("bad corpus")):
            broken = Coach(MagicMock())
        i = review_interaction(review_channel([msg(SUBSTANTIVE)]))
        await broken.review_channel(i)
        assert "bad corpus" in i.response.send_message.call_args[0][0]
