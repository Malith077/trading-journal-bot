"""
Tests for /rebuild_all and its supporting services.

The properties that matter for a destructive, scheduled command:
  - user-authored files survive; only derived files are deleted
  - nothing happens without both confirmations
  - DeepSeek calls never run during peak hours
  - a restart resumes the rebuild without repeating finished steps
  - the analysis locks are held throughout, and always released
"""
import os
import json
import asyncio
import datetime as dt
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from services import deepseek_hours as dh
from services import rebuild_service as rs

U = dt.timezone.utc
MON_0900 = dt.datetime(2026, 11, 23, 9, 0, tzinfo=U)    # peak
MON_1200 = dt.datetime(2026, 11, 23, 12, 0, tzinfo=U)   # off-peak
SAT_0700 = dt.datetime(2026, 11, 28, 7, 0, tzinfo=U)    # weekend


# ──────────────────────────────────────────────
# Peak hours
# ──────────────────────────────────────────────

class TestPeakHours:

    @pytest.mark.parametrize("hour,peak", [
        (0, False), (1, True), (3, True), (4, False), (5, False),
        (6, True), (9, True), (10, False), (23, False),
    ])
    def test_weekday_windows(self, hour, peak):
        assert dh.is_peak(MON_0900.replace(hour=hour)) is peak

    def test_weekends_never_peak(self):
        for day in (28, 29):  # Sat, Sun
            for hour in range(24):
                assert not dh.is_peak(dt.datetime(2026, 11, day, hour, tzinfo=U))

    def test_non_utc_input_is_converted(self):
        melbourne = dt.timezone(dt.timedelta(hours=11))
        # 20:00 Melbourne == 09:00 UTC → peak
        assert dh.is_peak(dt.datetime(2026, 11, 23, 20, 0, tzinfo=melbourne))

    def test_next_off_peak(self):
        assert dh.next_off_peak(MON_0900) == MON_0900.replace(hour=10)
        assert dh.next_off_peak(MON_0900.replace(hour=2, minute=30)) == MON_0900.replace(hour=4, minute=0)
        assert dh.next_off_peak(MON_1200) == MON_1200

    def test_next_off_peak_is_never_peak(self):
        t = dt.datetime(2026, 11, 23, tzinfo=U)
        for _ in range(24 * 7 * 4):
            assert not dh.is_peak(dh.next_off_peak(t))
            t += dt.timedelta(minutes=15)

    def test_discord_time(self):
        assert dh.discord_time(MON_1200, "R") == f"<t:{int(MON_1200.timestamp())}:R>"

    @pytest.mark.asyncio
    async def test_wait_returns_immediately_off_peak(self):
        sleep = AsyncMock()
        assert await dh.wait_for_off_peak(now=lambda: MON_1200, sleep=sleep) is False
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wait_sleeps_through_peak_and_reports_once(self):
        clock = {"t": MON_0900}

        async def sleep(seconds):
            clock["t"] += dt.timedelta(seconds=seconds)

        on_pause = AsyncMock()
        waited = await dh.wait_for_off_peak(now=lambda: clock["t"], sleep=sleep,
                                            on_pause=on_pause, max_chunk=600)
        assert waited is True
        assert not dh.is_peak(clock["t"])
        on_pause.assert_awaited_once_with(MON_0900.replace(hour=10))


# ──────────────────────────────────────────────
# File safety
# ──────────────────────────────────────────────

USER_FILES = ["reflections.json", "psych.json", "outcome.json", "checklist_state.json",
              "review.json", "my_notes.txt", "chart.png"]   # last two: unknown/hand-placed files


def make_folder(base: Path, name: str, message_ids=(1,)):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "history.json").write_text(json.dumps([{"message_id": str(m)} for m in message_ids]))
    (d / f"{message_ids[0]}_screenshot.PNG").write_bytes(b"img")
    (d / f"{message_ids[0]}_other.webp").write_bytes(b"img")
    for f in USER_FILES:
        (d / f).write_text("user data")
    return d


