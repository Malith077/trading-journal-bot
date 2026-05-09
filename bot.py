import os
import discord
import datetime
from discord.ext import commands
from config import BOT_TOKEN

class TradingAssistant(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        # Automatically load all cogs in the cogs/ folder
        # Using a relative path based on this file's location is safer
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        
        for filename in os.listdir(cogs_dir):
            if filename.endswith('.py') and filename != "__init__.py":
                await self.load_extension(f'cogs.{filename[:-3]}')
        
        print("✅ All Cogs loaded successfully.")

bot = TradingAssistant()

@bot.event
async def on_ready():
    # Added datetime import above to prevent the next NameError
    print(f'Logged in as {bot.user} | Tarneit Local Time: {datetime.datetime.now()}')
    print('--------------------------')

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_KEY is missing! Check your .env file.")
    else:
        bot.run(BOT_TOKEN)