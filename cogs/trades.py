import discord
import json
import re
import aiohttp
import base64
import asyncio
from pathlib import Path
from discord.ext import commands
from config import (
    OLLAMA_API_URL,
    OLLAMA_MODEL,
    TRADES_DIR,
    INSIGHTS_PATH,
    TRACKER_PATH
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


class Trades(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="sync_fractals")
    async def sync_fractals(self, ctx):
        """Download all trades and images from the Fractal_Trades category."""
        target_category_name = "Fractal_Trades"
        category = discord.utils.get(ctx.guild.categories, name=target_category_name)

        if not category:
            await ctx.send(f"❌ Couldn't find category `{target_category_name}`.")
            return

        await ctx.send(f"📥 Starting full JSON and Image sync for **{target_category_name}**...")

        total_images = 0
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

            with open(channel_path / "history.json", "w", encoding="utf-8") as f:
                json.dump(channel_data, f, indent=4)

        await ctx.send(f"✅ Sync complete! Downloaded **{total_images}** new images and updated all `history.json` files.")

    @commands.command(name="analyze_trades")
    async def analyze_trades(self, ctx):
        """AI-analyze new trades and update master_insights.json."""
        if not TRADES_DIR.exists():
            await ctx.send("❌ No trades directory found. Run `!sync_fractals` first.")
            return

        last_id = 0
        if TRACKER_PATH.exists():
            content = TRACKER_PATH.read_text().strip()
            if content.isdigit():
                last_id = int(content)

        new_trades = []
        for channel_dir in TRADES_DIR.iterdir():
            if channel_dir.is_dir():
                history_file = channel_dir / "history.json"
                if history_file.exists():
                    with open(history_file, "r", encoding="utf-8") as f:
                        channel_data = json.load(f)
                        for entry in channel_data:
                            if int(entry["message_id"]) > last_id:
                                entry["_folder_path"] = str(channel_dir)
                                new_trades.append(entry)

        retry_trades = []
        if FAILED_TRADES_PATH.exists():
            try:
                with open(FAILED_TRADES_PATH, "r", encoding="utf-8") as f:
                    retry_trades = json.load(f)
                if retry_trades:
                    await ctx.send(f"🔁 Found **{len(retry_trades)}** previously failed trade(s) to retry.")
            except (json.JSONDecodeError, Exception):
                retry_trades = []

        new_trades.sort(key=lambda x: int(x["message_id"]))
        all_trades = retry_trades + new_trades

        if not all_trades:
            await ctx.send("✅ Master Insights is already up to date!")
            return

        FAILED_TRADES_PATH.write_text("[]")

        self.bot.loop.create_task(self.run_analysis_loop(ctx, all_trades, last_id))
        await ctx.send(f"🚀 Found **{len(all_trades)}** trade(s) to process. Starting background analysis...")

    async def run_analysis_loop(self, ctx, all_trades, last_id):
        total_trades = len(all_trades)
        success_count = 0
        fallback_count = 0

        master_data = {"good_habits": [], "mistakes": []}
        if INSIGHTS_PATH.exists():
            try:
                with open(INSIGHTS_PATH, "r", encoding="utf-8") as f:
                    master_data = json.load(f)
            except Exception:
                pass

        status_msg = await ctx.send("🧪 Initializing analysis...")

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
                        with open(img_path, "rb") as f:
                            images_b64.append(base64.b64encode(f.read()).decode("utf-8"))

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

TASK:
Extract the specific 'good_habits' and 'mistakes' mentioned or visible in THIS SPECIFIC trade.
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
                    TRACKER_PATH.write_text(str(highest_id))

                await asyncio.sleep(0.5)

        master_data["good_habits"] = list(set(master_data["good_habits"]))
        master_data["mistakes"] = list(set(master_data["mistakes"]))

        with open(INSIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(master_data, f, indent=4, ensure_ascii=False)

        FAILED_TRADES_PATH.write_text("[]")

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

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Trades(bot))