class TestRebuildService:

    def test_allowlist(self):
        assert rs.is_derived(Path("history.json"))
        assert rs.is_derived(Path("1234_chart.png"))
        assert rs.is_derived(Path("1234_chart.JPEG"))
        for name in USER_FILES:
            assert not rs.is_derived(Path(name)), name
        assert not rs.is_derived(Path("1234_notes.txt"))   # right prefix, wrong type

    def test_wipe_deletes_only_derived_files_in_live_folders(self, tmp_path):
        live = make_folder(tmp_path, "trade_1_gc")
        removed = rs.wipe_derived(tmp_path, {"trade_1_gc"})
        assert removed == 3
        assert sorted(p.name for p in live.iterdir()) == sorted(USER_FILES)

    def test_wipe_never_touches_orphaned_folders(self, tmp_path):
        """A deleted channel's folder is the only copy of that trade."""
        orphan = make_folder(tmp_path, "trade_80_rty")
        before = sorted(p.name for p in orphan.iterdir())
        rs.wipe_derived(tmp_path, {"trade_80_rty_ym"})
        assert sorted(p.name for p in orphan.iterdir()) == before

    def test_wipe_missing_dir(self, tmp_path):
        assert rs.wipe_derived(tmp_path / "nope", {"x"}) == 0

    def test_backup_contents(self, tmp_path):
        downloads = tmp_path / "Fractal_Trades"
        make_folder(downloads, "trade_1_gc", (1, 2))
        make_folder(downloads, "trade_9_old", (9,))
        kb = tmp_path / "knowledge_base"
        kb.mkdir()
        (kb / "cisd_article.md").write_text("# CISD")
        insights = tmp_path / "master_insights.json"
        insights.write_text('{"good_habits": ["x"]}')

        dest = rs.backup(tmp_path / "backups" / "rebuild-1", [insights, tmp_path / "missing.txt"],
                         kb, [downloads])

        assert (dest / "master_insights.json").read_text() == '{"good_habits": ["x"]}'
        assert (dest / "knowledge_base" / "cisd_article.md").exists()
        assert (dest / "downloads" / "Fractal_Trades" / "trade_1_gc" / "history.json").exists()
        assert (dest / "downloads" / "Fractal_Trades" / "trade_9_old" / "history.json").exists()
        assert not list(dest.rglob("*.png")), "charts are re-downloadable and not backed up"

    def test_backup_refuses_to_overwrite(self, tmp_path):
        rs.backup(tmp_path / "b", [], tmp_path / "nokb", [])
        with pytest.raises(FileExistsError):
            rs.backup(tmp_path / "b", [], tmp_path / "nokb", [])

    def test_count_messages_unique(self, tmp_path):
        make_folder(tmp_path, "a", (1, 2))
        make_folder(tmp_path, "b", (2, 3))          # 2 duplicated by a renamed channel
        (tmp_path / "c").mkdir()
        (tmp_path / "c" / "history.json").write_text("{broken")
        assert rs.count_messages(tmp_path) == 3

    def test_classify_and_preserved(self, tmp_path):
        make_folder(tmp_path, "live")
        make_folder(tmp_path, "gone")
        live, orphan = rs.classify_folders(tmp_path, {"live"})
        assert [p.name for p in live] == ["live"] and [p.name for p in orphan] == ["gone"]
        assert rs.count_preserved([tmp_path]) == len(USER_FILES) * 2

    def test_read_tracker(self, tmp_path):
        assert rs.read_tracker(tmp_path / "none") == 0
        (tmp_path / "t").write_text(" 42\n")
        assert rs.read_tracker(tmp_path / "t") == 42
        (tmp_path / "t").write_text("junk")
        assert rs.read_tracker(tmp_path / "t") == 0


# ──────────────────────────────────────────────
# The cog
# ──────────────────────────────────────────────

def _channel(name, cid=None):
    c = MagicMock()
    c.name = name
    c.id = cid or abs(hash(name)) % 10_000
    c.send = AsyncMock()
    return c


