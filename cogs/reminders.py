import discord
from pathlib import Path
from discord.ext import commands, tasks
from config import REMINDER_CHANNEL_ID, REMINDER_TIME

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

        # --- 1. Load Insights & Knowledge ---
        insights_path = Path.cwd() / "master_insights.json"
        kb_dir = Path.cwd() / "knowledge_base"
        
        # ... [Your logic for sampling habits and picking a random KB article] ...
        # (Simplified for brevity, use your existing random sample logic here)

        embed = discord.Embed(title="☀️ Morning Trading Prep", color=discord.Color.gold())
        # ... [Your existing embed building logic] ...
        
        await channel.send(embed=embed)

    @daily_reminder.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @commands.command()
    async def morning_prep(self, ctx):
        await ctx.send("🎲 Shuffling your trade history...")
        await self.daily_reminder()

async def setup(bot):
    await bot.add_cog(Reminders(bot))