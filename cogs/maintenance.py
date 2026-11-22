"""
/rebuild_all — the occasional "start over" command.

Deletes everything the bot derived from Discord and rebuilds it: trade and
missed-trade insights, downloaded history and charts, knowledge base articles,
and the vector index. User-authored data is never touched (see
services/rebuild_service.py for how that's enforced).

Safety properties, each of which has a test:
  * Two confirmations: a button, then typing the phrase REBUILD.
  * Everything that changes is backed up first, to backups/rebuild-<time>/.
  * It only spends money off-peak: it starts at the next off-peak moment and
    pauses mid-run if DeepSeek's peak hours begin.
  * It survives restarts. Schedule and progress live in rebuild_state.json, so
    a deploy (which restarts the bot) resumes the rebuild rather than dropping
    it, and finished steps are never repeated.
  * It holds the analysis locks throughout, so auto-sync can't start a
    competing analysis halfway through.
"""
import os
import json
import asyncio
import datetime as dt

import discord
from discord import app_commands, ui
from discord.ext import commands

import cogs.trades as trades_mod
import cogs.missed as missed_mod
from cogs.knowledge import source_channel_name
from cogs.missed import empty_missed_insights
from cogs.trades import EST_COST_PER_MESSAGE
from config import (
    TRADES_DIR, MISSED_DIR, KB_DIR, BACKUPS_DIR,
    INSIGHTS_PATH, TRACKER_PATH, MISSED_INSIGHTS_PATH, MISSED_TRACKER_PATH,
    REBUILD_STATE_PATH,
)
from scripts.reindex_all import flush_and_reindex
from services import deepseek_hours as dh
from services import rebuild_service as rs
from services.json_utils import write_json_atomic
from services.llm_service import llm_service, LLMError

CONFIRM_PHRASE = "REBUILD"
# Rough per-article cost: up to 500 messages + 10 charts, default reasoning.
EST_COST_PER_ARTICLE = 0.01
IDLE_POLL_SECONDS = 30

STEPS = [
    ("backup", "Backing up current data"),
    ("wipe", "Clearing derived data"),
    ("sync", "Re-syncing every channel from Discord"),
    ("articles", "Regenerating knowledge base articles"),
    ("reindex", "Rebuilding the vector index"),
    ("trades", "Re-analysing trades"),
    ("missed", "Re-analysing missed trades"),
]


# ──────────────────────────────────────────────
# Persisted state
# ──────────────────────────────────────────────

