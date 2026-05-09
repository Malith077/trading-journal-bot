import discord
import json
import aiohttp
import base64
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

    @commands.command(name="download_trades")
    async def download_trades(self, ctx, limit: int = 100):
        """
        Extracts trade screenshots from the current Discord channel 
        and saves them to the local downloads folder.
        """
        # Ensure the target directory exists on the Pi/Mac
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        
        count = 0
        status_msg = await ctx.send(f"📥 Scanning for trade screenshots in **#{ctx.channel.name}**...")

        async for message in ctx.channel.history(limit=limit):
            if message.attachments:
                for attachment in message.attachments:
                    # Filter for common image extensions
                    if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                        # Use message ID prefix to prevent filename collisions
                        file_name = f"{message.id}_{attachment.filename}"
                        file_path = TRADES_DIR / file_name
                        
                        # Only download if we don't already have it
                        if not file_path.exists():
                            await attachment.save(file_path)
                            count += 1

        await status_msg.edit(content=f"✅ Successfully extracted **{count}** new trade screenshots to `{TRADES_DIR}`.")

    @commands.command(name="sync_fractals")
    async def sync_fractals(self, ctx):
        """Scans the Fractal_Trades directory and reports new files."""
        if not TRADES_DIR.exists():
            await ctx.send(f"❌ Directory not found: `{TRADES_DIR}`")
            return

        files = list(TRADES_DIR.glob("*.png")) + list(TRADES_DIR.glob("*.jpg")) + list(TRADES_DIR.glob("*.webp"))
        
        last_analyzed = ""
        if TRACKER_PATH.exists():
            last_analyzed = TRACKER_PATH.read_text().strip()

        # Sort to find files lexicographically "newer" than the last tracked file
        new_files = [f for f in sorted(files) if f.name > last_analyzed]
        
        embed = discord.Embed(
            title="📂 Fractal Sync Report",
            description=f"Found **{len(files)}** total trade screenshots.",
            color=discord.Color.blue()
        )
        embed.add_field(name="New Trades", value=f"{len(new_files)} pending analysis", inline=True)
        embed.set_footer(text=f"Last synced: {datetime.datetime.now().strftime('%H:%M:%S')}")
        
        await ctx.send(embed=embed)

    @commands.command(name="analyze_trades")
    async def analyze_trades(self, ctx):
        """Processes new trade screenshots through Ollama Vision."""
        if not TRADES_DIR.exists():
            await ctx.send("❌ No trades directory found.")
            return

        all_files = sorted([f for f in TRADES_DIR.glob("*") if f.suffix.lower() in ['.png', '.jpg', '.jpeg', '.webp']])
        
        last_file = ""
        if TRACKER_PATH.exists():
            last_file = TRACKER_PATH.read_text().strip()

        to_process = [f for f in all_files if f.name > last_file]

        if not to_process:
            await ctx.send("✅ Everything is up to date. No new trades to analyze.")
            return

        status_msg = await ctx.send(f"🧪 Analyzing {len(to_process)} new trades with **{OLLAMA_MODEL}**...")

        master_insights = []
        if INSIGHTS_PATH.exists():
            try:
                with open(INSIGHTS_PATH, "r") as f:
                    master_insights = json.load(f)
            except json.JSONDecodeError:
                master_insights = []

        async with aiohttp.ClientSession() as session:
            for file_path in to_process:
                with open(file_path, "rb") as img_file:
                    img_b64 = base64.b64encode(img_file.read()).decode('utf-8')

                prompt = """
                Analyze this trading chart based on the TTrades Fractal Model.
                Identify:
                1. The Directional Bias (Bullish/Bearish).
                2. Key FVG (Fair Value Gaps) or SMT Divergence visible.
                3. The Change in State of Delivery (CISD).
                Provide a short, structured summary of the setup.
                """

                payload = {
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "images": [img_b64],
                    "stream": False
                }

                async with session.post(OLLAMA_API_URL, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        analysis = data.get("response", "No analysis returned.")
                        
                        master_insights.append({
                            "filename": file_path.name,
                            "date": datetime.datetime.now().isoformat(),
                            "analysis": analysis
                        })
                        
                        # Update the tracker with the last successfully processed filename
                        TRACKER_PATH.write_text(file_path.name)
                    else:
                        await ctx.send(f"⚠️ Failed to analyze `{file_path.name}` (Status: {resp.status})")

        # Save all results to the central JSON database
        with open(INSIGHTS_PATH, "w") as f:
            json.dump(master_insights, f, indent=4)

        await status_msg.edit(content=f"✅ Analysis complete. **{len(to_process)}** trades added to `master_insights.json`.")

async def setup(bot):
    await bot.add_cog(Trades(bot))