@pytest.fixture
def env(tmp_path):
    from cogs import maintenance as m
    p = {
        "trades": tmp_path / "downloads" / "Fractal_Trades",
        "missed": tmp_path / "downloads" / "Missed_Trades",
        "kb": tmp_path / "knowledge_base",
        "backups": tmp_path / "backups",
        "insights": tmp_path / "master_insights.json",
        "tracker": tmp_path / "last_analyzed.txt",
        "m_insights": tmp_path / "missed_insights.json",
        "m_tracker": tmp_path / "last_missed_analyzed.txt",
        "state": tmp_path / "rebuild_state.json",
        "failed": tmp_path / "failed_trades.json",
        "m_failed": tmp_path / "failed_missed.json",
    }
    for d in ("trades", "missed", "kb"):
        p[d].mkdir(parents=True)
    with patch.multiple(m, TRADES_DIR=p["trades"], MISSED_DIR=p["missed"], KB_DIR=p["kb"],
                        BACKUPS_DIR=p["backups"], INSIGHTS_PATH=p["insights"], TRACKER_PATH=p["tracker"],
                        MISSED_INSIGHTS_PATH=p["m_insights"], MISSED_TRACKER_PATH=p["m_tracker"],
                        REBUILD_STATE_PATH=p["state"], flush_and_reindex=MagicMock()), \
         patch("cogs.knowledge.KB_DIR", p["kb"]), \
         patch("cogs.trades.FAILED_TRADES_PATH", p["failed"]), \
         patch("cogs.missed.FAILED_MISSED_PATH", p["m_failed"]):
        yield p


@pytest.fixture
def world():
    """A bot with the three cogs the rebuild drives, all mocked."""
    trade_chan = _channel("trade_1_gc", 11)
    missed_chan = _channel("missed_1_es", 12)
    cisd_chan = _channel("cisd", 13)
    command_chan = _channel("general", 99)

    guild = MagicMock()
    guild.id = 1
    guild.text_channels = [trade_chan, missed_chan, cisd_chan, command_chan]

    trades = MagicMock()
    trades._analysis_running = False
    cat = MagicMock(); cat.text_channels = [trade_chan]
    trades._get_trade_categories = MagicMock(return_value=[cat])
    trades._do_sync_fractals = AsyncMock()
    trades._collect_trades = AsyncMock(return_value=([{"message_id": "5"}], 0))
    trades._run_analysis = AsyncMock()

    missed = MagicMock()
    missed._analysis_running = False
    mcat = MagicMock(); mcat.text_channels = [missed_chan]
    missed._get_missed_categories = MagicMock(return_value=[mcat])
    missed._do_sync_missed = AsyncMock()
    missed._collect_missed = AsyncMock(return_value=([], 0))
    missed._run_analysis = AsyncMock()

    knowledge = MagicMock()
    knowledge.generate_article = AsyncMock(return_value=(Path("x"), 0))

    bot = MagicMock()
    bot.get_cog = lambda name: {"Trades": trades, "Missed": missed, "Knowledge": knowledge}.get(name)
    bot.get_guild = lambda gid: guild if gid == 1 else None
    bot.get_channel = lambda cid: command_chan if cid == 99 else None

    return MagicMock(bot=bot, guild=guild, trades=trades, missed=missed, knowledge=knowledge,
                     channel=command_chan, cisd=cisd_chan)


@pytest.fixture
def cog(world):
    from cogs.maintenance import Maintenance
    c = Maintenance(world.bot)
    clock = {"t": MON_1200}

    async def sleep(seconds):
        clock["t"] += dt.timedelta(seconds=seconds)

    c._now = lambda: clock["t"]
    c._sleep = sleep
    c.clock = clock
    return c


def _state(status="scheduled", step=0, **extra):
    s = {"status": status, "guild_id": 1, "channel_id": 99, "requested_by": "malith",
         "created_at": MON_1200.isoformat(), "scheduled_for": MON_1200.isoformat(),
         "started_at": None, "step": step, "backup_dir": None, "report": {}}
    s.update(extra)
    return s


def _messages(channel):
    return " | ".join(str(c.args[0]) if c.args else str(c.kwargs) for c in channel.send.call_args_list)


class TestPlanning:

    def test_plan_counts(self, cog, env, world):
        make_folder(env["trades"], "trade_1_gc", (1, 2))
        make_folder(env["trades"], "trade_0_deleted", (7,))
        (env["kb"] / "cisd_article.md").write_text("x")
        (env["kb"] / "deleted_channel_article.md").write_text("x")
        (env["kb"] / "handwritten.md").write_text("x")

        plan = cog.build_plan(world.guild)

        assert plan["live_folders"] == 1
        assert plan["orphan_folders"] == 1
        assert plan["messages"] == 3
        assert plan["regenerate"] == ["cisd_article.md"]
        assert sorted(plan["keep"]) == ["deleted_channel_article.md", "handwritten.md"]
        assert plan["start_now"] is True

    def test_plan_during_peak_starts_later(self, cog, env, world):
        cog.clock["t"] = MON_0900
        plan = cog.build_plan(world.guild)
        assert plan["start_now"] is False
        assert plan["start"] == MON_0900.replace(hour=10)
        when = next(f.value for f in cog.plan_embed(plan).fields if f.name == "⏱️ When")
        assert "peak hours until then" in when

    def test_is_admin_only(self, cog):
        assert cog.rebuild_all.default_permissions.administrator


