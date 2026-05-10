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
        if not channel: return

        # 1. Load random lessons from the dictionary structure
        good_sample, bad_sample = [], []
        if INSIGHTS_PATH.exists():
            with open(INSIGHTS_PATH, "r") as f:
                data = json.load(f)
                goods = data.get("good_habits", [])
                bads = data.get("mistakes", [])
                good_sample = random.sample(goods, min(3, len(goods)))
                bad_sample = random.sample(bads, min(3, len(bads)))

        # 2. Pick a random KB article
        kb_text = "No strategy notes found. Run `!extract_knowledge`."
        kb_title = "Strategy Spotlight"
        if KB_DIR.exists():
            articles = list(KB_DIR.glob("*.md"))
            if articles:
                choice = random.choice(articles)
                kb_title = choice.stem.replace("_", " ").title()
                with open(choice, "r") as f:
                    kb_text = f.read()[:500] + "..."

        # 3. Build Embed
        embed = discord.Embed(
            title="☀️ Morning Trading Prep",
            description=f"Focus for {datetime.date.today().strftime('%B %d')}:",
            color=discord.Color.gold()
        )

        if good_sample:
            embed.add_field(name="✅ Maintain These", value="\n".join([f"• {x}" for x in good_sample]), inline=False)
        if bad_sample:
            embed.add_field(name="⚠️ Watch For These", value="\n".join([f"• {x}" for x in bad_sample]), inline=False)
        
        embed.add_field(name=f"📖 Study: {kb_title}", value=f"```markdown\n{kb_text}\n```", inline=False)

        await channel.send(embed=embed)

    @daily_reminder.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @commands.command(name="morning_prep")
    async def morning_prep(self, ctx):
        """Manually trigger today's morning trading prep reminder."""
        await ctx.send("🎲 Shuffling your history...")
        await self.daily_reminder()

async def setup(bot):
    await bot.add_cog(Reminders(bot))