import discord
import json
import re
import aiohttp
import base64
import asyncio
import aiofiles
import asyncio
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord import ui
from config import (
    OLLAMA_API_URL,
    OLLAMA_MODEL,
    TRADES_DIR,
    INSIGHTS_PATH,
    TRACKER_PATH,
    CATEGORY_NAME,
    CHECKLIST_PATH
)

FAILED_TRADES_PATH = Path.cwd() / "failed_trades.json"
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries


def extract_json(raw: str) -> dict:
    """
    Robustly extracts a JSON object from a model response.
    Tries three methods in order:
      1. Direct parse (model returned clean JSON)
      2. Regex extraction (JSON buried in surrounding text)
      3. Fallback empty result (model response was completely unparseable)
    """
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{.*?\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    print(f"[WARN] Could not parse JSON from model response. Using empty fallback.\nRaw: {raw[:300]}")
    return {"good_habits": [], "mistakes": []}


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

    def __init__(self, trades_dir: Path, channel_name: str):
        super().__init__()
        self.trades_dir = trades_dir
        self.channel_name = channel_name

    async def on_submit(self, interaction: discord.Interaction):
        """Save reflections to JSON and pin a summary in the channel."""
        reflections = {
            "why_entered": self.why_entered.value,
            "how_felt": self.how_felt.value,
            "what_learned": self.what_learned.value
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


def _load_checklist_items() -> list[str]:
    """Load checklist items from the JSON config file."""
    try:
        with open(CHECKLIST_PATH, "r", encoding="utf-8") as f:
            items = json.load(f)
        if isinstance(items, list) and items:
            return [str(item) for item in items]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


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
    """Persistent view with toggle buttons for each checklist item."""

    def __init__(self, items: list[str] | None = None):
        super().__init__(timeout=None)  # Never expires
        if items is None:
            items = _load_checklist_items()
        self.items = items
        # Dynamically add a button for each item
        for i, item in enumerate(items):
            button = ui.Button(
                label=f"{i+1}. {item[:70]}",  # Discord button label max ~80 chars
                style=discord.ButtonStyle.secondary,
                custom_id=f"checklist_{i}",
            )
            button.callback = self._make_callback(i)
            self.add_item(button)

    def _make_callback(self, index: int):
        """Create a callback closure for a specific checklist item index."""
        async def callback(interaction: discord.Interaction):
            channel_name = interaction.channel.name
            channel_dir = TRADES_DIR / channel_name
            channel_dir.mkdir(parents=True, exist_ok=True)
            state_path = channel_dir / "checklist_state.json"

            # Load current state
            try:
                async with aiofiles.open(state_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                    states = json.loads(content)
            except (FileNotFoundError, json.JSONDecodeError):
                states = [False] * len(self.items)

            # Ensure states list matches items length
            while len(states) < len(self.items):
                states.append(False)

            # Toggle the clicked item
            states[index] = not states[index]

            # Save state
            async with aiofiles.open(state_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(states))

            # Update the embed in-place
            embed = _build_checklist_embed(self.items, states)

            # Update button styles to reflect state
            for i, child in enumerate(self.children):
                if isinstance(child, ui.Button) and child.custom_id and child.custom_id.startswith("checklist_"):
                    btn_idx = int(child.custom_id.split("_")[1])
                    if btn_idx < len(states) and states[btn_idx]:
                        child.style = discord.ButtonStyle.success
                    else:
                        child.style = discord.ButtonStyle.secondary

            await interaction.response.edit_message(embed=embed, view=self)

        return callback


class Trades(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Auto-sync debounce: guild_id → asyncio.Task
        self._pending_sync: dict[int, asyncio.Task] = {}

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

        # --- Checklist ---
        items = _load_checklist_items()
        if items:
            states = [False] * len(items)
            checklist_embed = _build_checklist_embed(items, states)
            checklist_view = ChecklistView(items)
            await channel.send(embed=checklist_embed, view=checklist_view)

            # Save initial state to disk
            channel_dir = TRADES_DIR / channel.name
            channel_dir.mkdir(parents=True, exist_ok=True)
            state_path = channel_dir / "checklist_state.json"
            async with aiofiles.open(state_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(states))

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
            "3️⃣ **What did I learn?**"
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
            return (
                "\nTrader's Self-Reflection:\n"
                f"- Why I entered: {data.get('why_entered', 'N/A')}\n"
                f"- How I felt: {data.get('how_felt', 'N/A')}\n"
                f"- What I learned: {data.get('what_learned', 'N/A')}\n"
            )
        except (json.JSONDecodeError, Exception):
            return ""

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

    async def _do_analyze_trades(self, interaction):
        if not TRADES_DIR.exists():
            await interaction.followup.send("❌ No trades directory found. Run `/sync_fractals` first.")
            return

        last_id = 0
        if TRACKER_PATH.exists():
            async with aiofiles.open(TRACKER_PATH, "r") as f:
                content = (await f.read()).strip()
            if content.isdigit():
                last_id = int(content)

        new_trades = []
        for channel_dir in TRADES_DIR.iterdir():
            if channel_dir.is_dir():
                history_file = channel_dir / "history.json"
                if history_file.exists():
                    async with aiofiles.open(history_file, "r", encoding="utf-8") as f:
                        channel_data = json.loads(await f.read())
                        for entry in channel_data:
                            if int(entry["message_id"]) > last_id:
                                entry["_folder_path"] = str(channel_dir)
                                new_trades.append(entry)

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
        all_trades = retry_trades + new_trades

        if not all_trades:
            await interaction.followup.send("✅ Master Insights is already up to date!")
            return

        async with aiofiles.open(FAILED_TRADES_PATH, "w", encoding="utf-8") as f:
            await f.write("[]")

        self.bot.loop.create_task(self.run_analysis_loop(interaction, all_trades, last_id))
        await interaction.followup.send(f"🚀 Found **{len(all_trades)}** trade(s) to process. Starting background analysis...")

    async def run_analysis_loop(self, interaction, all_trades, last_id):
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

        status_msg = await interaction.followup.send("🧪 Initializing analysis...")

        async with aiohttp.ClientSession() as session:
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

                images_b64 = []
                for img_name in trade.get("images", []):
                    img_path = Path(trade["_folder_path"]) / img_name
                    if img_path.exists():
                        async with aiofiles.open(img_path, "rb") as f:
                            images_b64.append(base64.b64encode(await f.read()).decode("utf-8"))

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

Respond STRICTLY in valid JSON format with no extra text:
{{"good_habits": ["..."], "mistakes": ["..."]}}"""

                payload = {
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "images": images_b64,
                    "stream": False,
                    "format": "json"
                }

                parsed = None
                last_error = None

                # Retry loop — up to MAX_RETRIES attempts per trade
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        async with session.post(OLLAMA_API_URL, json=payload, timeout=180) as resp:
                            if resp.status == 200:
                                res = await resp.json()
                                parsed = extract_json(res["response"])
                                break  # success — exit retry loop
                            else:
                                last_error = f"HTTP {resp.status}"
                                print(f"Trade {trade_id} attempt {attempt} failed — {last_error}")
                    except asyncio.TimeoutError:
                        last_error = "Timeout"
                        print(f"Trade {trade_id} attempt {attempt} timed out.")
                    except Exception as e:
                        last_error = str(e)
                        print(f"Trade {trade_id} attempt {attempt} error: {e}")

                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY)

                # Use result or empty fallback — trade is ALWAYS marked as processed
                if parsed is not None:
                    master_data["good_habits"].extend(parsed.get("good_habits", []))
                    master_data["mistakes"].extend(parsed.get("mistakes", []))
                    success_count += 1
                else:
                    print(f"Trade {trade_id} exhausted all {MAX_RETRIES} retries ({last_error}). Using empty fallback.")
                    fallback_count += 1

                # Always advance tracker regardless of outcome
                if trade_id > highest_id:
                    highest_id = trade_id
                    async with aiofiles.open(TRACKER_PATH, "w") as f:
                        await f.write(str(highest_id))

                await asyncio.sleep(0.5)

        master_data["good_habits"] = list(set(master_data["good_habits"]))
        master_data["mistakes"] = list(set(master_data["mistakes"]))

        async with aiofiles.open(INSIGHTS_PATH, "w", encoding="utf-8") as f:
            await f.write(json.dumps(master_data, indent=4, ensure_ascii=False))

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
    # Register persistent checklist view (items loaded from config)
    items = _load_checklist_items()
    if items:
        bot.add_view(ChecklistView(items))