class TestConfirmation:

    def _interaction(self, user_id=42):
        i = AsyncMock()
        i.user = MagicMock(); i.user.id = user_id; i.user.mention = "@malith"
        i.user.__str__ = lambda self: "malith"
        i.guild = MagicMock(); i.guild.id = 1
        i.channel = _channel("general", 99)
        return i

    @pytest.mark.asyncio
    async def test_command_shows_plan_and_does_nothing(self, cog, env, world):
        i = self._interaction()
        await cog.rebuild_all.callback(cog, i)
        from cogs.maintenance import RebuildConfirmView
        assert isinstance(i.followup.send.call_args.kwargs["view"], RebuildConfirmView)
        assert not env["state"].exists()

    @pytest.mark.asyncio
    async def test_only_invoker_can_continue(self, cog):
        from cogs.maintenance import RebuildConfirmView
        view = RebuildConfirmView(cog, author_id=1)
        assert await view.interaction_check(self._interaction(user_id=2)) is False

    @pytest.mark.asyncio
    async def test_continue_opens_phrase_modal(self, cog):
        from cogs.maintenance import RebuildConfirmView, RebuildPhraseModal
        view = RebuildConfirmView(cog, author_id=42)
        i = self._interaction()
        button = next(c for c in view.children if c.label.startswith("Continue"))
        await button.callback(i)
        assert isinstance(i.response.send_modal.call_args[0][0], RebuildPhraseModal)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("typed", ["", "rebuild", "REBUILD please", "yes"])
    async def test_wrong_phrase_schedules_nothing(self, cog, env, typed):
        from cogs.maintenance import RebuildPhraseModal
        modal = RebuildPhraseModal(cog)
        modal.phrase._value = typed
        cog._launch = MagicMock()
        await modal.on_submit(self._interaction())
        assert not env["state"].exists()
        cog._launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_correct_phrase_schedules(self, cog, env):
        from cogs.maintenance import RebuildPhraseModal
        modal = RebuildPhraseModal(cog)
        modal.phrase._value = "  REBUILD "
        cog._launch = MagicMock()
        i = self._interaction()
        await modal.on_submit(i)

        state = json.loads(env["state"].read_text())
        assert state["status"] == "scheduled"
        assert state["channel_id"] == 99
        cog._launch.assert_called_once()
        assert "Full rebuild scheduled" in i.channel.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_scheduling_during_peak_waits_for_off_peak(self, cog, env):
        from cogs.maintenance import RebuildPhraseModal
        cog.clock["t"] = MON_0900
        cog._launch = MagicMock()
        modal = RebuildPhraseModal(cog)
        modal.phrase._value = "REBUILD"
        await modal.on_submit(self._interaction())
        state = json.loads(env["state"].read_text())
        assert dt.datetime.fromisoformat(state["scheduled_for"]) == MON_0900.replace(hour=10)

    @pytest.mark.asyncio
    async def test_cannot_double_schedule(self, cog, env):
        env["state"].write_text(json.dumps(_state()))
        cog._launch = MagicMock()
        i = self._interaction()
        await cog.schedule(i)
        cog._launch.assert_not_called()
        assert "already scheduled" in i.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_existing_state_shows_status_not_plan(self, cog, env):
        env["state"].write_text(json.dumps(_state()))
        i = self._interaction()
        await cog.rebuild_all.callback(cog, i)
        assert "scheduled" in i.followup.send.call_args.kwargs["embed"].title.lower()


