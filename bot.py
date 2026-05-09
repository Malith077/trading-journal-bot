import os
import discord
import datetime
from discord.ext import commands
from config import BOT_TOKEN, HEALTH_CHANNEL_ID, HEALTH_CHANNEL_NAME

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
    print(f'🚀 Logged in as {bot.user} | {datetime.datetime.now()}')
    
    target_channel = None

    # 1. Try to find the channel by ID first
    if HEALTH_CHANNEL_ID:
        target_channel = bot.get_channel(HEALTH_CHANNEL_ID)
    
    # 2. If ID is missing or invalid, search all visible channels by name
    if not target_channel:
        target_channel = discord.utils.get(bot.get_all_channels(), name=HEALTH_CHANNEL_NAME)

    # 3. Send the Heartbeat
    if target_channel:
        embed = discord.Embed(
            title="🔋 System Health Check",
            description="**New version deployed successfully.**\nAll modules loaded. I'm alive!",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now()
        )
        # Add a little technical flair
        embed.add_field(name="Environment", value="Raspberry Pi 5", inline=True)
        embed.add_field(name="Status", value="Online", inline=True)
        
        try:
            await target_channel.send(embed=embed)
            print(f"✅ Deployment heartbeat sent to #{target_channel.name}")
        except Exception as e:
            print(f"⚠️ Could not send to health channel: {e}")
    else:
        print(f"❌ Could not find a channel named '{HEALTH_CHANNEL_NAME}'")

    print('--------------------------')


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_KEY is missing! Check your .env file.")
    else:
        bot.run(BOT_TOKEN)