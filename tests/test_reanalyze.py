"""
Tests for full re-analysis safety in cogs/trades.py.

A full re-run is long, touches production insights, and is exactly when three
latent problems bite, so each gets pinned here:
  - stale folders from renamed channels must not cause double analysis
  - an interrupted run must not lose insights the tracker already counted
  - two runs must never overlap and clobber master_insights.json
"""
import os
import json
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.trades import (
    Trades, ReanalyzeConfirmView, write_json_atomic, dedupe_insights,
)
from services.llm_service import LLMError


@pytest.fixture
def cog():
    bot = MagicMock()
    bot.loop = MagicMock()
    return Trades(bot)


@pytest.fixture
def interaction():
    i = AsyncMock()
    i.response = AsyncMock()
    i.followup = AsyncMock()
    i.followup.send = AsyncMock(return_value=AsyncMock())
    i.user = MagicMock()
    i.user.id = 42
    i.guild = MagicMock()
    return i


@pytest.fixture
def paths(tmp_path):
    p = {
        "trades": tmp_path / "Fractal_Trades",
        "insights": tmp_path / "master_insights.json",
        "tracker": tmp_path / "last_analyzed.txt",
        "failed": tmp_path / "failed_trades.json",
        "backups": tmp_path / "backups",
    }
    p["trades"].mkdir()
    with patch("cogs.trades.TRADES_DIR", p["trades"]), \
         patch("cogs.trades.INSIGHTS_PATH", p["insights"]), \
         patch("cogs.trades.TRACKER_PATH", p["tracker"]), \
         patch("cogs.trades.FAILED_TRADES_PATH", p["failed"]), \
         patch("cogs.trades.BACKUPS_DIR", p["backups"]):
        yield p


def write_history(trades_dir, channel, messages, mtime=None):
    d = trades_dir / channel
    d.mkdir(exist_ok=True)
    f = d / "history.json"
    f.write_text(json.dumps([
        {"message_id": str(mid), "content": text, "images": []} for mid, text in messages
    ]))
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return d


# ──────────────────────────────────────────────
# Stale folders → no double analysis
# ──────────────────────────────────────────────

class TestCollectTrades:

    @pytest.mark.asyncio
    async def test_renamed_channel_messages_counted_once(self, cog, paths):
        """The trade_80_rty / trade_80_rty_ym case."""
        write_history(paths["trades"], "trade_80_rty", [(800, "old copy")], mtime=1_000)
        write_history(paths["trades"], "trade_80_rty_ym", [(800, "live copy")], mtime=2_000)

        trades, duplicates = await cog._collect_trades(0)

        assert len(trades) == 1
        assert duplicates == 1

    @pytest.mark.asyncio
    async def test_newest_folder_wins(self, cog, paths):
        """The live channel's history is rewritten every sync; the stale one never is."""
        write_history(paths["trades"], "stale", [(800, "old")], mtime=1_000)
        live = write_history(paths["trades"], "live", [(800, "new")], mtime=2_000)

        trades, _ = await cog._collect_trades(0)

        assert trades[0]["_folder_path"] == str(live)
        assert trades[0]["content"] == "new"

    @pytest.mark.asyncio
    async def test_newest_wins_regardless_of_iteration_order(self, cog, paths):
        live = write_history(paths["trades"], "aaa_live", [(800, "new")], mtime=2_000)
        write_history(paths["trades"], "zzz_stale", [(800, "old")], mtime=1_000)
        trades, _ = await cog._collect_trades(0)
        assert trades[0]["_folder_path"] == str(live)

    @pytest.mark.asyncio
    async def test_respects_tracker(self, cog, paths):
        write_history(paths["trades"], "t1", [(100, "done"), (200, "new")])
        trades, _ = await cog._collect_trades(100)
        assert [t["message_id"] for t in trades] == ["200"]

    @pytest.mark.asyncio
    async def test_corrupt_history_skipped(self, cog, paths):
        write_history(paths["trades"], "good", [(1, "ok")])
        bad = paths["trades"] / "bad"
        bad.mkdir()
        (bad / "history.json").write_text("{not json")
        trades, _ = await cog._collect_trades(0)
        assert len(trades) == 1

    @pytest.mark.asyncio
    async def test_folders_without_history_ignored(self, cog, paths):
        (paths["trades"] / "fresh_channel").mkdir()
        (paths["trades"] / "fresh_channel" / "checklist_state.json").write_text("[]")
        trades, duplicates = await cog._collect_trades(0)
        assert trades == [] and duplicates == 0

    @pytest.mark.asyncio
    async def test_retries_deduped_against_fresh(self, cog, paths, interaction):
        write_history(paths["trades"], "t1", [(100, "fresh")])
        paths["failed"].write_text(json.dumps([
            {"message_id": "100", "content": "stale retry", "images": [], "_folder_path": "x"}]))

        await cog._do_analyze_trades(interaction)

        # The run is handed to create_task as a coroutine; read what it was queued with.
        coro = cog.bot.loop.create_task.call_args[0][0]
        queued = coro.cr_frame.f_locals["all_trades"]
        coro.close()
        assert [t["content"] for t in queued] == ["fresh"]