class TestRun:

    @pytest.mark.asyncio
    async def test_full_rebuild(self, cog, env, world):
        trade = make_folder(env["trades"], "trade_1_gc")
        orphan = make_folder(env["trades"], "trade_0_deleted")
        env["insights"].write_text('{"good_habits": ["gemma"], "mistakes": []}')
        env["tracker"].write_text("999")
        old_article = env["kb"] / "cisd_article.md"
        old_article.write_text("old")
        os.utime(old_article, (MON_1200.timestamp() - 3600,) * 2)

        locks_during = {}

        async def record_locks(*a, **k):
            locks_during["trades"] = world.trades._analysis_running
            locks_during["missed"] = world.missed._analysis_running
        world.trades._run_analysis.side_effect = record_locks

        await cog._run(_state())

        # Backup taken, insights reset
        backup = next(env["backups"].iterdir())
        assert "gemma" in (backup / "master_insights.json").read_text()
        assert json.loads(env["insights"].read_text()) == {"good_habits": [], "mistakes": []}
        assert env["tracker"].read_text() == "0"
        # User data intact; orphan untouched; derived files gone from the live folder
        assert sorted(p.name for p in trade.iterdir()) == sorted(USER_FILES)
        assert (orphan / "history.json").exists()
        # Every stage ran
        world.trades._do_sync_fractals.assert_awaited_once()
        world.missed._do_sync_missed.assert_awaited_once()
        world.knowledge.generate_article.assert_awaited_once_with(world.cisd)
        from cogs import maintenance as m
        m.flush_and_reindex.assert_called_once()
        world.trades._run_analysis.assert_awaited_once()
        # Locks held during analysis, released after
        assert locks_during == {"trades": True, "missed": True}
        assert world.trades._analysis_running is False and world.missed._analysis_running is False
        # Done: state cleared, summary posted
        assert not env["state"].exists()
        assert "Rebuild complete" in world.channel.send.call_args.kwargs["embed"].title

    @pytest.mark.asyncio
    async def test_waits_for_scheduled_time_then_off_peak(self, cog, env, world):
        cog.clock["t"] = MON_0900 - dt.timedelta(hours=1)        # 08:00, scheduled for 10:00
        await cog._run(_state(scheduled_for=MON_0900.replace(hour=10).isoformat()))
        assert cog.clock["t"] >= MON_0900.replace(hour=10)
        world.trades._do_sync_fractals.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pauses_for_peak_mid_run(self, cog, env, world):
        """Starts off-peak; peak begins before the analysis step; it pauses rather than paying peak rates."""
        cog.clock["t"] = MON_0900.replace(hour=5, minute=59)

        async def advance_into_peak(*a, **k):
            cog.clock["t"] = MON_0900.replace(hour=6, minute=30)
        world.trades._do_sync_fractals.side_effect = advance_into_peak

        seen = {}

        async def run_analysis(reporter, items, last_id, before_each):
            await before_each()
            seen["time"] = cog.clock["t"]
        world.trades._run_analysis.side_effect = run_analysis

        await cog._run(_state(scheduled_for=MON_0900.replace(hour=5).isoformat()))

        assert not dh.is_peak(seen["time"])
        assert "pausing until" in _messages(world.channel)

    @pytest.mark.asyncio
    async def test_waits_for_running_analysis_to_finish(self, cog, env, world):
        world.trades._analysis_running = True

        async def sleep(seconds):
            cog.clock["t"] += dt.timedelta(seconds=seconds)
            world.trades._analysis_running = False   # ordinary run finishes
        cog._sleep = sleep

        await cog._run(_state())
        world.trades._do_sync_fractals.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_is_recorded_and_locks_released(self, cog, env, world):
        world.trades._do_sync_fractals.side_effect = RuntimeError("discord down")
        await cog._run(_state())

        state = json.loads(env["state"].read_text())
        assert state["status"] == "failed"
        assert state["step"] == 2          # backup + wipe done, failed in sync
        assert "discord down" in state["error"]
        assert world.trades._analysis_running is False and world.missed._analysis_running is False
        assert "Rebuild failed" in _messages(world.channel)

    @pytest.mark.asyncio
    async def test_resume_skips_finished_steps(self, cog, env, world):
        """After a restart mid-analysis, the wipe must NOT run again."""
        env["insights"].write_text('{"good_habits": ["already analysed"], "mistakes": []}')
        env["tracker"].write_text("500")
        state = _state(status="running", step=5, started_at=MON_1200.isoformat(),
                       backup_dir=str(env["backups"] / "rebuild-x"))

        await cog._run(state, resumed=True)

        assert "already analysed" in env["insights"].read_text()
        assert env["tracker"].read_text() == "500"
        world.trades._do_sync_fractals.assert_not_awaited()
        assert world.trades._collect_trades.await_args[0][0] == 500
        assert "Resuming the rebuild at step 6" in _messages(world.channel)

    @pytest.mark.asyncio
    async def test_articles_resume_skips_already_regenerated(self, cog, env, world):
        article = env["kb"] / "cisd_article.md"
        article.write_text("new")
        os.utime(article, (MON_1200.timestamp() + 60,) * 2)   # written after the run started
        state = _state(status="running", step=3, started_at=MON_1200.isoformat())
        await cog._run(state, resumed=True)
        world.knowledge.generate_article.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_article_keeps_old_version(self, cog, env, world):
        from services.llm_service import LLMError
        article = env["kb"] / "cisd_article.md"
        article.write_text("old but good")
        os.utime(article, (MON_1200.timestamp() - 60,) * 2)
        world.knowledge.generate_article.side_effect = LLMError("timeout")
        await cog._run(_state())
        assert article.read_text() == "old but good"
        assert "failed (old version kept)" in _messages(world.channel)
        assert not env["state"].exists()       # the rebuild still completes

    @pytest.mark.asyncio
    async def test_missing_guild_fails_cleanly(self, cog, env, world):
        await cog._run(_state(guild_id=404))
        assert json.loads(env["state"].read_text())["status"] == "failed"


