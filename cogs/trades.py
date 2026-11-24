import discord
import json
import asyncio
import shutil
import datetime
import re
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord import ui
from services.json_utils import extract_json as _extract_json, write_json_atomic
from services.llm_service import llm_service, LLMError, encode_image_file
from services import psych_service as ps
from services.guild_guard import is_allowed_guild
from services.checklist_service import LIVE_CHECKLIST_PATH, MAX_ITEMS as MAX_CHECKLIST_ITEMS
from config import (
    BACKUPS_DIR,
    DEEPSEEK_JOURNAL_EFFORT,
    TRADES_DIR,
    INSIGHTS_PATH,
    TRACKER_PATH,
    CATEGORY_NAME,
    CHECKLIST_PATH
)

FAILED_TRADES_PATH = Path.cwd() / "failed_trades.json"
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries

# Rough DeepSeek cost per journal message (flash, reasoning off, ~1 chart),
# off-peak. Used only for the estimate shown before /reanalyze_trades.
EST_COST_PER_MESSAGE = 0.0005


EMPTY_TRADE_INSIGHTS = {"good_habits": [], "mistakes": []}


def extract_json(raw: str) -> dict:
    """Extract a trade-insight JSON object from a model response."""
    return _extract_json(raw, EMPTY_TRADE_INSIGHTS)


# ──────────────────────────────────────────────
# Trade Reflection UI Components
# ──────────────────────────────────────────────

