import discord
import json
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


class Trades(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="sync_fractals")
    async def sync_fractals(self, ctx):
        """Syncs all channels in the 'Fractal_Trades' category to local JSON + images."""
        target_category_name = "Fractal_Trades"
        category = discord.utils.get(ctx.guild.categories, name=target_category_name)

        if not category:
            await ctx.send(f"❌ Couldn't find category `{target_category_name}`.")
            return

        await ctx.send(f"📥 Starting flat JSON sync for **{target_category_name}**...")

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

                    for attachment in message.attachments:
                        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                            unique_filename = f"{message.id}_{attachment.filename}"
                            file_path = channel_path / unique_filename
                            await attachment.save(file_path)
                            msg_entry["images"].append(unique_filename)

                    channel_data.append(msg_entry)

            with open(channel_path / "history.json", "w", encoding="utf-8") as f:
                json.dump(channel_data, f, indent=4)

        await ctx.send("✅ Sync complete! Run `!analyze_trades` to process any new entries.")

    @commands.command(name="analyze_trades")
    async def analyze_trades(self, ctx):
        """Triggers the analysis loop as a background task to prevent Discord timeouts."""
        base_dir = TRADES_DIR

        if not base_dir.exists():
            await ctx.send("❌ No trades directory found. Run `!sync_fractals` first.")
            return

        # 1. Get last processed message ID
        last_id = 0
        if TRACKER_PATH.exists():
            try:
                content = TRACKER_PATH.read_text().strip()
                if content.isdigit():
                    last_id = int(content)
            except Exception:
                pass

        # 2. Gather NEW trades from history.json files (same logic as Script 1)
        new_trades = []
        for channel_dir in base_dir.iterdir():
            if channel_dir.is_dir():
                history_file = channel_dir / "history.json"
                if history_file.exists():
                    with open(history_file, "r", encoding="utf-8") as f:
                        channel_data = json.load(f)
                        for entry in channel_data:
                            if int(entry["message_id"]) > last_id:
                                entry["_folder_path"] = str(channel_dir)
                                new_trades.append(entry)

        if not new_trades:
            await ctx.send("✅ Master Insights is already up to date.")
            return

        # Start the background task so the bot stays responsive
        self.bot.loop.create_task(self.run_analysis_loop(ctx, new_trades, last_id))
        await ctx.send(f"🚀 Started background analysis for **{len(new_trades)}** trade(s). Monitoring progress...")

    async def run_analysis_loop(self, ctx, new_trades, last_id):
        """Internal loop that processes trades one-by-one with text + images (mirrors Script 1)."""
        total_trades = len(new_trades)
        success_count = 0
        fail_count = 0
        highest_id = last_id

        # Load current master insights
        master_data = {"good_habits": [], "mistakes": []}
        if INSIGHTS_PATH.exists():
            try:
                with open(INSIGHTS_PATH, "r") as f:
                    master_data = json.load(f)
            except (json.JSONDecodeError, Exception):
                pass

        status_msg = await ctx.send(f"🧠 Found {total_trades} new trade(s). Analyzing them one by one...")

        async with aiohttp.ClientSession() as session:
            for idx, trade in enumerate(new_trades, 1):
                trade_id = int(trade["message_id"])
                if trade_id > highest_id:
                    highest_id = trade_id

                # Encode images for THIS specific trade only (caps payload size)
                images_b64 = []
                for img_name in trade.get("images", []):
                    img_path = Path(trade["_folder_path"]) / img_name
                    if img_path.exists():
                        with open(img_path, "rb") as img_file:
                            images_b64.append(base64.b64encode(img_file.read()).decode("utf-8"))

                # Rich prompt: includes the trader's written notes AND charts (mirrors Script 1)
                prompt = f"""
Analyze this single trading journal entry and its attached chart(s).
The trader utilizes the TTrades Fractal Model (SMT, CISD, FVG).

Trade Notes:
{trade['content']}

TASK:
Extract the specific 'good_habits' and 'mistakes' mentioned or visible in THIS SPECIFIC trade.
Keep the insights concise and actionable.
If there are no mistakes, leave the mistakes array empty. If no good habits, leave it empty.

Respond STRICTLY in valid JSON format:
{{"good_habits": ["..."], "mistakes": ["..."]}}
"""

                payload = {
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "images": images_b64,
                    "stream": False
                }

                try:
                    async with session.post(OLLAMA_API_URL, json=payload, timeout=120) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            raw_text = result["response"]
                            cleaned_text = raw_text.replace("```json", "").replace("```", "").strip()
                            parsed = json.loads(cleaned_text)

                            master_data["good_habits"].extend(parsed.get("good_habits", []))
                            master_data["mistakes"].extend(parsed.get("mistakes", []))

                            success_count += 1
                            # Write tracker after each successful trade (crash-resilient)
                            TRACKER_PATH.write_text(str(highest_id))
                        else:
                            print(f"Trade {trade_id} failed with status: {resp.status}")
                            fail_count += 1
                except Exception as e:
                    print(f"Error parsing trade {trade_id}: {e}")
                    fail_count += 1

                # Update Discord status every 2 trades (matches Script 1 rhythm)
                if idx % 2 == 0 or idx == total_trades:
                    try:
                        await status_msg.edit(content=f"🧠 Analyzing trades... ({idx}/{total_trades} completed)")
                    except Exception:
                        pass

                # Let the event loop breathe between calls
                await asyncio.sleep(0.5)

        # Deduplicate (same as Script 1)
        master_data["good_habits"] = list(set(master_data["good_habits"]))
        master_data["mistakes"] = list(set(master_data["mistakes"]))

        # Final save
        with open(INSIGHTS_PATH, "w") as f:
            json.dump(master_data, f, indent=4)

        # Final confirmation embed (mirrors Script 1)
        embed = discord.Embed(
            title="✅ Analysis Complete",
            description=f"Successfully analyzed {success_count} trade(s).",
            color=discord.Color.green()
        )
        embed.add_field(name="Total Strengths in DB", value=str(len(master_data["good_habits"])), inline=True)
        embed.add_field(name="Total Mistakes in DB", value=str(len(master_data["mistakes"])), inline=True)
        if fail_count > 0:
            embed.add_field(name="Failed", value=str(fail_count), inline=True)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Trades(bot))