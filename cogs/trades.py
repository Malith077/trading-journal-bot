import discord
import json
import aiohttp
import base64
import asyncio
import datetime
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
        """Syncs all channels in the 'Fractal_Trades' category to local storage."""
        target_category_name = "Fractal_Trades"
        category = discord.utils.get(ctx.guild.categories, name=target_category_name)
        
        if not category:
            await ctx.send(f"❌ Couldn't find category `{target_category_name}`.")
            return

        await ctx.send(f"📥 Syncing all channels in **{target_category_name}**...")
        
        count = 0
        for channel in category.text_channels:
            channel_path = TRADES_DIR / channel.name
            channel_path.mkdir(parents=True, exist_ok=True)
            
            async for message in channel.history(limit=None, oldest_first=True):
                if message.attachments:
                    for attachment in message.attachments:
                        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                            unique_filename = f"{message.id}_{attachment.filename}"
                            file_path = channel_path / unique_filename
                            if not file_path.exists():
                                await attachment.save(file_path)
                                count += 1

        await ctx.send(f"✅ Sync complete! Downloaded **{count}** new screenshots.")

    @commands.command(name="analyze_trades")
    async def analyze_trades(self, ctx):
        """Triggers the analysis loop as a background task to prevent Discord timeouts."""
        if not TRADES_DIR.exists():
            await ctx.send("❌ No trades directory found. Run `!sync_fractals` first.")
            return

        # 1. Get last processed ID
        last_id = 0
        if TRACKER_PATH.exists():
            content = TRACKER_PATH.read_text().strip()
            if content.isdigit():
                last_id = int(content)

        # 2. Find new files
        new_trades = []
        for img_path in TRADES_DIR.rglob("*"):
            if img_path.is_file() and "_" in img_path.name:
                try:
                    msg_id = int(img_path.name.split("_")[0])
                    if msg_id > last_id:
                        new_trades.append((msg_id, img_path))
                except ValueError:
                    continue

        if not new_trades:
            await ctx.send("✅ Master Insights is already up to date.")
            return

        new_trades.sort(key=lambda x: x[0])
        
        # Start the background task so the bot stays responsive
        self.bot.loop.create_task(self.run_analysis_loop(ctx, new_trades, last_id))
        await ctx.send(f"🚀 Started background analysis for **{len(new_trades)}** trades. Monitoring progress...")

    async def run_analysis_loop(self, ctx, new_trades, last_id):
        """Internal loop that handles the cloud API calls without blocking the bot."""
        total_trades = len(new_trades)
        success_count = 0
        fail_count = 0
        
        master_data = {"good_habits": [], "mistakes": []}
        if INSIGHTS_PATH.exists():
            try:
                with open(INSIGHTS_PATH, "r") as f:
                    master_data = json.load(f)
            except:
                pass

        status_msg = await ctx.send("🧪 Initializing analysis...")

        async with aiohttp.ClientSession() as session:
            highest_id = last_id
            for idx, (msg_id, img_path) in enumerate(new_trades, 1):
                # Update status message
                try:
                    await status_msg.edit(content=(
                        f"📊 **Batch Progress:** `{idx}/{total_trades}`\n"
                        f"🖼️ **Current:** `{img_path.name}`\n"
                        f"✅ **Success:** `{success_count}` | ❌ **Failed:** `{fail_count}`"
                    ))
                except:
                    pass

                try:
                    with open(img_path, "rb") as f:
                        img_b64 = base64.b64encode(f.read()).decode('utf-8')

                    prompt = """
                    Analyze this trading chart (TTrades Fractal Model). 
                    Extract specific 'good_habits' and 'mistakes'.
                    Respond STRICTLY in valid JSON:
                    {"good_habits": ["..."], "mistakes": ["..."]}
                    """

                    payload = {
                        "model": OLLAMA_MODEL, 
                        "prompt": prompt, 
                        "images": [img_b64], 
                        "stream": False
                    }

                    # We use a generous timeout for cloud uploads
                    async with session.post(OLLAMA_API_URL, json=payload, timeout=120) as resp:
                        if resp.status == 200:
                            res = await resp.json()
                            raw_json = res["response"].replace("```json", "").replace("```", "").strip()
                            parsed = json.loads(raw_json)
                            
                            master_data["good_habits"].extend(parsed.get("good_habits", []))
                            master_data["mistakes"].extend(parsed.get("mistakes", []))
                            
                            success_count += 1
                            highest_id = max(highest_id, msg_id)
                            TRACKER_PATH.write_text(str(highest_id))
                        else:
                            fail_count += 1
                except Exception as e:
                    print(f"Error on {img_path.name}: {e}")
                    fail_count += 1

                # Small sleep to let the event loop breathe
                await asyncio.sleep(0.5)

        # Final Save
        master_data["good_habits"] = list(set(master_data["good_habits"]))
        master_data["mistakes"] = list(set(master_data["mistakes"]))
        with open(INSIGHTS_PATH, "w") as f:
            json.dump(master_data, f, indent=4)

        await ctx.send(f"✅ **Analysis Complete!**\nIndexed {success_count} trades, {fail_count} failed.")

async def setup(bot):
    await bot.add_cog(Trades(bot))