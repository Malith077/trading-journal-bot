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

# In bot.py
@bot.event
async def on_ready():
    # Log to terminal for debugging in Tarneit
    print(f'🚀 Logged in as {bot.user} | {datetime.datetime.now()}')
    
    # 1. Get the notification channel from your config
    from config import REMINDER_CHANNEL_ID
    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    
    # 2. Send the "I'm Alive" heartbeat
    if channel:
        try:
            await channel.send("✨ **New version of me is deployed. I'm alive!**")
            print("✅ Deployment heartbeat sent to Discord.")
        except Exception as e:
            print(f"⚠️ Failed to send heartbeat: {e}")
            
    print('--------------------------')
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_KEY is missing! Check your .env file.")
    else:
        bot.run(BOT_TOKEN)