"""
Trading Bias Cog — manages daily asset bias tracking.
Prompts for daily bias at 7 AM AEST and persists state in CouchDB.
"""
import discord
import datetime
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo
from config import BIAS_CHANNEL_NAME, BIAS_ASSETS, BIAS_RESET_TIME
from services.couchdb_service import couchdb_service


class BiasView(discord.ui.View):
    """Interactive view for selecting trading bias for various assets."""
    def __init__(self, assets, state):
        super().__init__(timeout=None)  # Persistent view
        self.assets = assets
        self.state = state  # dict: {asset: bias}
        self.selected_asset = assets[0]
        self._update_select_options()

    def _update_select_options(self):
        """Update the select menu with asset options."""
        self.asset_select.options = [
            discord.SelectOption(
                label=asset, 
                value=asset, 
                default=(asset == self.selected_asset)
            ) for asset in self.assets
        ]

    def create_embed(self):
        """Create the status embed showing all current biases."""
        now = datetime.datetime.now(ZoneInfo("Australia/Melbourne"))
        embed = discord.Embed(
            title="📊 Daily Trading Bias",
            description=f"**Date:** {now.strftime('%A, %d %B %Y')}\nUpdate your bias for the assets below.",
            color=discord.Color.blue()
        )
        
        bias_list = []
        for asset in self.assets:
            bias = self.state.get(asset, "Neutral")
            emoji = "⚪"
            if bias == "Bullish": emoji = "🟢"
            elif bias == "Bearish": emoji = "🔴"
            bias_list.append(f"**{asset}:** {emoji} {bias}")
        
        embed.add_field(name="Current Biases", value="\n".join(bias_list), inline=False)
        embed.add_field(name="Editing", value=f"Currently updating: **{self.selected_asset}**", inline=False)
        embed.set_footer(text="Bias resets daily at 7 AM AEST")
        return embed

    @discord.ui.select(placeholder="Select Asset to Update", min_values=1, max_values=1, custom_id="bias_asset_select")
    async def asset_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        self.selected_asset = select.values[0]
        self._update_select_options()
        await interaction.edit_original_response(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Bullish", style=discord.ButtonStyle.success, emoji="🟢", custom_id="bias_bullish")
    async def bullish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_bias(interaction, "Bullish")

    @discord.ui.button(label="Bearish", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="bias_bearish")
    async def bearish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_bias(interaction, "Bearish")

    @discord.ui.button(label="Neutral", style=discord.ButtonStyle.secondary, emoji="⚪", custom_id="bias_neutral")
    async def neutral_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_bias(interaction, "Neutral")

    async def _update_bias(self, interaction: discord.Interaction, bias: str):
        await interaction.response.defer()
        self.state[self.selected_asset] = bias
        # Save to CouchDB
        now = datetime.datetime.now(ZoneInfo("Australia/Melbourne"))
        payload = {
            "date": now.strftime("%Y-%m-%d"),
            "biases": self.state,
            "updated_at": now.isoformat()
        }
        await couchdb_service.save_bias(payload)
        await interaction.edit_original_response(embed=self.create_embed(), view=self)


class Bias(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bias_reset_task.start()

    def cog_unload(self):
        self.bias_reset_task.cancel()

    @tasks.loop(time=BIAS_RESET_TIME)
    async def bias_reset_task(self):
        """Triggered daily at 7 AM AEST to post a fresh bias prompt."""
        print("⏰ 7 AM AEST: Resetting trading bias...")
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=BIAS_CHANNEL_NAME)
            if channel:
                await self.send_new_bias_prompt(channel)

    async def send_new_bias_prompt(self, channel):
        """Send a fresh bias prompt message."""
        # Initial state is all Neutral
        state = {asset: "Neutral" for asset in BIAS_ASSETS}
        view = BiasView(BIAS_ASSETS, state)
        await channel.send(embed=view.create_embed(), view=view)

    @commands.Cog.listener()
    async def on_ready(self):
        """Ensure persistent view is registered and check if today's prompt exists."""
        # Ensure channel exists in all guilds
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=BIAS_CHANNEL_NAME)
            if not channel:
                try:
                    await guild.create_text_channel(name=BIAS_CHANNEL_NAME)
                    print(f"✅ Created missing channel #{BIAS_CHANNEL_NAME} in {guild.name}")
                except discord.Forbidden:
                    print(f"❌ Permission denied: Cannot create #{BIAS_CHANNEL_NAME} in {guild.name}")

        # Registered persistent views must be added here
        now = datetime.datetime.now(ZoneInfo("Australia/Melbourne"))
        date_str = now.strftime("%Y-%m-%d")
        existing_data = await couchdb_service.get_bias_by_date(date_str)
        
        state = existing_data.get("biases", {asset: "Neutral" for asset in BIAS_ASSETS}) if existing_data else {asset: "Neutral" for asset in BIAS_ASSETS}
        
        self.bot.add_view(BiasView(BIAS_ASSETS, state))
        print(f"✅ Bias persistent view registered for {date_str}")

    @app_commands.command(name="bias", description="Manually trigger the daily bias prompt")
    async def bias_cmd(self, interaction: discord.Interaction):
        """Manually trigger the bias prompt."""
        # Find the target channel in the current guild
        channel = discord.utils.get(interaction.guild.text_channels, name=BIAS_CHANNEL_NAME)
        
        if not channel:
            # Create it if missing
            try:
                channel = await interaction.guild.create_text_channel(name=BIAS_CHANNEL_NAME)
            except discord.Forbidden:
                await interaction.response.send_message(f"❌ I don't have permission to create #{BIAS_CHANNEL_NAME}", ephemeral=True)
                return

        await self.send_new_bias_prompt(channel)
        await interaction.response.send_message(f"✅ New bias prompt generated in {channel.mention}", ephemeral=True)

    @commands.command(name="bias")
    async def bias_prefix_cmd(self, ctx: commands.Context):
        """Prefix command version of !bias"""
        channel = discord.utils.get(ctx.guild.text_channels, name=BIAS_CHANNEL_NAME)
        if not channel:
            try:
                channel = await ctx.guild.create_text_channel(name=BIAS_CHANNEL_NAME)
            except discord.Forbidden:
                await ctx.send("❌ I don't have permission to create the bias channel.")
                return

        await self.send_new_bias_prompt(channel)
        if ctx.channel.id != channel.id:
            await ctx.send(f"✅ New bias prompt generated in {channel.mention}", delete_after=10)


async def setup(bot):
    await bot.add_cog(Bias(bot))