def load_state() -> dict | None:
    try:
        data = json.loads(REBUILD_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ Ignoring unreadable {REBUILD_STATE_PATH.name}: {e}")
        return None


def save_state(state: dict) -> None:
    tmp = REBUILD_STATE_PATH.with_name(REBUILD_STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, REBUILD_STATE_PATH)


def clear_state() -> None:
    REBUILD_STATE_PATH.unlink(missing_ok=True)


# ──────────────────────────────────────────────
# Reporting into a channel long after any interaction expired
# ──────────────────────────────────────────────

class _NullMessage:
    async def edit(self, *args, **kwargs):
        pass


class ChannelReporter:
    """
    Interaction-shaped adapter for the analysis loops, which report through
    `interaction.followup.send`. Interactions expire after 15 minutes; a
    scheduled rebuild runs hours later, so it reports to the channel instead.
    """

    def __init__(self, channel, guild):
        self._channel = channel
        self.guild = guild
        self.followup = self

    async def send(self, *args, **kwargs):
        kwargs.pop("ephemeral", None)
        if self._channel is None:
            print("[rebuild]", *args)
            return _NullMessage()
        return await self._channel.send(*args, **kwargs)


# ──────────────────────────────────────────────
# Confirmation UI
# ──────────────────────────────────────────────

class RebuildPhraseModal(ui.Modal, title="Confirm full rebuild"):
    """Second confirmation: the phrase has to be typed, not clicked."""

    phrase = ui.TextInput(
        label=f"Type {CONFIRM_PHRASE} to confirm",
        placeholder=CONFIRM_PHRASE,
        max_length=20,
    )

    def __init__(self, cog: "Maintenance"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if self.phrase.value.strip() != CONFIRM_PHRASE:
            await interaction.response.send_message(
                f"That wasn't `{CONFIRM_PHRASE}`, so nothing was scheduled.", ephemeral=True)
            return
        await self.cog.schedule(interaction)


class RebuildConfirmView(ui.View):
    """First confirmation. Only the person who ran the command can press it."""

    def __init__(self, cog: "Maintenance", author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/rebuild_all` can confirm it.", ephemeral=True)
            return False
        return True

    @ui.button(label="Continue…", style=discord.ButtonStyle.danger)
    async def proceed(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RebuildPhraseModal(self.cog))

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — nothing was changed.", view=self)
        self.stop()


class StatusView(ui.View):
    """Cancel a scheduled rebuild, or resume/discard a failed one. Admins only."""

    def __init__(self, cog: "Maintenance", state: dict):
        super().__init__(timeout=300)
        self.cog = cog
        if state.get("status") == "scheduled":
            self._add("Cancel rebuild", discord.ButtonStyle.danger, cog.cancel)
        elif state.get("status") == "failed":
            self._add("Resume", discord.ButtonStyle.primary, cog.resume)
            self._add("Discard", discord.ButtonStyle.secondary, cog.discard)

    def _add(self, label, style, handler):
        button = ui.Button(label=label, style=style)

        async def callback(interaction: discord.Interaction):
            await handler(interaction)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.administrator:
            await interaction.response.send_message("Only server admins can change a rebuild.", ephemeral=True)
            return False
        return True


# ──────────────────────────────────────────────
# The cog
# ──────────────────────────────────────────────

class Maintenance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._resumed_on_start = False
        # Injectable for tests.
        self._now = dh.utcnow
        self._sleep = asyncio.sleep

    def _cogs(self):
        return self.bot.get_cog("Trades"), self.bot.get_cog("Missed"), self.bot.get_cog("Knowledge")

    def _launch(self, state: dict, resumed: bool = False) -> asyncio.Task:
        self._task = asyncio.create_task(self._run(state, resumed=resumed))
        return self._task

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready fires again after every reconnect; only resume once.
        if self._resumed_on_start:
            return
        self._resumed_on_start = True
        state = load_state()
        if state and state.get("status") in ("scheduled", "running"):
            print(f"♻️ Resuming {state['status']} rebuild from {REBUILD_STATE_PATH.name}")
            self._launch(state, resumed=state["status"] == "running")

    # --- planning ---

    def _live_channels(self, guild) -> tuple[set[str], set[str]]:
        trades, missed, _ = self._cogs()
        trade_live = {c.name for cat in trades._get_trade_categories(guild) for c in cat.text_channels}
        missed_live = {c.name for cat in missed._get_missed_categories(guild) for c in cat.text_channels}
        return trade_live, missed_live

    def build_plan(self, guild) -> dict:
        trade_live, missed_live = self._live_channels(guild)
        text_channels = {c.name for c in guild.text_channels}

        t_live, t_orphan = rs.classify_folders(TRADES_DIR, trade_live)
        m_live, m_orphan = rs.classify_folders(MISSED_DIR, missed_live)
        articles = sorted(KB_DIR.glob("*.md")) if KB_DIR.exists() else []
        regenerate = [a.name for a in articles if source_channel_name(a) in text_channels]
        keep = [a.name for a in articles if a.name not in regenerate]

        messages = rs.count_messages(TRADES_DIR) + rs.count_messages(MISSED_DIR)
        now = self._now()
        start = dh.next_off_peak(now)
        return {
            "live_folders": len(t_live) + len(m_live),
            "orphan_folders": len(t_orphan) + len(m_orphan),
            "preserved_files": rs.count_preserved([TRADES_DIR, MISSED_DIR]),
            "messages": messages,
            "regenerate": regenerate,
            "keep": keep,
            "cost": messages * EST_COST_PER_MESSAGE + len(regenerate) * EST_COST_PER_ARTICLE,
            "start_now": start <= now,
            "start": start,
        }

    def plan_embed(self, plan: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🧨 Rebuild everything from scratch?",
            description=(f"Deletes everything the bot derived from Discord and rebuilds it with "
                         f"`{llm_service.model_for(images=True)}`, outside DeepSeek's peak hours."),
            color=discord.Color.red(),
        )
        embed.add_field(name="🗑️ Deleted and rebuilt", value=(
            "• Trade and missed-trade insights, and their trackers\n"
            f"• `history.json` and downloaded charts in **{plan['live_folders']}** channel folder(s)\n"
            f"• **{len(plan['regenerate'])}** knowledge base article(s), regenerated from their channels\n"
            "• The vector index behind `/ask`"
        ), inline=False)
        embed.add_field(name="🛡️ Never touched", value=(
            f"• **{plan['preserved_files']}** file(s) you wrote: reflections, psych state, outcomes, "
            "missed-trade reviews, checklists\n"
            f"• **{plan['orphan_folders']}** folder(s) whose channel no longer exists — the only copy of those trades\n"
            f"• **{len(plan['keep'])}** article(s) with no source channel to rebuild from\n"
            "• Psych and coach logs"
        ), inline=False)
        embed.add_field(name="💾 Backup", value=(
            "Insights, trackers, every `history.json` and the whole knowledge base are copied to "
            "`backups/rebuild-<time>/` before anything is deleted."
        ), inline=False)
        if plan["start_now"]:
            when = ("Starts **now** — it's off-peak. If peak hours begin while it's running, "
                    "it pauses and carries on afterwards.")
        else:
            when = (f"Starts {dh.discord_time(plan['start'])} ({dh.discord_time(plan['start'], 'R')}). "
                    "DeepSeek is in peak hours until then.")
        embed.add_field(name="⏱️ When", value=when, inline=False)
        embed.add_field(name="💲 Rough cost", value=(
            f"~${plan['cost']:.2f} at off-peak prices — about {plan['messages']} messages and "
            f"{len(plan['regenerate'])} articles, based on the last sync."
        ), inline=False)
        embed.set_footer(text=f"Two confirmations: press Continue, then type {CONFIRM_PHRASE}.")
        return embed

    def status_embed(self, state: dict) -> discord.Embed:
        status = state.get("status")
        step = state.get("step", 0)
        label = STEPS[step][1] if step < len(STEPS) else "Finishing"
        if status == "scheduled":
            start = dt.datetime.fromisoformat(state["scheduled_for"])
            return discord.Embed(
                title="🗓️ Rebuild scheduled",
                description=(f"Starts {dh.discord_time(start)} ({dh.discord_time(start, 'R')}). "
                             f"Requested by {state.get('requested_by')}."),
                color=discord.Color.orange())
        if status == "failed":
            return discord.Embed(
                title="❌ Rebuild failed",
                description=(f"Stopped at step {step + 1}/{len(STEPS)} ({label}): `{state.get('error')}`\n"
                             f"Backup: `{state.get('backup_dir') or 'not taken yet'}`\n\n"
                             "**Resume** carries on from that step. **Discard** forgets the rebuild and "
                             "leaves the data as it is now, partly rebuilt — the backup stays in `backups/`."),
                color=discord.Color.red())
        return discord.Embed(
            title="⚙️ Rebuild running",
            description=f"Step {min(step + 1, len(STEPS))}/{len(STEPS)}: {label}. Progress is posted in its channel.",
            color=discord.Color.blurple())

    # --- command ---

    @app_commands.command(name="rebuild_all",
                          description="Delete all derived data and rebuild trades and the knowledge base from scratch.")
    @app_commands.default_permissions(administrator=True)
    async def rebuild_all(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        state = load_state()
        if state:
            view = StatusView(self, state)
            kwargs = {"view": view} if view.children else {}
            await interaction.followup.send(embed=self.status_embed(state), ephemeral=True, **kwargs)
            return

        if not all(self._cogs()):
            await interaction.followup.send("❌ The trades, missed-trades and knowledge modules must all be loaded.",
                                            ephemeral=True)
            return

        plan = self.build_plan(interaction.guild)
        await interaction.followup.send(embed=self.plan_embed(plan),
                                        view=RebuildConfirmView(self, interaction.user.id), ephemeral=True)

    async def schedule(self, interaction: discord.Interaction):
        if load_state():
            await interaction.response.send_message(
                "A rebuild is already scheduled or running. Run `/rebuild_all` to see it.", ephemeral=True)
            return

        now = self._now()
        start = dh.next_off_peak(now)
        state = {
            "status": "scheduled",
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "requested_by": str(interaction.user),
            "created_at": now.isoformat(),
            "scheduled_for": start.isoformat(),
            "started_at": None,
            "step": 0,
            "backup_dir": None,
            "report": {},
        }
        save_state(state)

        when = "now" if start <= now else f"{dh.discord_time(start)} ({dh.discord_time(start, 'R')})"
        await interaction.response.send_message(f"✅ Confirmed. The rebuild starts {when}.", ephemeral=True)
        await interaction.channel.send(
            f"🧨 **Full rebuild scheduled** by {interaction.user.mention} — starts {when}. "
            "Progress will be posted here. `/rebuild_all` shows its status, and can cancel it before it starts.")
        self._launch(state)

    # --- status actions ---

    async def cancel(self, interaction: discord.Interaction):
        state = load_state()
        if not state or state.get("status") != "scheduled":
            await interaction.response.send_message(
                "It has already started, so it can't be cancelled now.", ephemeral=True)
            return
        if self._task and not self._task.done():
            self._task.cancel()
        clear_state()
        await interaction.response.edit_message(content="🛑 Rebuild cancelled — nothing was changed.",
                                                embed=None, view=None)
        await interaction.channel.send(f"🛑 Scheduled rebuild cancelled by {interaction.user.mention}.")

    async def resume(self, interaction: discord.Interaction):
        state = load_state()
        if not state or state.get("status") != "failed":
            await interaction.response.send_message("There's no failed rebuild to resume.", ephemeral=True)
            return
        state["status"] = "running"
        state.pop("error", None)
        save_state(state)
        await interaction.response.edit_message(content="▶️ Resuming the rebuild.", embed=None, view=None)
        self._launch(state, resumed=True)

    async def discard(self, interaction: discord.Interaction):
        state = load_state()
        clear_state()
        backup = (state or {}).get("backup_dir") or "none was taken"
        await interaction.response.edit_message(
            content=f"🗑️ Rebuild discarded. Backup: `{backup}`.", embed=None, view=None)

    # --- running ---

    async def _wait_until(self, target: dt.datetime):
        while (remaining := (target - self._now()).total_seconds()) > 0:
            await self._sleep(min(remaining, 300))

    async def _gate(self, reporter: ChannelReporter):
        """Hold DeepSeek calls until off-peak, saying so when it pauses."""
        async def on_pause(resume_at):
            await reporter.send(f"⏸️ DeepSeek peak hours — pausing until {dh.discord_time(resume_at, 't')} "
                                f"({dh.discord_time(resume_at, 'R')}).")
        if await dh.wait_for_off_peak(now=self._now, sleep=self._sleep, on_pause=on_pause):
            await reporter.send("▶️ Off-peak again — carrying on.")

    async def _wait_for_idle(self):
        """Let any ordinary analysis run finish before taking the locks."""
        trades, missed, _ = self._cogs()
        while trades._analysis_running or missed._analysis_running:
            await self._sleep(IDLE_POLL_SECONDS)

    def _set_locks(self, held: bool):
        trades, missed, _ = self._cogs()
        trades._analysis_running = held
        missed._analysis_running = held

    async def _run(self, state: dict, resumed: bool = False):
        channel = self.bot.get_channel(state["channel_id"])
        guild = self.bot.get_guild(state["guild_id"])
        reporter = ChannelReporter(channel, guild)
        locked = False
        try:
            if state["status"] == "scheduled":
                await self._wait_until(dt.datetime.fromisoformat(state["scheduled_for"]))
                await self._gate(reporter)
                await self._wait_for_idle()
                state["status"] = "running"
                state["started_at"] = self._now().isoformat()
                save_state(state)
                await reporter.send(f"🧨 **Full rebuild starting** — {len(STEPS)} steps. Progress will be posted here.")
            else:
                await self._wait_for_idle()
                if resumed:
                    await reporter.send(f"♻️ Resuming the rebuild at step {state['step'] + 1}/{len(STEPS)}.")

            if guild is None:
                raise RuntimeError(f"guild {state['guild_id']} isn't available to the bot")

            self._set_locks(True)
            locked = True
            for i in range(state["step"], len(STEPS)):
                key, label = STEPS[i]
                await reporter.send(f"**Step {i + 1}/{len(STEPS)}** — {label}…")
                await getattr(self, f"_step_{key}")(state, guild, reporter)
                state["step"] = i + 1
                save_state(state)

            await reporter.send(embed=self.summary_embed(state))
            clear_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state["status"] = "failed"
            state["error"] = f"{type(e).__name__}: {e}"[:500]
            save_state(state)
            step = min(state["step"], len(STEPS) - 1)
            await reporter.send(
                f"❌ **Rebuild failed** at step {step + 1}/{len(STEPS)} ({STEPS[step][1]}): `{state['error']}`\n"
                f"Backup: `{state.get('backup_dir') or 'not taken yet'}`. Run `/rebuild_all` to resume or discard it.")
        finally:
            if locked:
                self._set_locks(False)

    # --- steps (each safe to re-run after a restart) ---

    async def _step_backup(self, state, guild, reporter):
        if state.get("backup_dir"):
            return
        dest = BACKUPS_DIR / rs.backup_name(self._now())
        files = [INSIGHTS_PATH, TRACKER_PATH, MISSED_INSIGHTS_PATH, MISSED_TRACKER_PATH,
                 trades_mod.FAILED_TRADES_PATH, missed_mod.FAILED_MISSED_PATH]
        await asyncio.to_thread(rs.backup, dest, files, KB_DIR, [TRADES_DIR, MISSED_DIR])
        state["backup_dir"] = str(dest)
        save_state(state)
        await reporter.send(f"💾 Backed up to `backups/{dest.name}/`.")

    async def _step_wipe(self, state, guild, reporter):
        trade_live, missed_live = self._live_channels(guild)
        await write_json_atomic(INSIGHTS_PATH, {"good_habits": [], "mistakes": []})
        await write_json_atomic(MISSED_INSIGHTS_PATH, empty_missed_insights())
        TRACKER_PATH.write_text("0")
        MISSED_TRACKER_PATH.write_text("0")
        trades_mod.FAILED_TRADES_PATH.write_text("[]")
        missed_mod.FAILED_MISSED_PATH.write_text("[]")
        removed = rs.wipe_derived(TRADES_DIR, trade_live) + rs.wipe_derived(MISSED_DIR, missed_live)
        state["report"]["files_cleared"] = removed
        await reporter.send(f"🧹 Insights and trackers reset; {removed} downloaded file(s) cleared.")

    async def _step_sync(self, state, guild, reporter):
        trades, missed, _ = self._cogs()
        await trades._do_sync_fractals(guild, reporter.send)
        await missed._do_sync_missed(guild, reporter.send)

    async def _step_articles(self, state, guild, reporter):
        _, _, knowledge = self._cogs()
        report = state["report"].setdefault("articles", {"regenerated": 0, "failed": [], "kept": []})
        started = dt.datetime.fromisoformat(state["started_at"]).timestamp()
        channels = {c.name: c for c in guild.text_channels}
        articles = sorted(KB_DIR.glob("*.md")) if KB_DIR.exists() else []

        for article in articles:
            name = source_channel_name(article)
            channel = channels.get(name) if name else None
            if channel is None:
                if article.name not in report["kept"]:
                    report["kept"].append(article.name)
                continue
            if article.stat().st_mtime >= started:
                continue  # already regenerated before a restart
            await self._gate(reporter)
            try:
                result = await knowledge.generate_article(channel)
            except LLMError as e:
                print(f"[rebuild] article for #{name} failed: {e}")
                if article.name not in report["failed"]:
                    report["failed"].append(article.name)
                save_state(state)
                continue
            if result:
                report["regenerated"] += 1
            save_state(state)

        await reporter.send(
            f"📚 {report['regenerated']} article(s) regenerated"
            + (f", {len(report['failed'])} failed (old version kept)" if report["failed"] else "")
            + (f", {len(report['kept'])} kept with no source channel" if report["kept"] else "") + ".")

    async def _step_reindex(self, state, guild, reporter):
        await asyncio.to_thread(flush_and_reindex)
        await reporter.send("🔎 Vector index rebuilt.")

    async def _analyse(self, cog, base_dir, tracker_path, collect, label, reporter):
        if not base_dir.exists():
            await reporter.send(f"No {label} folder — skipping.")
            return
        last_id = rs.read_tracker(tracker_path)
        items, duplicates = await collect(last_id)
        items.sort(key=lambda x: int(x["message_id"]))
        if not items:
            await reporter.send(f"No {label} messages left to analyse.")
            return
        note = f" ({duplicates} duplicate(s) from stale folders skipped)" if duplicates else ""
        await reporter.send(f"🧪 {len(items)} {label} message(s) to analyse{note}.")
        await cog._run_analysis(reporter, items, last_id, before_each=lambda: self._gate(reporter))

    async def _step_trades(self, state, guild, reporter):
        trades, _, _ = self._cogs()
        await self._analyse(trades, TRADES_DIR, TRACKER_PATH, trades._collect_trades, "trade", reporter)

    async def _step_missed(self, state, guild, reporter):
        _, missed, _ = self._cogs()
        await self._analyse(missed, MISSED_DIR, MISSED_TRACKER_PATH, missed._collect_missed, "missed-trade", reporter)

    def summary_embed(self, state: dict) -> discord.Embed:
        report = state.get("report", {})
        articles = report.get("articles", {})
        embed = discord.Embed(title="✅ Rebuild complete", color=discord.Color.green())
        if state.get("started_at"):
            elapsed = self._now() - dt.datetime.fromisoformat(state["started_at"])
            hours, rem = divmod(int(elapsed.total_seconds()), 3600)
            embed.description = f"Took {hours}h {rem // 60}m, including any peak-hour pauses."
        embed.add_field(name="💾 Backup", value=f"`{state.get('backup_dir')}`", inline=False)
        embed.add_field(name="🧹 Files cleared", value=str(report.get("files_cleared", 0)), inline=True)
        embed.add_field(name="📚 Articles", value=(
            f"{articles.get('regenerated', 0)} regenerated, {len(articles.get('failed', []))} failed, "
            f"{len(articles.get('kept', []))} kept"), inline=True)
        return embed


async def setup(bot):
    await bot.add_cog(Maintenance(bot))
