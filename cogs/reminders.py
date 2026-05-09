import discord
import json
import random
import datetime
from pathlib import Path
from discord.ext import commands, tasks
from config import REMINDER_CHANNEL_ID, REMINDER_TIME, INSIGHTS_PATH, KB_DIR

class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_reminder.start()

    def cog_unload(self):
        self.daily_reminder.cancel()

    @tasks.loop(time=REMINDER_TIME)
    async def daily_reminder(self):
        channel = self.bot.get_channel(REMINDER_CHANNEL_ID)
        if not channel:
            print(f"⚠️ Could not find reminder channel ID: {REMINDER_CHANNEL_ID}")
            return

        # --- 1. Sample Random Trade Insight ---
        random_insight = "No trade insights found yet. Run !analyze_trades first."
        if INSIGHTS_PATH.exists():
            try:
                with open(INSIGHTS_PATH, "r") as f:
                    insights = json.load(f)
                    if insights:
                        # Pick a random trade setup from your analyzed history
                        selected = random.choice(insights)
                        random_insight = f"**File:** `{selected['filename']}`\n{selected['analysis']}"
            except Exception as e:
                random_insight = f"Error loading insights: {e}"

        # --- 2. Pick a Random Knowledge Article ---
        kb_article = "No articles in knowledge_base yet."
        if KB_DIR.exists():
            articles = list(KB_DIR.glob("*.md"))
            if articles:
                # Select a random strategy note (e.g., T-Spot, SMT Divergence)
                choice = random.choice(articles)
                kb_article = f"📖 **{choice.stem.replace('_', ' ').title()}**\nUse `!ask` to dive deeper into this strategy."

        # --- 3. Build the Morning Prep Embed ---
        embed = discord.Embed(
            title="☀️ Morning Trading Prep",
            description=f"Good morning! Here is your curated prep for **{datetime.date.today().strftime('%A, %b %d')}**.",
            color=discord.Color.gold()
        )
        
        embed.add_field(
            name="📉 Historical Insight", 
            value=random_insight[:1024], # Discord limit safety
            inline=False
        )
        
        embed.add_field(
            name="🧠 Strategy Review", 
            value=kb_article, 
            inline=False
        )
        
        embed.add_field(
            name="✅ Pre-Market Checklist",
            value=(
                "• Check Economic Calendar (High Impact News)\n"
                "• Mark Daily/H4 Fair Value Gaps\n"
                "• Identify SMT Divergence on correlated pairs\n"
                "• Confirm Directional Bias"
            ),
            inline=False
        )
        
        embed.set_footer(text="Stick to the model. Trust the process.")
        embed.set_thumbnail(url="https://i.imgur.com/8E39pUn.png") # Optional placeholder for your bot logo

        await channel.send(embed=embed)

    @daily_reminder.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @commands.command(name="morning_prep")
    async def morning_prep(self, ctx):
        """Manually trigger the morning prep routine."""
        await ctx.send("🎲 Shuffling your trade history and strategies...")
        await self.daily_reminder()

async def setup(bot):
    await bot.add_cog(Reminders(bot))