# ──────────────────────────────────────────────
# Single-run lock
# ──────────────────────────────────────────────

class TestRunLock:

    @pytest.mark.asyncio
    async def test_second_run_refused_while_busy(self, cog, paths, interaction):
        write_history(paths["trades"], "t1", [(1, "x")])
        cog._analysis_running = True

        await cog._do_analyze_trades(interaction)

        cog.bot.loop.create_task.assert_not_called()
        assert "already in progress" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_lock_taken_when_run_starts(self, cog, paths, interaction):
        write_history(paths["trades"], "t1", [(1, "x")])
        await cog._do_analyze_trades(interaction)
        assert cog._analysis_running is True
        cog.bot.loop.create_task.call_args[0][0].close()

    @pytest.mark.asyncio
    async def test_lock_released_after_success(self, cog, paths, interaction):
        trades = [{"message_id": "1", "content": "x", "images": [],
                   "_folder_path": str(paths["trades"])}]
        with patch("cogs.trades.llm_service.complete",
                   new=AsyncMock(return_value='{"good_habits": [], "mistakes": []}')):
            await cog.run_analysis_loop(interaction, trades, 0)
        assert cog._analysis_running is False

    @pytest.mark.asyncio
    async def test_lock_released_after_crash(self, cog, paths, interaction):
        trades = [{"message_id": "1", "content": "x", "images": [],
                   "_folder_path": str(paths["trades"])}]
        with patch("cogs.trades.llm_service.complete",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(RuntimeError):
                await cog.run_analysis_loop(interaction, trades, 0)
        assert cog._analysis_running is False


# ──────────────────────────────────────────────
# Interruption doesn't lose counted work
# ──────────────────────────────────────────────

class TestCrashSafety:

    @pytest.mark.asyncio
    async def test_insights_survive_interruption(self, cog, paths, interaction):
        """A restart mid-run (e.g. a deploy) must not lose finished trades."""
        trades = [
            {"message_id": "1", "content": "a", "images": [], "_folder_path": str(paths["trades"])},
            {"message_id": "2", "content": "b", "images": [], "_folder_path": str(paths["trades"])},
        ]
        complete = AsyncMock(side_effect=[
            '{"good_habits": ["Waited for C2"], "mistakes": []}',
            asyncio.CancelledError(),          # service restarted here
        ])
        with patch("cogs.trades.llm_service.complete", new=complete):
            with pytest.raises(asyncio.CancelledError):
                await cog.run_analysis_loop(interaction, trades, 0)

        assert "Waited for C2" in json.loads(paths["insights"].read_text())["good_habits"]
        # Tracker only covers what was actually saved, so a resume redoes trade 2.
        assert paths["tracker"].read_text() == "1"

    @pytest.mark.asyncio
    async def test_failed_call_still_advances(self, cog, paths, interaction):
        trades = [{"message_id": "5", "content": "a", "images": [],
                   "_folder_path": str(paths["trades"])}]
        with patch("cogs.trades.llm_service.complete",
                   new=AsyncMock(side_effect=LLMError("down"))):
            await cog.run_analysis_loop(interaction, trades, 0)
        assert paths["tracker"].read_text() == "5"
        assert paths["insights"].exists()

    @pytest.mark.asyncio
    async def test_resume_keeps_earlier_insights(self, cog, paths, interaction):
        paths["insights"].write_text(json.dumps({"good_habits": ["earlier"], "mistakes": []}))
        trades = [{"message_id": "9", "content": "a", "images": [],
                   "_folder_path": str(paths["trades"])}]
        with patch("cogs.trades.llm_service.complete",
                   new=AsyncMock(return_value='{"good_habits": ["later"], "mistakes": []}')):
            await cog.run_analysis_loop(interaction, trades, 8)
        habits = json.loads(paths["insights"].read_text())["good_habits"]
        assert habits == ["earlier", "later"]


class TestHelpers:

    @pytest.mark.asyncio
    async def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "x.json"
        await write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text()) == {"a": 1}
        assert not (tmp_path / "x.json.tmp").exists()

    @pytest.mark.asyncio
    async def test_atomic_write_replaces_existing(self, tmp_path):
        target = tmp_path / "x.json"
        target.write_text('{"old": true}')
        await write_json_atomic(target, {"new": True})
        assert json.loads(target.read_text()) == {"new": True}

    def test_dedupe_keeps_order(self):
        out = dedupe_insights({"good_habits": ["b", "a", "b"], "mistakes": ["x", "x"]})
        assert out == {"good_habits": ["b", "a"], "mistakes": ["x"]}

    def test_dedupe_fills_missing_keys(self):
        assert dedupe_insights({}) == {"good_habits": [], "mistakes": []}