class TestRestartAndStatusActions:

    @pytest.mark.asyncio
    async def test_on_ready_resumes_running(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="running", step=3)))
        cog._launch = MagicMock()
        await cog.on_ready()
        await cog.on_ready()                     # reconnects must not relaunch
        cog._launch.assert_called_once()
        assert cog._launch.call_args.kwargs["resumed"] is True

    @pytest.mark.asyncio
    async def test_on_ready_reschedules_scheduled(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="scheduled")))
        cog._launch = MagicMock()
        await cog.on_ready()
        cog._launch.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ready_leaves_failed_alone(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="failed")))
        cog._launch = MagicMock()
        await cog.on_ready()
        cog._launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_corrupt_state_ignored(self, cog, env):
        env["state"].write_text("{nope")
        cog._launch = MagicMock()
        await cog.on_ready()
        cog._launch.assert_not_called()

    def _admin(self, admin=True):
        i = AsyncMock()
        i.user = MagicMock(); i.user.mention = "@malith"
        i.user.guild_permissions.administrator = admin
        i.channel = _channel("general", 99)
        return i

    @pytest.mark.asyncio
    async def test_cancel_scheduled(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="scheduled")))
        task = MagicMock(); task.done.return_value = False
        cog._task = task
        await cog.cancel(self._admin())
        task.cancel.assert_called_once()
        assert not env["state"].exists()

    @pytest.mark.asyncio
    async def test_cannot_cancel_once_running(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="running", step=2)))
        i = self._admin()
        await cog.cancel(i)
        assert env["state"].exists()
        assert "already started" in i.response.send_message.call_args[0][0]

    @pytest.mark.asyncio
    async def test_resume_failed(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="failed", step=4, error="x")))
        cog._launch = MagicMock()
        await cog.resume(self._admin())
        state = json.loads(env["state"].read_text())
        assert state["status"] == "running" and "error" not in state
        cog._launch.assert_called_once()

    @pytest.mark.asyncio
    async def test_discard_keeps_backup_path_in_message(self, cog, env):
        env["state"].write_text(json.dumps(_state(status="failed", backup_dir="backups/rebuild-1")))
        i = self._admin()
        await cog.discard(i)
        assert not env["state"].exists()
        assert "backups/rebuild-1" in i.response.edit_message.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_status_view_requires_admin(self, cog):
        from cogs.maintenance import StatusView
        view = StatusView(cog, _state(status="scheduled"))
        assert await view.interaction_check(self._admin(admin=False)) is False
        assert await view.interaction_check(self._admin(admin=True)) is True

    def test_status_view_buttons(self, cog):
        from cogs.maintenance import StatusView
        assert [c.label for c in StatusView(cog, _state("scheduled")).children] == ["Cancel rebuild"]
        assert [c.label for c in StatusView(cog, _state("failed")).children] == ["Resume", "Discard"]
        assert StatusView(cog, _state("running", step=2)).children == []


def test_deploy_protects_rebuild_state():
    wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml").read_text()
    assert "--exclude 'rebuild_state.json'" in wf
    assert "--exclude 'backups/'" in wf
