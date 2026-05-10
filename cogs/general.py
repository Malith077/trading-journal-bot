import discord
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Remove the default help command so ours takes over
        self.bot.remove_command("help")

    @commands.command(name="help")
    async def custom_help(self, ctx):
        """Shows all available commands grouped by module."""
        embed = discord.Embed(
            title="📖 Command Reference",
            description="Here's everything I can do. Use `!<command>` to run one.",
            color=discord.Color.blurple()
        )

        for cog_name, cog in sorted(self.bot.cogs.items()):
            # Get all non-hidden commands in this cog
            cog_commands = [cmd for cmd in cog.get_commands() if not cmd.hidden]
            if not cog_commands:
                continue

            lines = []
            for cmd in cog_commands:
                brief = cmd.help.split("\n")[0] if cmd.help else "No description"
                lines.append(f"`!{cmd.name}` — {brief}")

            embed.add_field(
                name=f"🔹 {cog_name}",
                value="\n".join(lines),
                inline=False
            )

        embed.set_footer(text="Tip: Type !help to see this again at any time.")
        await ctx.send(embed=embed)

    @commands.command()
    async def ping(self, ctx):
        """Check if the bot is alive."""
        await ctx.send('Pong! 🏓')

    @commands.command()
    async def list_channels(self, ctx):
        """List all text channels the bot can see."""
        accessible = [c.name for c in ctx.guild.text_channels]
        await ctx.send(f"I can see **{len(accessible)}** channels. Check terminal for list.")
        print(f"Accessible: {accessible}")


async def setup(bot):
    await bot.add_cog(General(bot))