class ReflectionModal(ui.Modal, title="📝 Trade Reflection"):
    """Discord Modal (form) that collects the 3 reflection questions."""

    why_entered = ui.TextInput(
        label="Why did I enter this trade?",
        placeholder="e.g. CISD confirmed on 5m, swept Asia high, daily bias bearish...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    how_felt = ui.TextInput(
        label="How did I feel?",
        placeholder="e.g. Confident, anxious, FOMO, patient...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    what_learned = ui.TextInput(
        label="What did I learn?",
        placeholder="e.g. Need to wait for C2 closure, stop was too tight...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    # Shull's F-eC probe. Optional, because the honest answer is often "nothing"
    # and forcing it would produce noise instead of data.
    reminds_me_of = ui.TextInput(
        label="What did this remind me of?",
        placeholder="An earlier trade, an earlier week — or something outside markets entirely.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000
    )

    def __init__(self, trades_dir: Path, channel_name: str):
        super().__init__()
        self.trades_dir = trades_dir
        self.channel_name = channel_name

    async def on_submit(self, interaction: discord.Interaction):
        """Save reflections to JSON and pin a summary in the channel."""
        reflections = {
            "why_entered": self.why_entered.value,
            "how_felt": self.how_felt.value,
            "what_learned": self.what_learned.value,
            "reminds_me_of": self.reminds_me_of.value or None
        }

        # Save to disk
        channel_dir = self.trades_dir / self.channel_name
        channel_dir.mkdir(parents=True, exist_ok=True)
        reflections_path = channel_dir / "reflections.json"
        async with aiofiles.open(reflections_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(reflections, indent=4, ensure_ascii=False))

        # Post and pin a summary embed
        embed = discord.Embed(
            title="📝 Trade Reflection Recorded",
            color=discord.Color.teal()
        )
        embed.add_field(name="Why did I enter?", value=reflections["why_entered"], inline=False)
        embed.add_field(name="How did I feel?", value=reflections["how_felt"], inline=False)
        embed.add_field(name="What did I learn?", value=reflections["what_learned"], inline=False)
        if reflections["reminds_me_of"]:
            embed.add_field(name="What did this remind me of?", value=reflections["reminds_me_of"], inline=False)
        embed.set_footer(text="Tip: /state logs the emotional read; /outcome tags the result.")

        await interaction.response.send_message(embed=embed)
        response_msg = await interaction.original_response()
        try:
            await response_msg.pin()
        except discord.Forbidden:
            pass  # Bot may lack pin permissions


class ReflectionView(ui.View):
    """Persistent button that opens the ReflectionModal."""

    def __init__(self):
        super().__init__(timeout=None)  # Never expires

    @ui.button(label="📝 Record Reflections", style=discord.ButtonStyle.primary, custom_id="trade_reflection_btn")
    async def open_modal(self, interaction: discord.Interaction, button: ui.Button):
        channel_name = interaction.channel.name
        modal = ReflectionModal(TRADES_DIR, channel_name)
        await interaction.response.send_modal(modal)

    @ui.button(label="🧠 Shull Review", style=discord.ButtonStyle.secondary, custom_id="shull_review_btn")
    async def shull_review(self, interaction: discord.Interaction, button: ui.Button):
        """Run when you're done writing: a Shull reading of the whole channel."""
        coach = interaction.client.get_cog("Coach")
        if coach is None:
            await interaction.response.send_message("❌ Coaching isn't loaded.", ephemeral=True)
            return
        await coach.review_channel(interaction)


def _load_checklist_items() -> list[str]:
    """
    The checklist new trade channels get.

    The list maintained in #trade_checklist (see cogs/checklist.py) wins; the
    repo's trade_checklist.json is the default until that channel is used.
    """
    for path in (LIVE_CHECKLIST_PATH, CHECKLIST_PATH):
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(items, list) and items:
            return [str(item) for item in items][:MAX_CHECKLIST_ITEMS]
    return []


_CHECK_ICONS = ("✅", "⬜")


def _items_from_embed(message) -> tuple[list[str], list[bool]]:
    """
    Recover a checklist's items and ticks from the message it is shown in.

    Channels created before checklists were snapshotted saved only their
    ticks, so the message itself is the record of which items they had.
    """
    try:
        field = next(f for f in message.embeds[0].fields if f.name == "Checklist")
    except (AttributeError, IndexError, StopIteration, TypeError):
        return [], []
    items, states = [], []
    for line in str(field.value).splitlines():
        line = line.strip()
        icon = next((i for i in _CHECK_ICONS if line.startswith(i)), None)
        if icon is None:
            continue
        items.append(line[len(icon):].strip())
        states.append(icon == "✅")
    return items, states


async def _read_checklist_state(state_path: Path, message=None) -> tuple[list[str], list[bool]]:
    """
    A trade channel's own checklist: the items it was created with, and ticks.

    Saved as {"items": [...], "states": [...]}. The older format was a bare
    list of ticks; for those the items come from the checklist message.
    """
    saved = None
    try:
        async with aiofiles.open(state_path, "r", encoding="utf-8") as f:
            saved = json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if isinstance(saved, dict) and isinstance(saved.get("items"), list) and saved["items"]:
        items = [str(i) for i in saved["items"]]
        states = [bool(x) for x in saved.get("states") or []]
    else:
        items, states = _items_from_embed(message)
        if isinstance(saved, list):
            states = [bool(x) for x in saved]
        if not items:
            items = _load_checklist_items()

    states = (states + [False] * len(items))[:len(items)]
    return items, states


async def _write_checklist_state(state_path: Path, items: list[str], states: list[bool]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(state_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps({"items": items, "states": states}, ensure_ascii=False))


def _build_checklist_embed(items: list[str], states: list[bool]) -> discord.Embed:
    """Build a checklist embed with progress tracking."""
    completed = sum(states)
    total = len(items)
    progress_pct = int((completed / total) * 100) if total > 0 else 0

    # Build progress bar
    filled = int(progress_pct / 10)
    bar = "█" * filled + "░" * (10 - filled)

    if completed == total:
        color = discord.Color.green()
        status = "✅ All checks passed!"
    elif completed > 0:
        color = discord.Color.gold()
        status = f"{bar}  {completed}/{total}"
    else:
        color = discord.Color.light_grey()
        status = f"{bar}  {completed}/{total}"

    embed = discord.Embed(
        title="📋 Trade Entry Checklist",
        description="Toggle each item as you verify it. All checks should pass before entering.",
        color=color
    )

    checklist_lines = []
    for i, item in enumerate(items):
        icon = "✅" if states[i] else "⬜"
        checklist_lines.append(f"{icon}  {item}")

    embed.add_field(name="Checklist", value="\n".join(checklist_lines), inline=False)
    embed.add_field(name="Progress", value=status, inline=False)
    return embed


class ChecklistView(ui.View):
    """
    Toggle buttons for a checklist.

    Each trade channel renders its own snapshot of the checklist. The single
    view registered at startup (`persistent()`) only routes clicks: it carries
    a button for every possible index, so a checklist posted under an older,
    longer list still responds after the list is edited.
    """

    def __init__(self, items: list[str] | None = None, states: list[bool] | None = None,
                 *, slots: int | None = None):
        super().__init__(timeout=None)  # Never expires
        if items is None:
            items = _load_checklist_items()
        self.items = items
        states = states or []
        for i in range(len(items) if slots is None else slots):
            label = f"{i+1}. {items[i][:70]}" if i < len(items) else f"{i+1}."  # labels max 80
            button = ui.Button(
                label=label,
                style=(discord.ButtonStyle.success if i < len(states) and states[i]
                       else discord.ButtonStyle.secondary),
                custom_id=f"checklist_{i}",
            )
            button.callback = self._make_callback(i)
            self.add_item(button)

    @classmethod
    def persistent(cls) -> "ChecklistView":
        """The click router registered once at startup."""
        return cls(items=[], slots=MAX_CHECKLIST_ITEMS)

    def _make_callback(self, index: int):
        """Create a callback closure for a specific checklist item index."""
        async def callback(interaction: discord.Interaction):
            state_path = TRADES_DIR / interaction.channel.name / "checklist_state.json"
            items, states = await _read_checklist_state(state_path, interaction.message)

            if index >= len(items):
                await interaction.response.send_message(
                    "That item isn't on this trade's checklist.", ephemeral=True)
                return

            states[index] = not states[index]
            await _write_checklist_state(state_path, items, states)
            await interaction.response.edit_message(
                embed=_build_checklist_embed(items, states),
                view=ChecklistView(items, states),
            )

        return callback


def dedupe_insights(master_data: dict) -> dict:
    """Drop exact duplicates while keeping first-seen order."""
    return {k: list(dict.fromkeys(master_data.get(k, []))) for k in ("good_habits", "mistakes")}


class ReanalyzeConfirmView(ui.View):
    """Explicit go/no-go before resetting insights — only the invoker may press."""

    def __init__(self, cog: "Trades", author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/reanalyze_trades` can confirm it.", ephemeral=True)
            return False
        return True

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @ui.button(label="Re-analyse everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self._disable()
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.cog._start_reanalysis(interaction)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self._disable()
        await interaction.response.edit_message(content="Cancelled — nothing was changed.", view=self)
        self.stop()


class Trades(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Auto-sync debounce: guild_id → asyncio.Task
        self._pending_sync: dict[int, asyncio.Task] = {}
        # Only one analysis run may hold master_insights.json at a time; two
        # concurrent runs each rewrite it and the last one to finish wins.
        self._analysis_running = False

    # --- Multi-Category Helper ---

    @staticmethod
    def _get_trade_categories(guild: discord.Guild) -> list:
        """Return all categories whose names start with 'Fractal_Trades', sorted by name."""
        cats = [c for c in guild.categories if c.name.startswith("Fractal_Trades")]
        cats.sort(key=lambda c: c.name)
        return cats

    # --- Channel Creation Listener ---

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Post checklist and reflection prompt when a new channel is created under any Fractal_Trades category."""
        if not isinstance(channel, discord.TextChannel):
            return
        if not channel.category or not channel.category.name.startswith("Fractal_Trades"):
            return
        if not is_allowed_guild(channel.guild):
            return

        # --- Checklist ---
        items = _load_checklist_items()
        if items:
            states = [False] * len(items)
            checklist_embed = _build_checklist_embed(items, states)
            checklist_view = ChecklistView(items)
            await channel.send(embed=checklist_embed, view=checklist_view)

            # Snapshot the list with the ticks, so later edits to the checklist
            # in #trade_checklist don't change this trade's.
            await _write_checklist_state(TRADES_DIR / channel.name / "checklist_state.json",
                                         items, states)

        # --- Reflection Prompt ---
        embed = discord.Embed(
            title="🆕 New Trade Channel",
            description=(
                f"Welcome to **#{channel.name}**!\n\n"
                "When you're ready, click below to record your reflections for this trade. "
                "Take your time — the button stays here."
            ),
            color=discord.Color.blue()
        )
        embed.add_field(name="📋 Questions", value=(
            "1️⃣ **Why did I enter this trade?**\n"
            "2️⃣ **How did I feel?**\n"
            "3️⃣ **What did I learn?**\n"
            "4️⃣ **What did this remind me of?**"
        ), inline=False)
        embed.add_field(name="🧠 Before you click", value=(
            "`/state` — mark-to-market your emotional read *before* the entry.\n"
            "`/outcome` — tag the result afterwards so the two can be correlated."
        ), inline=False)
        embed.add_field(name="🧠 When you're done writing", value=(
            "Press **Shull Review** for a reading of everything in this channel — your "
            "notes and your reflections — against Denise Shull's framework. "
            "Edited or added something? Press it again and the reading is updated in place."
        ), inline=False)

        view = ReflectionView()
        await channel.send(embed=embed, view=view)

    # --- Auto-Sync Listener ---
    # Watches for new messages in Fractal_Trades channels.
    # After 2 minutes of silence, auto-runs sync + analysis.

    AUTO_SYNC_DELAY = 120  # seconds to wait after last message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Detect new posts in Fractal_Trades and schedule auto sync+analysis."""
        if message.author.bot:
            return
        if not message.guild:
            return

        channel = message.channel
        if not hasattr(channel, "category") or not channel.category:
            return
        if not channel.category.name.startswith("Fractal_Trades"):
            return
        if not is_allowed_guild(message.guild):
            return

        guild_id = message.guild.id

        # Cancel any existing pending sync for this guild (reset the timer)
        if guild_id in self._pending_sync:
            self._pending_sync[guild_id].cancel()

        # Schedule a new delayed sync
        self._pending_sync[guild_id] = self.bot.loop.create_task(
            self._delayed_sync(message.guild, channel)
        )

    async def _delayed_sync(self, guild: discord.Guild, trigger_channel: discord.TextChannel):
        """Wait for silence, then run sync + analysis in the trigger channel."""
        try:
            await asyncio.sleep(self.AUTO_SYNC_DELAY)
        except asyncio.CancelledError:
            return  # Timer was reset by a new message

        # Build a lightweight interaction-like object for reusing sync/analysis logic
        interaction = await self._make_auto_interaction(trigger_channel)
        if not interaction:
            return

        print(f"⚡ Auto-sync triggered by activity in #{trigger_channel.name}")
        
        # Manually invoke the logic directly (instead of using the slash command callback)
        await self._do_sync_fractals(interaction.guild, interaction.followup.send)
        await self._do_analyze_trades(interaction)

        # Clean up
        self._pending_sync.pop(guild.id, None)

    async def _make_auto_interaction(self, channel: discord.TextChannel):
        """Create a minimal interaction-like object so sync/analysis can send messages."""
        # Redirect auto-sync status messages to 'general' chat to avoid cluttering trade reviews
        target_channel = discord.utils.get(channel.guild.text_channels, name="general")
        if not target_channel:
            target_channel = channel

        class AutoFollowup:
            async def send(self, *args, **kwargs):
                return await target_channel.send(*args, **kwargs)

        class AutoInteraction:
            def __init__(self):
                self.guild = channel.guild
                self.followup = AutoFollowup()
                
        return AutoInteraction()

    async def _get_reflections_context(self, trade: dict) -> str:
        """Load reflections.json for a trade's channel and format as prompt context."""
        folder = Path(trade.get("_folder_path", ""))
        reflections_path = folder / "reflections.json"
        if not reflections_path.exists():
            return ""

        try:
            async with aiofiles.open(reflections_path, "r", encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content)
            context = (
                "\nTrader's Self-Reflection:\n"
                f"- Why I entered: {data.get('why_entered', 'N/A')}\n"
                f"- How I felt: {data.get('how_felt', 'N/A')}\n"
                f"- What I learned: {data.get('what_learned', 'N/A')}\n"
            )
            if data.get("reminds_me_of"):
                context += f"- What it reminded me of (F-eC): {data['reminds_me_of']}\n"
            return context + await self._get_psych_context(folder)
        except (json.JSONDecodeError, Exception):
            return ""

    @staticmethod
    async def _get_psych_context(folder: Path) -> str:
        """Load any logged psychological state + outcome for this trade."""
        state, outcome = {}, {}
        for name, target in (("psych.json", "state"), ("outcome.json", "outcome")):
            path = folder / name
            if not path.exists():
                continue
            try:
                async with aiofiles.open(path, "r", encoding="utf-8") as f:
                    loaded = json.loads(await f.read())
                if isinstance(loaded, dict):
                    if target == "state":
                        state = loaded
                    else:
                        outcome = loaded
            except (json.JSONDecodeError, Exception):
                continue
        return ps.format_state_context(state, outcome)

    @app_commands.command(name="sync_fractals", description="Download all trades and images from the Fractal_Trades category.")
    async def sync_fractals(self, interaction: discord.Interaction):
        """Download all trades and images from the Fractal_Trades category."""
        await interaction.response.defer()
        await self._do_sync_fractals(interaction.guild, interaction.followup.send)

    async def _do_sync_fractals(self, guild: discord.Guild, send_func):
        categories = self._get_trade_categories(guild)

        if not categories:
            await send_func(f"❌ Couldn't find category `{CATEGORY_NAME}`.")
            return

        await send_func(f"📥 Starting full JSON and Image sync across **{len(categories)}** Fractal_Trades category/categories...")

        total_images = 0
        for category in categories:
            for channel in category.text_channels:
                channel_path = TRADES_DIR / channel.name
                channel_path.mkdir(parents=True, exist_ok=True)
                channel_data = []

                async for message in channel.history(limit=None, oldest_first=True):
                    if message.content or message.attachments:
                        msg_entry = {
                            "message_id": str(message.id),
                            "timestamp": message.created_at.isoformat(),
                            "author": message.author.name,
                            "content": message.content,
                            "images": []
                        }

                        if message.attachments:
                            for attachment in message.attachments:
                                if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                                    unique_filename = f"{message.id}_{attachment.filename}"
                                    file_path = channel_path / unique_filename

                                    if not file_path.exists():
                                        await attachment.save(file_path)
                                        total_images += 1

                                    msg_entry["images"].append(unique_filename)

                        channel_data.append(msg_entry)

                async with aiofiles.open(channel_path / "history.json", "w", encoding="utf-8") as f:
                    await f.write(json.dumps(channel_data, indent=4))

        await send_func(f"✅ Sync complete! Downloaded **{total_images}** new images and updated all `history.json` files.")

    @app_commands.command(name="analyze_trades", description="AI-analyze new trades and update master_insights.json.")
    async def analyze_trades(self, interaction: discord.Interaction):
        """AI-analyze new trades and update master_insights.json."""
        try:
            await interaction.response.defer()
        except AttributeError:
            pass # Handle AutoInteraction case
        await self._do_analyze_trades(interaction)

    @app_commands.command(name="reanalyze_trades",
                          description="Re-sync every trade and re-run all of them through the AI from scratch.")
    @app_commands.default_permissions(administrator=True)
    async def reanalyze_trades(self, interaction: discord.Interaction):
        """Sync, show what a full re-run would cost, and wait for confirmation."""
        await interaction.response.defer()
        if self._analysis_running:
            await interaction.followup.send("⏳ An analysis run is already in progress. Try again once it finishes.")
            return

        await self._do_sync_fractals(interaction.guild, interaction.followup.send)
        if not TRADES_DIR.exists():
            return

        trades, duplicates = await self._collect_trades(0)
        if not trades:
            await interaction.followup.send("📭 No trade messages found to analyse.")
            return

        n_images = sum(len(t.get("images", [])) for t in trades)
        est = len(trades) * EST_COST_PER_MESSAGE

        embed = discord.Embed(
            title="🔁 Re-analyse every trade?",
            description=(f"Sync finished. This runs every trade message back through "
                         f"`{llm_service.model_for(images=True)}`."),
            color=discord.Color.orange(),
        )
        embed.add_field(name="What happens", value=(
            "1. `master_insights.json` and `last_analyzed.txt` are copied to `backups/`\n"
            "2. Both are reset to empty\n"
            f"3. **{len(trades)}** messages (**{n_images}** charts) are analysed from scratch"
        ), inline=False)
        embed.add_field(name="Rough cost", value=(
            f"~${est:.2f} off-peak, ~${est * 2:.2f} at peak (01:00–04:00 and 06:00–10:00 UTC, weekdays)\n"
            "Estimate only."
        ), inline=False)
        if duplicates:
            embed.add_field(name="🗂️ Stale folders", value=(
                f"{duplicates} message(s) also appear in leftover folders from renamed channels. "
                "Each message is analysed once."
            ), inline=False)
        embed.add_field(name="If it's interrupted", value=(
            "Insights are saved after every trade. Run `/analyze_trades` to resume where it stopped."
        ), inline=False)

        await interaction.followup.send(embed=embed, view=ReanalyzeConfirmView(self, interaction.user.id))

    async def _backup_analysis_state(self) -> list[Path]:
        """Copy the current insights and tracker into backups/ with a timestamp."""
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        saved = []
        for src in (INSIGHTS_PATH, TRACKER_PATH):
            if src.exists():
                dest = BACKUPS_DIR / f"{src.stem}-{stamp}{src.suffix}"
                shutil.copy2(src, dest)
                saved.append(dest)
        return saved

    async def _start_reanalysis(self, interaction: discord.Interaction):
        """Back up, reset, then hand over to the normal analysis path."""
        if self._analysis_running:
            await interaction.followup.send("⏳ An analysis run is already in progress.")
            return

        saved = await self._backup_analysis_state()
        await write_json_atomic(INSIGHTS_PATH, {"good_habits": [], "mistakes": []})
        async with aiofiles.open(TRACKER_PATH, "w") as f:
            await f.write("0")
        async with aiofiles.open(FAILED_TRADES_PATH, "w", encoding="utf-8") as f:
            await f.write("[]")

        backed_up = ", ".join(f"`backups/{p.name}`" for p in saved) or "nothing to back up"
        await interaction.followup.send(f"💾 Backed up: {backed_up}. Insights reset — starting the re-run.")
        await self._do_analyze_trades(interaction)

    async def _collect_trades(self, last_id: int) -> tuple[list[dict], int]:
        """
        Gather history entries newer than `last_id`, one per message id.

        A renamed channel leaves its old folder behind (sync never deletes), so
        the same message can appear in two history.json files. Keep the copy
        from the most recently written file — the live channel is rewritten on
        every sync, the stale folder never is. Returns (entries, duplicates).
        """
        by_id: dict[int, tuple[float, dict]] = {}
        duplicates = 0
        for channel_dir in TRADES_DIR.iterdir():
            history_file = channel_dir / "history.json"
            if not (channel_dir.is_dir() and history_file.exists()):
                continue
            mtime = history_file.stat().st_mtime
            try:
                async with aiofiles.open(history_file, "r", encoding="utf-8") as f:
                    channel_data = json.loads(await f.read())
            except (json.JSONDecodeError, OSError):
                continue
            for entry in channel_data:
                mid = int(entry["message_id"])
                if mid <= last_id:
                    continue
                entry["_folder_path"] = str(channel_dir)
                if mid in by_id:
                    duplicates += 1
                    if mtime <= by_id[mid][0]:
                        continue
                by_id[mid] = (mtime, entry)
        return [entry for _, entry in by_id.values()], duplicates

    async def _do_analyze_trades(self, interaction):
        if self._analysis_running:
            await interaction.followup.send(
                "⏳ An analysis run is already in progress. New trades will be picked up by the next run.")
            return

        if not TRADES_DIR.exists():
            await interaction.followup.send("❌ No trades directory found. Run `/sync_fractals` first.")
            return

        last_id = 0
        if TRACKER_PATH.exists():
            async with aiofiles.open(TRACKER_PATH, "r") as f:
                content = (await f.read()).strip()
            if content.isdigit():
                last_id = int(content)

        new_trades, duplicates = await self._collect_trades(last_id)

        retry_trades = []
        if FAILED_TRADES_PATH.exists():
            try:
                async with aiofiles.open(FAILED_TRADES_PATH, "r", encoding="utf-8") as f:
                    retry_trades = json.loads(await f.read())
                if retry_trades:
                    await interaction.followup.send(f"🔁 Found **{len(retry_trades)}** previously failed trade(s) to retry.")
            except (json.JSONDecodeError, Exception):
                retry_trades = []

        new_trades.sort(key=lambda x: int(x["message_id"]))
        fresh_ids = {int(t["message_id"]) for t in new_trades}
        retry_trades = [t for t in retry_trades if int(t["message_id"]) not in fresh_ids]
        all_trades = retry_trades + new_trades
        if duplicates:
            print(f"[analyze] skipped {duplicates} duplicate message(s) from stale channel folders")

        if not all_trades:
            await interaction.followup.send("✅ Master Insights is already up to date!")
            return

        async with aiofiles.open(FAILED_TRADES_PATH, "w", encoding="utf-8") as f:
            await f.write("[]")

        self._analysis_running = True
        self.bot.loop.create_task(self.run_analysis_loop(interaction, all_trades, last_id))
        await interaction.followup.send(f"🚀 Found **{len(all_trades)}** trade(s) to process. Starting background analysis...")

    async def run_analysis_loop(self, interaction, all_trades, last_id):
        """Run the analysis, always releasing the single-run lock afterwards."""
        self._analysis_running = True
        try:
            await self._run_analysis(interaction, all_trades, last_id)
        finally:
            self._analysis_running = False

    async def _run_analysis(self, interaction, all_trades, last_id, before_each=None):
        """
        `before_each`, if given, is awaited before every model call. The full
        rebuild uses it to sit out DeepSeek's peak hours mid-run.
        """
        total_trades = len(all_trades)
        success_count = 0
        fallback_count = 0

        master_data = {"good_habits": [], "mistakes": []}
        if INSIGHTS_PATH.exists():
            try:
                async with aiofiles.open(INSIGHTS_PATH, "r", encoding="utf-8") as f:
                    master_data = json.loads(await f.read())
            except Exception:
                pass
        master_data = dedupe_insights(master_data)

        status_msg = await interaction.followup.send(
            f"🧪 Initializing analysis via `{llm_service.model_for(images=True)}`..."
        )

        highest_id = last_id
        for idx, trade in enumerate(all_trades, 1):
            trade_id = int(trade["message_id"])

            try:
                await status_msg.edit(content=(
                    f"📊 **Batch Progress:** `{idx}/{total_trades}`\n"
                    f"🆔 **ID:** `{trade_id}`\n"
                    f"✅ **Analysed:** `{success_count}` | ⚠️ **Fallback:** `{fallback_count}`"
                ))
            except Exception:
                pass

            images = []
            for img_name in trade.get("images", []):
                encoded = await encode_image_file(Path(trade["_folder_path"]) / img_name)
                if encoded:
                    images.append(encoded)

            reflections_context = await self._get_reflections_context(trade)
            prompt = f"""Analyze this single trading journal entry and its attached chart(s).
The trader utilizes the TTrades Fractal Model (SMT, CISD, FVG).

Here are examples of how to extract insights from trade notes:

EXAMPLE 1:
Trade Notes: "Waited for the hourly C2 candle to close bearish, confirmed CISD on the 5m, then entered short at the continuation OB. Hit 2R target at the previous day low."
Output: {{"good_habits": ["Waiting for hourly C2 candle closure before entry", "Confirming CISD on 5-minute timeframe for precision", "Using continuation Order Block for entry", "Hitting predefined 2R target with discipline"], "mistakes": []}}

EXAMPLE 2:
Trade Notes: "Took a long after seeing one bullish candle. No CISD confirmation, no C2 closure. Got stopped out. Then re-entered the same direction out of frustration."
Output: {{"good_habits": [], "mistakes": ["Entering without CISD confirmation", "Not waiting for C2 candle closure", "Revenge trading after initial loss"]}}

EXAMPLE 3:
Trade Notes: "Daily bias was bearish. Waited for London sweep of Asia high, then looked for shorts. Found IC-CISD within the C2 candle on the hourly. Entered at the FVG but placed stop too tight and got stopped out before the move."
Output: {{"good_habits": ["Establishing daily bias before session", "Waiting for London sweep of Asia session high", "Identifying IC-CISD within the C2 candle", "Using FVG for entry confluence"], "mistakes": ["Stop loss placed too tight, stopped out before the anticipated move"]}}

NOW ANALYZE THIS TRADE:
Trade Notes:
{trade['content']}
{reflections_context}
TASK:
Extract the specific 'good_habits' and 'mistakes' mentioned or visible in THIS SPECIFIC trade.
Also consider the trader's own reflections if provided above — they reveal emotional and decision-making patterns.
Keep each insight to a single concise sentence. Do not repeat insights from the examples above unless they genuinely apply.
{ps.SHULL_ANALYST_GUIDANCE}

Respond STRICTLY in valid JSON format with no extra text:
{{"good_habits": ["..."], "mistakes": ["..."]}}"""

            parsed = None
            last_error = None
            if before_each:
                await before_each()

            # Retries, timeouts and provider differences live in llm_service.
            try:
                raw = await llm_service.complete(
                    prompt,
                    images=images,
                    json_mode=True,
                    reasoning_effort=DEEPSEEK_JOURNAL_EFFORT,
                    retries=MAX_RETRIES,
                    retry_delay=RETRY_DELAY,
                )
                parsed = extract_json(raw)
            except LLMError as e:
                last_error = str(e)

            # Use result or empty fallback — trade is ALWAYS marked as processed
            if parsed is not None:
                master_data["good_habits"].extend(parsed.get("good_habits", []))
                master_data["mistakes"].extend(parsed.get("mistakes", []))
                success_count += 1
            else:
                print(f"Trade {trade_id} exhausted all {MAX_RETRIES} retries ({last_error}). Using empty fallback.")
                fallback_count += 1

            # Save insights first, then advance the tracker. If the process dies
            # between the two, the trade is re-analysed next run (harmless,
            # deduped) instead of being marked done with its insights lost.
            await write_json_atomic(INSIGHTS_PATH, dedupe_insights(master_data))
            if trade_id > highest_id:
                highest_id = trade_id
                async with aiofiles.open(TRACKER_PATH, "w") as f:
                    await f.write(str(highest_id))

            await asyncio.sleep(0.5)

        master_data = dedupe_insights(master_data)
        await write_json_atomic(INSIGHTS_PATH, master_data)

        async with aiofiles.open(FAILED_TRADES_PATH, "w", encoding="utf-8") as f:
            await f.write("[]")

        embed = discord.Embed(
            title="✅ Analysis Complete",
            description=f"Processed all **{total_trades}** trade(s).",
            color=discord.Color.green() if fallback_count == 0 else discord.Color.orange()
        )
        embed.add_field(name="✅ Full Analysis", value=str(success_count), inline=True)
        embed.add_field(name="⚠️ Empty Fallback", value=str(fallback_count), inline=True)
        embed.add_field(name="📚 Total Strengths", value=str(len(master_data["good_habits"])), inline=True)
        embed.add_field(name="⚠️ Total Mistakes", value=str(len(master_data["mistakes"])), inline=True)

        if fallback_count > 0:
            embed.add_field(
                name="ℹ️ About Fallbacks",
                value=f"`{fallback_count}` trade(s) couldn't be parsed after {MAX_RETRIES} attempts and were skipped with an empty result. Check your terminal logs for details.",
                inline=False
            )

        await interaction.followup.send(embed=embed)

    async def _do_new_trade(self, interaction: discord.Interaction, asset: str):
        await interaction.response.defer()

        categories = self._get_trade_categories(interaction.guild)
        if not categories:
            await interaction.followup.send(f"❌ Couldn't find category '{CATEGORY_NAME}'. Please create it first.")
            return

        # Find the highest trade number across ALL Fractal_Trades categories
        max_num = 0
        pattern = re.compile(r"^trade_(\d+)_")
        for cat in categories:
            for channel in cat.text_channels:
                match = pattern.match(channel.name)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num

        new_num = max_num + 1
        safe_asset = asset.lower().replace(" ", "_").replace("-", "_")
        new_channel_name = f"trade_{new_num}_{safe_asset}"

        # If the latest category is full (50 channels), create the next one
        latest_category = categories[-1]
        if len(latest_category.text_channels) >= 50:
            last_name = latest_category.name  # e.g. "Fractal_Trades" or "Fractal_Trades2"
            suffix = last_name[len("Fractal_Trades"):]  # "" → 2, "2" → 3, etc.
            next_num = int(suffix) + 1 if suffix else 2
            new_cat_name = f"Fractal_Trades{next_num}"
            try:
                latest_category = await interaction.guild.create_category(new_cat_name)
            except discord.Forbidden:
                await interaction.followup.send("❌ I don't have permission to create a new category.")
                return
            except discord.HTTPException as e:
                await interaction.followup.send(f"❌ Failed to create category: {e}")
                return

        try:
            new_channel = await latest_category.create_text_channel(name=new_channel_name)
            await interaction.followup.send(f"✅ Successfully created new trade channel: {new_channel.mention}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to create channels in that category.")
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to create channel: {e}")

    @app_commands.command(name="new_trade", description="Creates a new numbered trade channel")
    @app_commands.describe(asset="The asset being traded (e.g. gc, es)")
    async def new_trade(self, interaction: discord.Interaction, asset: str):
        await self._do_new_trade(interaction, asset)

    @app_commands.command(name="nt", description="Alias for /new_trade")
    @app_commands.describe(asset="The asset being traded (e.g. gc, es)")
    async def nt(self, interaction: discord.Interaction, asset: str):
        await self._do_new_trade(interaction, asset)


async def setup(bot):
    await bot.add_cog(Trades(bot))
    bot.add_view(ReflectionView())
    # Routes checklist clicks in every trade channel, whatever list it has.
    bot.add_view(ChecklistView.persistent())