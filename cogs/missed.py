import discord
import json
import re
import asyncio
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord import ui
from services.json_utils import extract_json as _extract_json
from services.llm_service import llm_service, LLMError, encode_image_file
from services import psych_service as ps
from services.guild_guard import is_allowed_guild
from config import (
    DEEPSEEK_JOURNAL_EFFORT,
    MISSED_DIR,
    MISSED_INSIGHTS_PATH,
    MISSED_TRACKER_PATH,
    MISSED_CATEGORY_NAME,
)

FAILED_MISSED_PATH = Path.cwd() / "failed_missed.json"
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries

# Missed trades are graded on a different axis than taken trades. There are no
# good_habits/mistakes because nothing was executed — what matters is why the
# setup was left on the table and what would make it catchable next time.
MISSED_INSIGHT_KEYS = ("miss_reasons", "setup_patterns", "action_items")


def empty_missed_insights() -> dict:
    """A fresh empty insight dict. Always build one rather than copying a shared
    constant — a shallow copy would alias the lists and leak across analyses."""
    return {key: [] for key in MISSED_INSIGHT_KEYS}

# Structured reasons, so misses can be counted rather than only read.
# (value, label, emoji) — value is what gets persisted in review.json.
MISS_REASONS = [
    ("not_at_desk", "Wasn't at the desk", "🚪"),
    ("hesitated", "Hesitated / lacked conviction", "😰"),
    ("waited_too_long", "Waited for more confirmation", "⏳"),
    ("no_fill", "Price ran without me / no fill", "🏃"),
    ("wrong_bias", "Had the wrong bias", "🧭"),
    ("risk_rules", "Blocked by risk or session rules", "🛑"),
    ("saw_late", "Only spotted it afterwards", "🔍"),
    ("other", "Other", "❔"),
]

MISS_REASON_LABELS = {value: label for value, label, _ in MISS_REASONS}

# Shull's fear spectrum runs from fear of losing to fear of missing out. A missed
# trade is almost always the loss-fear end expressing itself, so each reason is
# mapped to the fear it most often represents. This lets /missed_stats say
# something sharper than "you hesitated".
MISS_REASON_FEARS = {
    "not_at_desk": None,               # operational, not emotional
    "hesitated": "fear of being wrong",
    "waited_too_long": "fear of being wrong",
    "no_fill": None,                   # mechanical
    "wrong_bias": "fear of being wrong",
    "risk_rules": None,                # a rule doing its job
    "saw_late": None,                  # attention, not fear
    "other": None,
}


def extract_missed_json(raw: str) -> dict:
    """Extract a missed-trade JSON object from a model response."""
    return _extract_json(raw, empty_missed_insights())


async def _load_review(channel_name: str) -> dict:
    """Load the saved review for a missed-trade channel, or an empty dict."""
    review_path = MISSED_DIR / channel_name / "review.json"
    try:
        async with aiofiles.open(review_path, "r", encoding="utf-8") as f:
            return json.loads(await f.read())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        return {}


async def _save_review(channel_name: str, updates: dict) -> dict:
    """Merge updates into a channel's review.json and return the merged result."""
    channel_dir = MISSED_DIR / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    review = await _load_review(channel_name)
    review.update(updates)

    async with aiofiles.open(channel_dir / "review.json", "w", encoding="utf-8") as f:
        await f.write(json.dumps(review, indent=4, ensure_ascii=False))

    return review


# ──────────────────────────────────────────────
# Missed Trade Review UI Components
# ──────────────────────────────────────────────

