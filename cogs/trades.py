import discord
import json
import aiohttp
import base64
from pathlib import Path
from discord.ext import commands
from config import OLLAMA_API_URL, OLLAMA_MODEL, TRADES_DIR, INSIGHTS_PATH, TRACKER_PATH

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
            # We preserve your subfolder structure
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
        """Processes new trades and extracts 'good_habits' and 'mistakes' for the Morning Prep."""
        if not TRADES_DIR.exists():
            await ctx.send("❌ No trades directory found. Run `!sync_fractals` first.")
            return

        # 1. Get last processed ID
        last_id = 0
        if TRACKER_PATH.exists():
            content = TRACKER_PATH.read_text().strip()
            if content.isdigit():
                last_id = int(content)

        # 2. Find new files across all subfolders
        new_trades = []
        # We look for files where the prefix (Message ID) is > last_id
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

        # Sort chronologically by Message ID
        new_trades.sort(key=lambda x: x[0])

        # 3. Load existing Master Insights
        master_data = {"good_habits": [], "mistakes": []}
        if INSIGHTS_PATH.exists():
            try:
                with open(INSIGHTS_PATH, "r") as f:
                    master_data = json.load(f)
            except: pass

        status_msg = await ctx.send(f"🧠 Analyzing {len(new_trades)} new trade setups...")

        async with aiohttp.ClientSession() as session:
            highest_id = last_id
            for msg_id, img_path in new_trades:
                with open(img_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode('utf-8')

                prompt = """
                Analyze this trading chart (TTrades Fractal Model). 
                Extract specific 'good_habits' and 'mistakes'.
                Respond STRICTLY in valid JSON:
                {"good_habits": ["..."], "mistakes": ["..."]}
                """

                payload = {"model": OLLAMA_MODEL, "prompt": prompt, "images": [img_b64], "stream": False}

                async with session.post(OLLAMA_API_URL, json=payload) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        try:
                            # Clean potential markdown ticks from AI response
                            raw_json = res["response"].replace("```json", "").replace("```", "").strip()
                            parsed = json.loads(raw_json)
                            
                            master_data["good_habits"].extend(parsed.get("good_habits", []))
                            master_data["mistakes"].extend(parsed.get("mistakes", []))
                            
                            highest_id = max(highest_id, msg_id)
                            TRACKER_PATH.write_text(str(highest_id))
                        except:
                            print(f"Failed to parse AI response for {img_path.name}")

        # 4. Deduplicate and Save
        master_data["good_habits"] = list(set(master_data["good_habits"]))
        master_data["mistakes"] = list(set(master_data["mistakes"]))

        with open(INSIGHTS_PATH, "w") as f:
            json.dump(master_data, f, indent=4)

        await status_msg.edit(content=f"✅ Analysis complete. Added insights to `master_insights.json`.")

async def setup(bot):
    await bot.add_cog(Trades(bot))