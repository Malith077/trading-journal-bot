import os
import discord
import datetime
from discord import app_commands
from discord.ext import commands
from config import BOT_TOKEN, HEALTH_CHANNEL_ID, HEALTH_CHANNEL_NAME, DEV_GUILD_ID
from webhook_server import start_webhook_server
from services.couchdb_service import couchdb_service
from services.llm_service import llm_service
from services.guild_guard import is_allowed_guild, is_locked, describe_lock

class GuardedCommandTree(app_commands.CommandTree):
    """Refuses slash commands from guilds outside the dev lock (see guild_guard)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_allowed_guild(interaction.guild):
            return True
        await interaction.response.send_message(
            "🔒 This instance is locked to a development server and won't act here.",
            ephemeral=True
        )
        return False


class TradingAssistant(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix='!', intents=intents, tree_cls=GuardedCommandTree)
        self._webhook_runner = None

    async def setup_hook(self):
        # Automatically load all cogs in the cogs/ folder
        # Using a relative path based on this file's location is safer
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        
        for filename in os.listdir(cogs_dir):
            if filename.endswith('.py') and filename != "__init__.py":
                await self.load_extension(f'cogs.{filename[:-3]}')
        
        # Guild-scoped sync lands immediately; global sync can take up to an
        # hour to propagate, which is unusable for local testing.
        if DEV_GUILD_ID:
            await self._sync_dev_guild()
        else:
            synced = await self.tree.sync()
            print(f"✅ All Cogs loaded. Synced {len(synced)} command(s) globally "
                  f"(may take up to 1 hour to appear — set DEV_GUILD_ID for instant sync).")

        # Start the webhook server for TradingView alerts
        self._webhook_runner = await start_webhook_server(self)

    async def _sync_dev_guild(self):
        """
        Register commands in the dev guild, explaining any failure instead of
        crashing on it.

        A 50001 here is always a setup problem — the bot isn't in that guild, or
        it was invited without the `applications.commands` scope. Neither is
        worth a traceback, and letting startup continue means on_ready can print
        the guild list, which distinguishes the two cases.
        """
        guild = discord.Object(id=DEV_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.Forbidden:
            print(f"\n❌ Can't register slash commands in guild {DEV_GUILD_ID} "
                  f"(403 Missing Access).\n"
                  f"   This is one of two things:\n"
                  f"     1. The bot isn't in that server at all, or\n"
                  f"     2. It was invited WITHOUT the `applications.commands` scope.\n"
                  f"        Inviting with only `bot` puts it in the server but blocks\n"
                  f"        slash-command registration — this is the usual cause.\n"
                  f"   Re-invite it with BOTH scopes:\n"
                  f"     {self.invite_url()}\n"
                  f"   The guild list printed below tells you which case you're in.\n"
                  f"   Starting anyway; slash commands won't be available.\n")
            return
        except discord.HTTPException as e:
            print(f"\n❌ Command sync failed for guild {DEV_GUILD_ID}: {e}\n"
                  f"   Starting anyway; slash commands won't be available.\n")
            return

        print(f"✅ All Cogs loaded. Synced {len(synced)} command(s) to dev guild "
              f"{DEV_GUILD_ID} (instant).")

    def invite_url(self) -> str:
        """An invite link for this application with the scopes/permissions it needs."""
        # View + Manage Channels, Send + Manage Messages, Embed, Attach, Read History.
        permissions = 125968
        app_id = self.application_id or "YOUR_APP_ID"
        return (f"https://discord.com/api/oauth2/authorize?client_id={app_id}"
                f"&permissions={permissions}&scope=bot%20applications.commands")

    async def close(self):
        """Clean up services before shutting down."""
        if self._webhook_runner:
            await self._webhook_runner.cleanup()
        await couchdb_service.close()
        await llm_service.close()
        await super().close()

bot = TradingAssistant()

# Discord routinely invalidates gateway sessions for load balancing, and
# discord.py re-identifies on its own. That logs identically to a genuine
# failure loop (bad token, missing intents), so make the healthy case say so.
@bot.event
async def on_resumed():
    print(f"🔄 Gateway session resumed at {datetime.datetime.now():%H:%M:%S} — "
          f"this is normal; Discord recycles sessions periodically.")


@bot.event
async def on_disconnect():
    # Only interesting if it does not come back; on_ready/on_resumed follows.
    print(f"🔌 Gateway disconnected at {datetime.datetime.now():%H:%M:%S} — reconnecting...")


@bot.event
async def on_ready():
    # on_ready fires again after every full re-identify, not just at startup.
    if getattr(bot, "_ready_once", False):
        print(f"✅ Reconnected at {datetime.datetime.now():%H:%M:%S} as {bot.user}.")
        return
    bot._ready_once = True

    print(f'🚀 Logged in as {bot.user} | {datetime.datetime.now()}')

    # Which guilds this instance will actually touch. Running a production
    # token locally shows up here as a list of guilds being ignored.
    print(describe_lock(bot.guilds))
    
    # Check Database connection
    await couchdb_service.check_connection()

    target_channel = None

    # A locked dev instance must not announce itself in the production server.
    if is_locked():
        print("🔕 Guild lock active — skipping the deployment heartbeat.")
        print('--------------------------')
        return

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