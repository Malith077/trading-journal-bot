import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Remove the default help command so ours takes over
        self.bot.remove_command("help")

    @app_commands.command(name="help", description="Shows all available commands.")
    async def custom_help(self, interaction: discord.Interaction):
        """Shows all available commands grouped by module."""
        embed = discord.Embed(
            title="📖 Command Reference",
            description="Here's everything I can do. Use `/` to run a command.",
            color=discord.Color.blurple()
        )

        for cmd in self.bot.tree.walk_commands():
            embed.add_field(
                name=f"/{cmd.name}",
                value=cmd.description or "No description",
                inline=False
            )

        embed.set_footer(text="Tip: Type / to see native Discord autocomplete.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def ping(self, interaction: discord.Interaction):
        """Check if the bot is alive."""
        await interaction.response.send_message('Pong! 🏓')

    @app_commands.command(name="list_channels", description="List all text channels the bot can see.")
    async def list_channels(self, interaction: discord.Interaction):
        """List all text channels the bot can see."""
        accessible = [c.name for c in interaction.guild.text_channels]
        await interaction.response.send_message(f"I can see **{len(accessible)}** channels. Check terminal for list.")
        print(f"Accessible: {accessible}")


async def setup(bot):
    await bot.add_cog(General(bot))