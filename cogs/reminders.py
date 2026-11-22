import discord
import json
import random
import aiofiles
import datetime
from pathlib import Path
from discord import app_commands
from discord.ext import commands, tasks
from config import REMINDER_CHANNEL_ID, REMINDER_TIME, INSIGHTS_PATH, MISSED_INSIGHTS_PATH, KB_DIR
from services.schedule_utils import is_trading_day
from services import psych_service as ps
from config import PSYCH_LOG_PATH

class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_reminder.start()

    def cog_unload(self):
        self.daily_reminder.cancel()

    @tasks.loop(time=REMINDER_TIME)
    async def daily_reminder(self):
        # Skip weekends — no scheduled morning prep when markets are closed.
        # (Manual /morning_prep bypasses this via _send_reminder.)
        if not is_trading_day():
            return
        await self._send_reminder()

    async def _send_reminder(self):
        channel = self.bot.get_channel(REMINDER_CHANNEL_ID)
        if not channel: return

        # 1. Load random lessons from the dictionary structure
        good_sample, bad_sample = [], []
        if INSIGHTS_PATH.exists():
            async with aiofiles.open(INSIGHTS_PATH, "r") as f:
                data = json.loads(await f.read())
                goods = data.get("good_habits", [])
                bads = data.get("mistakes", [])
                good_sample = random.sample(goods, min(3, len(goods)))
                bad_sample = random.sample(bads, min(3, len(bads)))

        # 2. Load missed-trade lessons (tracked separately from taken trades)
        missed_sample = []
        if MISSED_INSIGHTS_PATH.exists():
            try:
                async with aiofiles.open(MISSED_INSIGHTS_PATH, "r") as f:
                    missed_data = json.loads(await f.read())
                # Action items are the actionable half of a missed trade; fall
                # back to the raw reasons if the model produced none.
                pool = missed_data.get("action_items", []) or missed_data.get("miss_reasons", [])
                missed_sample = random.sample(pool, min(2, len(pool)))
            except (json.JSONDecodeError, Exception):
                pass

        # 3. Yesterday's closing emotional read, if there was one.
        # Shull's point: you carry it into today whether or not you name it.
        carry_over = None
        states = await ps.read_states(PSYCH_LOG_PATH)
        if states:
            last = states[-1]
            bits = []
            if last.get("emotional_context"):
                bits.append(", ".join(
                    ps.EMOTION_LABELS.get(e, e) for e in last["emotional_context"]
                ))
            if last.get("fear_spectrum") is not None:
                bits.append(ps.fear_label(last["fear_spectrum"]))
            if bits:
                carry_over = " · ".join(bits)

        # 4. Pick a random KB article
        kb_text = "No strategy notes found. Run `!extract_knowledge`."
        kb_title = "Strategy Spotlight"
        if KB_DIR.exists():
            articles = list(KB_DIR.glob("*.md"))
            if articles:
                choice = random.choice(articles)
                kb_title = choice.stem.replace("_", " ").title()
                async with aiofiles.open(choice, "r") as f:
                    kb_text = (await f.read())[:500] + "..."

        # 5. Build Embed
        embed = discord.Embed(
            title="☀️ Morning Trading Prep",
            description=f"Focus for {datetime.date.today().strftime('%B %d')}:",
            color=discord.Color.gold()
        )

        if good_sample:
            embed.add_field(name="✅ Maintain These", value="\n".join([f"• {x}" for x in good_sample]), inline=False)
        if bad_sample:
            embed.add_field(name="⚠️ Watch For These", value="\n".join([f"• {x}" for x in bad_sample]), inline=False)
        
        if missed_sample:
            embed.add_field(name="🎯 Don't Miss These Again", value="\n".join([f"• {x}" for x in missed_sample]), inline=False)

        if carry_over:
            embed.add_field(
                name="🧠 You closed yesterday feeling",
                value=f"{carry_over}\n*Name it now, or it names your first trade.* Run `/state` when you sit down.",
                inline=False
            )

        embed.add_field(name=f"📖 Study: {kb_title}", value=f"```markdown\n{kb_text}\n```", inline=False)

        await channel.send(embed=embed)

    @daily_reminder.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="morning_prep", description="Manually trigger today's morning trading prep reminder.")
    async def morning_prep(self, interaction: discord.Interaction):
        """Manually trigger today's morning trading prep reminder."""
        await interaction.response.send_message("🎲 Shuffling your history...")
        await self._send_reminder()

async def setup(bot):
    await bot.add_cog(Reminders(bot))