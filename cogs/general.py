from discord.ext import commands

class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        await ctx.send('Pong! 🏓')

    @commands.command()
    async def list_channels(self, ctx):
        accessible = [c.name for c in ctx.guild.text_channels]
        await ctx.send(f"I can see **{len(accessible)}** channels. Check terminal for list.")
        print(f"Accessible: {accessible}")

async def setup(bot):
    await bot.add_cog(General(bot))