class MissedReviewModal(ui.Modal, title="🎯 Missed Trade Review"):
    """Discord Modal (form) that collects the 4 missed-trade questions."""

    setup_seen = ui.TextInput(
        label="What was the setup?",
        placeholder="e.g. London swept Asia high, hourly C2 closed bearish, CISD on 5m...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    why_missed = ui.TextInput(
        label="Why did I miss it?",
        placeholder="e.g. Stepped away before London, wanted one more confirmation candle...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    what_happened = ui.TextInput(
        label="What did price do after?",
        placeholder="e.g. Ran 3R straight to the previous day low without pullback...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )
    what_changes = ui.TextInput(
        label="What will I change next time?",
        placeholder="e.g. Set an alert at the Asia high so I'm at the desk for the sweep...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )

    def __init__(self, channel_name: str):
        super().__init__()
        self.channel_name = channel_name

    async def on_submit(self, interaction: discord.Interaction):
        """Save the review to JSON and pin a summary in the channel."""
        review = await _save_review(self.channel_name, {
            "setup_seen": self.setup_seen.value,
            "why_missed": self.why_missed.value,
            "what_happened": self.what_happened.value,
            "what_changes": self.what_changes.value,
        })

        embed = discord.Embed(
            title="🎯 Missed Trade Review Recorded",
            color=discord.Color.orange()
        )
        reason = review.get("reason_category")
        if reason:
            embed.add_field(name="Reason", value=MISS_REASON_LABELS.get(reason, reason), inline=False)
        embed.add_field(name="What was the setup?", value=review["setup_seen"], inline=False)
        embed.add_field(name="Why did I miss it?", value=review["why_missed"], inline=False)
        embed.add_field(name="What did price do after?", value=review["what_happened"], inline=False)
        embed.add_field(name="What will I change next time?", value=review["what_changes"], inline=False)

        await interaction.response.send_message(embed=embed)
        response_msg = await interaction.original_response()
        try:
            await response_msg.pin()
        except discord.Forbidden:
            pass  # Bot may lack pin permissions


class MissReasonSelect(ui.Select):
    """Dropdown that tags the miss with a countable reason category."""

    def __init__(self):
        super().__init__(
            placeholder="Why was this one missed?",
            min_values=1,
            max_values=1,
            custom_id="missed_reason_select",
            options=[
                discord.SelectOption(label=label, value=value, emoji=emoji)
                for value, label, emoji in MISS_REASONS
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        reason = self.values[0]
        await _save_review(interaction.channel.name, {"reason_category": reason})
        await interaction.response.send_message(
            f"🏷️ Tagged as **{MISS_REASON_LABELS[reason]}**.",
            ephemeral=True
        )


class MissedReviewView(ui.View):
    """Persistent reason dropdown + button that opens the MissedReviewModal."""

    def __init__(self):
        super().__init__(timeout=None)  # Never expires
        self.add_item(MissReasonSelect())

    @ui.button(label="🎯 Record Missed Trade Review", style=discord.ButtonStyle.primary, custom_id="missed_review_btn")
    async def open_modal(self, interaction: discord.Interaction, button: ui.Button):
        modal = MissedReviewModal(interaction.channel.name)
        await interaction.response.send_modal(modal)


class Missed(commands.Cog):
    """Journals setups that were seen but never taken, separately from executed trades."""

    def __init__(self, bot):
        self.bot = bot
        # Auto-sync debounce: guild_id → asyncio.Task
        self._pending_sync: dict[int, asyncio.Task] = {}

    # --- Multi-Category Helper ---

    @staticmethod
    def _get_missed_categories(guild: discord.Guild) -> list:
        """Return all categories whose names start with 'Missed_Trades', sorted by name."""
        cats = [c for c in guild.categories if c.name.startswith(MISSED_CATEGORY_NAME)]
        cats.sort(key=lambda c: c.name)
        return cats

    @staticmethod
    def _is_missed_channel(channel) -> bool:
        """True if the channel lives under a Missed_Trades category."""
        category = getattr(channel, "category", None)
        return bool(category and category.name.startswith(MISSED_CATEGORY_NAME))

    # --- Channel Creation Listener ---

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Post the review prompt when a new channel is created under any Missed_Trades category."""
        if not isinstance(channel, discord.TextChannel):
            return
        if not self._is_missed_channel(channel):
            return
        if not is_allowed_guild(channel.guild):
            return

        # No entry checklist here — the checklist is a pre-entry gate, and this
        # trade was never entered. The review questions are the whole point.
        embed = discord.Embed(
            title="🎯 Missed Setup",
            description=(
                f"Logging the one that got away in **#{channel.name}**.\n\n"
                "Drop your charts, tag why it was missed, then record the review. "
                "The button stays here.\n\n"
                "Use `/state` to log what you were feeling when you passed on it — "
                "hesitation is data, not a character flaw."
            ),
            color=discord.Color.orange()
        )
        embed.add_field(name="📋 Questions", value=(
            "1️⃣ **What was the setup?**\n"
            "2️⃣ **Why did I miss it?**\n"
            "3️⃣ **What did price do after?**\n"
            "4️⃣ **What will I change next time?**"
        ), inline=False)

        await channel.send(embed=embed, view=MissedReviewView())

    # --- Auto-Sync Listener ---
    # Watches for new messages in Missed_Trades channels.
    # After 2 minutes of silence, auto-runs sync + analysis.

    AUTO_SYNC_DELAY = 120  # seconds to wait after last message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Detect new posts in Missed_Trades and schedule auto sync+analysis."""
        if message.author.bot:
            return
        if not message.guild:
            return
        if not self._is_missed_channel(message.channel):
            return
        if not is_allowed_guild(message.guild):
            return

        guild_id = message.guild.id

        # Cancel any existing pending sync for this guild (reset the timer)
        if guild_id in self._pending_sync:
            self._pending_sync[guild_id].cancel()

        # Schedule a new delayed sync
        self._pending_sync[guild_id] = self.bot.loop.create_task(
            self._delayed_sync(message.guild, message.channel)
        )

    async def _delayed_sync(self, guild: discord.Guild, trigger_channel: discord.TextChannel):
        """Wait for silence, then run sync + analysis in the trigger channel."""
        try:
            await asyncio.sleep(self.AUTO_SYNC_DELAY)
        except asyncio.CancelledError:
            return  # Timer was reset by a new message

        interaction = await self._make_auto_interaction(trigger_channel)
        if not interaction:
            return

        print(f"⚡ Missed-trade auto-sync triggered by activity in #{trigger_channel.name}")

        await self._do_sync_missed(interaction.guild, interaction.followup.send)
        await self._do_analyze_missed(interaction)

        self._pending_sync.pop(guild.id, None)

    async def _make_auto_interaction(self, channel: discord.TextChannel):
        """Create a minimal interaction-like object so sync/analysis can send messages."""
        # Redirect auto-sync status messages to 'general' chat to avoid cluttering reviews
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

    async def _get_review_context(self, missed: dict) -> str:
        """Load review.json for a missed trade's channel and format as prompt context."""
        folder = Path(missed.get("_folder_path", ""))
        review_path = folder / "review.json"
        if not review_path.exists():
            return ""

        try:
            async with aiofiles.open(review_path, "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
            reason = data.get("reason_category")
            reason_label = MISS_REASON_LABELS.get(reason, reason) if reason else "Not tagged"
            context = (
                "\nTrader's Missed-Trade Review:\n"
                f"- Tagged reason: {reason_label}\n"
                f"- The setup: {data.get('setup_seen', 'N/A')}\n"
                f"- Why it was missed: {data.get('why_missed', 'N/A')}\n"
                f"- What price did after: {data.get('what_happened', 'N/A')}\n"
                f"- Planned change: {data.get('what_changes', 'N/A')}\n"
            )
            fear = MISS_REASON_FEARS.get(reason)
            if fear:
                context += f"- Fear most often behind this reason: {fear}\n"
            return context + await self._get_psych_context(folder)
        except (json.JSONDecodeError, Exception):
            return ""

    @staticmethod
    async def _get_psych_context(folder: Path) -> str:
        """Load any logged psychological state for this missed setup."""
        path = folder / "psych.json"
        if not path.exists():
            return ""
        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                state = json.loads(await f.read())
            return ps.format_state_context(state if isinstance(state, dict) else {})
        except (json.JSONDecodeError, Exception):
            return ""

    # --- Sync ---

    @app_commands.command(name="sync_missed", description="Download all missed trades and images from the Missed_Trades category.")
    async def sync_missed(self, interaction: discord.Interaction):
        """Download all missed trades and images from the Missed_Trades category."""
        await interaction.response.defer()
        await self._do_sync_missed(interaction.guild, interaction.followup.send)

    async def _do_sync_missed(self, guild: discord.Guild, send_func):
        categories = self._get_missed_categories(guild)

        if not categories:
            await send_func(f"❌ Couldn't find category `{MISSED_CATEGORY_NAME}`.")
            return

        await send_func(f"📥 Starting missed-trade sync across **{len(categories)}** {MISSED_CATEGORY_NAME} category/categories...")

        total_images = 0
        for category in categories:
            for channel in category.text_channels:
                channel_path = MISSED_DIR / channel.name
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

        await send_func(f"✅ Missed-trade sync complete! Downloaded **{total_images}** new images and updated all `history.json` files.")

    # --- Analysis ---

    @app_commands.command(name="analyze_missed", description="AI-analyze new missed trades and update missed_insights.json.")
    async def analyze_missed(self, interaction: discord.Interaction):
        """AI-analyze new missed trades and update missed_insights.json."""
        try:
            await interaction.response.defer()
        except AttributeError:
            pass  # Handle AutoInteraction case
        await self._do_analyze_missed(interaction)

    async def _do_analyze_missed(self, interaction):
        if not MISSED_DIR.exists():
            await interaction.followup.send("❌ No missed trades directory found. Run `/sync_missed` first.")
            return

        last_id = 0
        if MISSED_TRACKER_PATH.exists():
            async with aiofiles.open(MISSED_TRACKER_PATH, "r") as f:
                content = (await f.read()).strip()
            if content.isdigit():
                last_id = int(content)

        new_missed = []
        for channel_dir in MISSED_DIR.iterdir():
            if channel_dir.is_dir():
                history_file = channel_dir / "history.json"
                if history_file.exists():
                    async with aiofiles.open(history_file, "r", encoding="utf-8") as f:
                        channel_data = json.loads(await f.read())
                        for entry in channel_data:
                            if int(entry["message_id"]) > last_id:
                                entry["_folder_path"] = str(channel_dir)
                                new_missed.append(entry)

        retry_missed = []
        if FAILED_MISSED_PATH.exists():
            try:
                async with aiofiles.open(FAILED_MISSED_PATH, "r", encoding="utf-8") as f:
                    retry_missed = json.loads(await f.read())
                if retry_missed:
                    await interaction.followup.send(f"🔁 Found **{len(retry_missed)}** previously failed missed trade(s) to retry.")
            except (json.JSONDecodeError, Exception):
                retry_missed = []

        new_missed.sort(key=lambda x: int(x["message_id"]))
        all_missed = retry_missed + new_missed

        if not all_missed:
            await interaction.followup.send("✅ Missed Insights is already up to date!")
            return

        async with aiofiles.open(FAILED_MISSED_PATH, "w", encoding="utf-8") as f:
            await f.write("[]")

        self.bot.loop.create_task(self.run_analysis_loop(interaction, all_missed, last_id))
        await interaction.followup.send(f"🚀 Found **{len(all_missed)}** missed trade(s) to process. Starting background analysis...")

    @staticmethod
    def _build_prompt(missed: dict, review_context: str) -> str:
        """Build the missed-trade analysis prompt for the model."""
        return f"""Analyze this MISSED trading opportunity and its attached chart(s).
The trader utilizes the TTrades Fractal Model (SMT, CISD, FVG).

IMPORTANT: This setup was NEVER TAKEN. There is no execution to grade — do not
comment on entries, exits, or trade management that did not happen. Focus only on
why the opportunity was left on the table and what would make it catchable next time.

Here are examples of how to analyze a missed setup:

EXAMPLE 1:
Notes: "Saw London sweep the Asia high and the hourly C2 close bearish, but I was still asleep. Woke up to a 4R move down to the previous day low."
Output: {{"miss_reasons": ["Not at the desk for the London session open"], "setup_patterns": ["London sweep of Asia high into bearish hourly C2"], "action_items": ["Set a price alert on the Asia session high so the sweep wakes me up"]}}

EXAMPLE 2:
Notes: "CISD confirmed on the 5m inside the C2 candle. I wanted to see one more bearish candle close before entering, price ran to target without me."
Output: {{"miss_reasons": ["Waiting for confirmation beyond the trade rules", "Hesitation despite a valid CISD signal"], "setup_patterns": ["5m CISD inside the hourly C2 candle"], "action_items": ["Treat confirmed CISD as the trigger and stop demanding extra candles"]}}

EXAMPLE 3:
Notes: "Daily bias was bullish but I talked myself into thinking the sweep invalidated it. Skipped the long. It ran 3R up."
Output: {{"miss_reasons": ["Overriding a valid daily bias after a liquidity sweep"], "setup_patterns": ["Bullish daily bias with a downside liquidity sweep before the move"], "action_items": ["Write the daily bias down before session and only flip it on hourly confirmation"]}}

NOW ANALYZE THIS MISSED SETUP:
Notes:
{missed['content']}
{review_context}
TASK:
Extract from THIS SPECIFIC missed setup:
- "miss_reasons": why the trader failed to take it (behavioural, operational, or analytical).
- "setup_patterns": what the setup actually was, so recurring missed setups can be spotted.
- "action_items": concrete, repeatable changes that would catch this setup next time.
Weigh the trader's own review above heavily — it states the real reason.
Keep each insight to a single concise sentence. Do not repeat insights from the examples above unless they genuinely apply.
{ps.SHULL_ANALYST_GUIDANCE}
A missed trade usually sits at the "fear of losing / fear of being wrong" end of the
fear spectrum. Where the notes support it, say which fear was operating — but do not
invent one for a miss that was purely operational (asleep, no fill, blocked by rules).

Respond STRICTLY in valid JSON format with no extra text:
{{"miss_reasons": ["..."], "setup_patterns": ["..."], "action_items": ["..."]}}"""

    async def run_analysis_loop(self, interaction, all_missed, last_id):
        total_missed = len(all_missed)
        success_count = 0
        fallback_count = 0

        master_data = empty_missed_insights()
        if MISSED_INSIGHTS_PATH.exists():
            try:
                async with aiofiles.open(MISSED_INSIGHTS_PATH, "r", encoding="utf-8") as f:
                    master_data = json.loads(await f.read())
            except Exception:
                pass
        # Tolerate an insights file written by an older schema version
        for key in MISSED_INSIGHT_KEYS:
            master_data.setdefault(key, [])

        status_msg = await interaction.followup.send(
            f"🧪 Initializing missed-trade analysis via `{llm_service.model_for(images=True)}`..."
        )

        highest_id = last_id
        for idx, missed in enumerate(all_missed, 1):
            missed_id = int(missed["message_id"])

            try:
                await status_msg.edit(content=(
                    f"🎯 **Missed Batch Progress:** `{idx}/{total_missed}`\n"
                    f"🆔 **ID:** `{missed_id}`\n"
                    f"✅ **Analysed:** `{success_count}` | ⚠️ **Fallback:** `{fallback_count}`"
                ))
            except Exception:
                pass

            images = []
            for img_name in missed.get("images", []):
                encoded = await encode_image_file(Path(missed["_folder_path"]) / img_name)
                if encoded:
                    images.append(encoded)

            review_context = await self._get_review_context(missed)
            prompt = self._build_prompt(missed, review_context)

            parsed = None
            last_error = None

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
                parsed = extract_missed_json(raw)
            except LLMError as e:
                last_error = str(e)

            # Use result or empty fallback — entry is ALWAYS marked as processed
            if parsed is not None:
                for key in MISSED_INSIGHT_KEYS:
                    master_data[key].extend(parsed.get(key, []))
                success_count += 1
            else:
                print(f"Missed trade {missed_id} exhausted all {MAX_RETRIES} retries ({last_error}). Using empty fallback.")
                fallback_count += 1

            # Always advance tracker regardless of outcome
            if missed_id > highest_id:
                highest_id = missed_id
                async with aiofiles.open(MISSED_TRACKER_PATH, "w") as f:
                    await f.write(str(highest_id))

            await asyncio.sleep(0.5)

        for key in MISSED_INSIGHT_KEYS:
            master_data[key] = list(set(master_data[key]))

        async with aiofiles.open(MISSED_INSIGHTS_PATH, "w", encoding="utf-8") as f:
            await f.write(json.dumps(master_data, indent=4, ensure_ascii=False))

        async with aiofiles.open(FAILED_MISSED_PATH, "w", encoding="utf-8") as f:
            await f.write("[]")

        embed = discord.Embed(
            title="✅ Missed Trade Analysis Complete",
            description=f"Processed all **{total_missed}** missed trade(s).",
            color=discord.Color.green() if fallback_count == 0 else discord.Color.orange()
        )
        embed.add_field(name="✅ Full Analysis", value=str(success_count), inline=True)
        embed.add_field(name="⚠️ Empty Fallback", value=str(fallback_count), inline=True)
        embed.add_field(name="🚪 Miss Reasons", value=str(len(master_data["miss_reasons"])), inline=True)
        embed.add_field(name="🔎 Setup Patterns", value=str(len(master_data["setup_patterns"])), inline=True)
        embed.add_field(name="🛠️ Action Items", value=str(len(master_data["action_items"])), inline=True)

        if fallback_count > 0:
            embed.add_field(
                name="ℹ️ About Fallbacks",
                value=f"`{fallback_count}` entry/entries couldn't be parsed after {MAX_RETRIES} attempts and were skipped with an empty result. Check your terminal logs for details.",
                inline=False
            )

        await interaction.followup.send(embed=embed)

    # --- Stats ---

    @app_commands.command(name="missed_stats", description="Show a breakdown of why setups keep getting missed.")
    async def missed_stats(self, interaction: discord.Interaction):
        """Show a breakdown of why setups keep getting missed."""
        await interaction.response.defer()

        counts: dict[str, int] = {}
        assets: dict[str, int] = {}
        total = 0
        untagged = 0

        if MISSED_DIR.exists():
            for channel_dir in sorted(MISSED_DIR.iterdir()):
                if not channel_dir.is_dir():
                    continue
                total += 1

                match = re.match(r"^missed_\d+_(.+)$", channel_dir.name)
                if match:
                    asset = match.group(1).upper()
                    assets[asset] = assets.get(asset, 0) + 1

                review = await _load_review(channel_dir.name)
                reason = review.get("reason_category")
                if reason:
                    counts[reason] = counts.get(reason, 0) + 1
                else:
                    untagged += 1

        if total == 0:
            await interaction.followup.send("📭 No missed trades logged yet. Use `/missed_trade` to log one.")
            return

        embed = discord.Embed(
            title="🎯 Missed Trade Breakdown",
            description=f"**{total}** missed setup(s) logged.",
            color=discord.Color.orange()
        )

        if counts:
            ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            lines = []
            for reason, count in ranked:
                pct = int((count / total) * 100)
                bar = "█" * max(1, int(pct / 10))
                lines.append(f"`{bar:<10}` **{count}** ({pct}%) — {MISS_REASON_LABELS.get(reason, reason)}")
            embed.add_field(name="Why they were missed", value="\n".join(lines), inline=False)

            # Shull read: how much of the miss pile is fear rather than logistics?
            fear_driven = sum(c for r, c in counts.items() if MISS_REASON_FEARS.get(r))
            if fear_driven:
                pct = int((fear_driven / total) * 100)
                embed.add_field(
                    name="🧠 The Shull read",
                    value=(
                        f"**{fear_driven}** of **{total}** ({pct}%) sit at the *fear of being wrong* "
                        "end of the fear spectrum — not logistics, not rules. That's the half "
                        "you can actually work on.\n"
                        "Run `/psych_report` to see whether that fear is costing or saving you money."
                    ),
                    inline=False
                )

        if untagged:
            embed.add_field(name="🏷️ Untagged", value=f"{untagged} missed trade(s) have no reason tagged yet.", inline=False)

        if assets:
            top_assets = sorted(assets.items(), key=lambda kv: kv[1], reverse=True)[:5]
            embed.add_field(
                name="📊 Most missed assets",
                value="\n".join(f"• **{asset}** — {count}" for asset, count in top_assets),
                inline=False
            )

        await interaction.followup.send(embed=embed)

    # --- Channel Creation ---

    async def _do_missed_trade(self, interaction: discord.Interaction, asset: str):
        await interaction.response.defer()

        categories = self._get_missed_categories(interaction.guild)
        if not categories:
            # Unlike Fractal_Trades, bootstrap the category so missed trades are
            # frictionless to log — hesitation is the thing we're fighting here.
            try:
                categories = [await interaction.guild.create_category(MISSED_CATEGORY_NAME)]
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ Couldn't find category '{MISSED_CATEGORY_NAME}' and I don't have permission to create it."
                )
                return
            except discord.HTTPException as e:
                await interaction.followup.send(f"❌ Failed to create category: {e}")
                return

        # Find the highest missed number across ALL Missed_Trades categories
        max_num = 0
        pattern = re.compile(r"^missed_(\d+)_")
        for cat in categories:
            for channel in cat.text_channels:
                match = pattern.match(channel.name)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num

        new_num = max_num + 1
        safe_asset = asset.lower().replace(" ", "_").replace("-", "_")
        new_channel_name = f"missed_{new_num}_{safe_asset}"

        # If the latest category is full (50 channels), create the next one
        latest_category = categories[-1]
        if len(latest_category.text_channels) >= 50:
            last_name = latest_category.name  # e.g. "Missed_Trades" or "Missed_Trades2"
            suffix = last_name[len(MISSED_CATEGORY_NAME):]  # "" → 2, "2" → 3, etc.
            next_num = int(suffix) + 1 if suffix else 2
            new_cat_name = f"{MISSED_CATEGORY_NAME}{next_num}"
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
            await interaction.followup.send(f"🎯 Logged a missed setup: {new_channel.mention}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to create channels in that category.")
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to create channel: {e}")

    @app_commands.command(name="missed_trade", description="Log a setup you saw but didn't take")
    @app_commands.describe(asset="The asset you missed (e.g. gc, es)")
    async def missed_trade(self, interaction: discord.Interaction, asset: str):
        await self._do_missed_trade(interaction, asset)

    @app_commands.command(name="mt", description="Alias for /missed_trade")
    @app_commands.describe(asset="The asset you missed (e.g. gc, es)")
    async def mt(self, interaction: discord.Interaction, asset: str):
        await self._do_missed_trade(interaction, asset)


async def setup(bot):
    await bot.add_cog(Missed(bot))
    bot.add_view(MissedReviewView())