# ──────────────────────────────────────────────
# /reanalyze_trades
# ──────────────────────────────────────────────

class TestReanalyzeCommand:

    @pytest.mark.asyncio
    async def test_refused_while_busy(self, cog, paths, interaction):
        cog._analysis_running = True
        cog._do_sync_fractals = AsyncMock()
        await cog.reanalyze_trades.callback(cog, interaction)
        cog._do_sync_fractals.assert_not_awaited()
        assert "already in progress" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_syncs_then_asks_for_confirmation(self, cog, paths, interaction):
        write_history(paths["trades"], "t1", [(1, "a"), (2, "b")])
        cog._do_sync_fractals = AsyncMock()

        await cog.reanalyze_trades.callback(cog, interaction)

        cog._do_sync_fractals.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert isinstance(kwargs["view"], ReanalyzeConfirmView)
        fields = {f.name: f.value for f in kwargs["embed"].fields}
        assert "**2** messages" in fields["What happens"]
        # Nothing destructive happens before the button is pressed.
        assert not paths["insights"].exists()
        cog.bot.loop.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_reports_stale_duplicates(self, cog, paths, interaction):
        write_history(paths["trades"], "old", [(1, "a")], mtime=1_000)
        write_history(paths["trades"], "new", [(1, "a")], mtime=2_000)
        cog._do_sync_fractals = AsyncMock()
        await cog.reanalyze_trades.callback(cog, interaction)
        fields = {f.name for f in interaction.followup.send.call_args.kwargs["embed"].fields}
        assert "🗂️ Stale folders" in fields

    @pytest.mark.asyncio
    async def test_nothing_to_analyse(self, cog, paths, interaction):
        cog._do_sync_fractals = AsyncMock()
        await cog.reanalyze_trades.callback(cog, interaction)
        assert "No trade messages" in interaction.followup.send.call_args[0][0]

    def test_is_admin_only(self, cog):
        perms = cog.reanalyze_trades.default_permissions
        assert perms is not None and perms.administrator


class TestStartReanalysis:

    @pytest.mark.asyncio
    async def test_backs_up_then_resets(self, cog, paths, interaction):
        paths["insights"].write_text(json.dumps({"good_habits": ["gemma insight"], "mistakes": []}))
        paths["tracker"].write_text("999")
        cog._do_analyze_trades = AsyncMock()

        await cog._start_reanalysis(interaction)

        backups = sorted(p.name for p in paths["backups"].iterdir())
        assert any(b.startswith("master_insights-") and b.endswith(".json") for b in backups)
        assert any(b.startswith("last_analyzed-") and b.endswith(".txt") for b in backups)
        insights_backup = next(paths["backups"].glob("master_insights-*.json"))
        assert "gemma insight" in insights_backup.read_text()

        assert json.loads(paths["insights"].read_text()) == {"good_habits": [], "mistakes": []}
        assert paths["tracker"].read_text() == "0"
        cog._do_analyze_trades.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_works_with_nothing_to_back_up(self, cog, paths, interaction):
        cog._do_analyze_trades = AsyncMock()
        await cog._start_reanalysis(interaction)
        assert paths["tracker"].read_text() == "0"
        assert "nothing to back up" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_refused_while_busy(self, cog, paths, interaction):
        paths["insights"].write_text('{"good_habits": ["keep me"], "mistakes": []}')
        cog._analysis_running = True
        cog._do_analyze_trades = AsyncMock()
        await cog._start_reanalysis(interaction)
        cog._do_analyze_trades.assert_not_awaited()
        assert "keep me" in paths["insights"].read_text()


class TestConfirmView:

    def _button(self, view, label):
        return next(c for c in view.children if getattr(c, "label", "") == label)

    @pytest.mark.asyncio
    async def test_other_users_cannot_confirm(self, cog):
        view = ReanalyzeConfirmView(cog, author_id=1)
        i = AsyncMock()
        i.user = MagicMock()
        i.user.id = 2
        assert await view.interaction_check(i) is False

    @pytest.mark.asyncio
    async def test_confirm_starts_reanalysis(self, cog):
        cog._start_reanalysis = AsyncMock()
        view = ReanalyzeConfirmView(cog, author_id=1)
        await self._button(view, "Re-analyse everything").callback(AsyncMock())
        cog._start_reanalysis.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_changes_nothing(self, cog):
        cog._start_reanalysis = AsyncMock()
        view = ReanalyzeConfirmView(cog, author_id=1)
        i = AsyncMock()
        await self._button(view, "Cancel").callback(i)
        cog._start_reanalysis.assert_not_awaited()
        assert "nothing was changed" in i.response.edit_message.call_args.kwargs["content"]


def test_deploy_protects_backups():
    wf = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"
    assert "--exclude 'backups/'" in wf.read_text()
