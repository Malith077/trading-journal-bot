"""
Edit the trade entry checklist from Discord instead of from code.

Post the checklist in #trade_checklist, one item per line, across as many
messages as you like. Edit a message to reword or reorder, delete one to drop
its items. The bot re-reads the channel after every change and confirms what
the checklist now is.

New trade channels get the updated list. Existing trade channels keep the list
they were created with, so ticking an old checklist never shows items it
didn't have.

Commands:
  /checklist_setup   Create #trade_checklist and post the current list to copy.
"""
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import cogs.trades as trades_mod
from services import checklist_service as cl
from services.guild_guard import is_allowed_guild

STATUS_TITLE = "📋 Checklist updated"
HISTORY_LIMIT = 200


def status_embed(items: list[str], overflow: int) -> discord.Embed:
    embed = discord.Embed(
        title=STATUS_TITLE,
        description="\n".join(f"**{i}.** {item}" for i, item in enumerate(items, 1)),
        color=discord.Color.green(),
    )
    if overflow:
        embed.add_field(
            name="⚠️ Too many items",
            value=(f"Only the first {cl.MAX_ITEMS} are used ({overflow} left out). "
                   "Discord allows 25 buttons on a message."),
            inline=False,
        )
    embed.set_footer(text="New trade channels use this list. Existing ones keep the list they started with.")
    return embed


def instructions_embed(items: list[str]) -> discord.Embed:
    listing = "\n".join(items) if items else "Rule one\nRule two"
    embed = discord.Embed(
        title="📋 Your trade entry checklist lives here",
        description=(
            "Post your checklist in this channel, **one item per line**. You can "
            "split it across several messages.\n\n"
            "• **Edit** a message to reword or reorder items\n"
            "• **Delete** a message to remove its items\n"
            "• Leading `-`, `1.` or `[ ]` markers are ignored\n\n"
            "Until you post here, the current list stays in use. To start from "
            "it, copy this into a message of your own:"
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(name="Current list", value=f"```\n{listing[:1000]}\n```", inline=False)
    return embed


class Checklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._synced_on_ready = False

    @staticmethod
    def is_checklist_channel(channel) -> bool:
        return (isinstance(channel, discord.TextChannel)
                and channel.name == cl.CHECKLIST_CHANNEL_NAME)

    def _is_own_status(self, message) -> bool:
        me = getattr(self.bot, "user", None)
        return (me is not None and message.author.id == me.id and bool(message.embeds)
                and (message.embeds[0].title or "").startswith(STATUS_TITLE))

    async def sync_from_channel(self, channel) -> list[str] | None:
        """
        Re-read the checklist channel and save it as the live list.

        Only your own messages count; the bot's instructions and confirmations
        are skipped. Returns the new list, or None if the channel has no items
        (the current list is then left alone rather than emptied).
        """
        async with self._lock:
            texts, old_status = [], []
            async for message in channel.history(limit=HISTORY_LIMIT, oldest_first=True):
                if message.author.bot:
                    if self._is_own_status(message):
                        old_status.append(message)
                    continue
                texts.append(message.content)

            items, overflow = cl.parse_items(texts)
            if not items:
                return None
            if items == trades_mod._load_checklist_items():
                return items

            await asyncio.to_thread(cl.save_live, items, trades_mod.LIVE_CHECKLIST_PATH)
            # One confirmation at the bottom, not a trail of them.
            for message in old_status:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            await channel.send(embed=status_embed(items, overflow))
            return items

    async def _maybe_sync(self, channel):
        if channel is None or not self.is_checklist_channel(channel):
            return
        if not is_allowed_guild(channel.guild):
            return
        try:
            await self.sync_from_channel(channel)
        except discord.HTTPException as e:
            print(f"[checklist] couldn't read #{channel.name}: {e}")

    # --- listeners ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        await self._maybe_sync(message.channel)

    # Raw events: edits and deletes of messages older than the bot's cache
    # (anything from before the last restart) only arrive this way.
    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        await self._maybe_sync(self.bot.get_channel(payload.channel_id))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        await self._maybe_sync(self.bot.get_channel(payload.channel_id))

    @commands.Cog.listener()
    async def on_ready(self):
        """Pick up anything changed in the channel while the bot was offline."""
        if self._synced_on_ready:
            return
        self._synced_on_ready = True
        for guild in self.bot.guilds:
            await self._maybe_sync(discord.utils.get(guild.text_channels,
                                                     name=cl.CHECKLIST_CHANNEL_NAME))

    # --- /checklist_setup ---

    @app_commands.command(name="checklist_setup",
                          description="Create #trade_checklist, where you edit the trade entry checklist.")
    async def checklist_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = discord.utils.get(interaction.guild.text_channels,
                                    name=cl.CHECKLIST_CHANNEL_NAME)
        if channel is None:
            try:
                channel = await interaction.guild.create_text_channel(cl.CHECKLIST_CHANNEL_NAME)
            except discord.Forbidden:
                await interaction.followup.send("❌ I don't have permission to create channels.")
                return
        await channel.send(embed=instructions_embed(trades_mod._load_checklist_items()))
        await interaction.followup.send(f"✅ Edit your checklist in {channel.mention}.")


async def setup(bot):
    await bot.add_cog(Checklist(bot))
