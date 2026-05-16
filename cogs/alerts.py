"""
Alerts cog — listens for TradingView webhook events and posts
rich embeds to the #trading_alerts channel.
"""
import discord
from discord.ext import commands
from config import ALERTS_CHANNEL_NAME
from services.couchdb_service import couchdb_service


class Alerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_tradingview_alert(self, payload: dict):
        """Handle an incoming TradingView alert dispatched by the webhook server."""
        # --- Persistence ---
        await couchdb_service.save_alert(payload)

        message = payload.get("message", "Unknown signal")
        ticker = payload.get("ticker", "N/A")
        timeframe = payload.get("timeframe", "N/A")
        signal = payload.get("signal", "N/A")
        time_utc = payload.get("time_utc", "N/A")
        candle = payload.get("candle_signal", {})

        # Determine direction from signal string
        is_bullish = "BULLISH" in signal.upper()
        is_bearish = "BEARISH" in signal.upper()

        if is_bullish:
            color = discord.Color.green()
            emoji = "🟢"
        elif is_bearish:
            color = discord.Color.red()
            emoji = "🔴"
        else:
            color = discord.Color.greyple()
            emoji = "⚪"

        # Build the embed
        embed = discord.Embed(
            title=f"{emoji} {signal}",
            description=message,
            color=color
        )
        embed.add_field(name="Ticker", value=f"`{ticker}`", inline=True)
        embed.add_field(name="Timeframe", value=f"`{timeframe}`", inline=True)
        embed.add_field(name="Time (UTC)", value=f"`{time_utc}`", inline=True)

        if candle:
            ohlc = (
                f"**O:** {candle.get('o', 'N/A')}  "
                f"**H:** {candle.get('h', 'N/A')}  "
                f"**L:** {candle.get('l', 'N/A')}  "
                f"**C:** {candle.get('c', 'N/A')}"
            )
            embed.add_field(name="📊 Candle", value=ohlc, inline=False)

        embed.set_footer(text="TradingView Alert • Fractal Model")

        # Send to every guild's #trading_alerts channel
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=ALERTS_CHANNEL_NAME)
            
            if not channel:
                try:
                    channel = await guild.create_text_channel(name=ALERTS_CHANNEL_NAME)
                    print(f"✅ Created missing channel #{ALERTS_CHANNEL_NAME} in {guild.name}")
                except discord.Forbidden:
                    print(f"❌ Permission denied: Cannot create #{ALERTS_CHANNEL_NAME} in {guild.name}")
                    continue

            if channel:
                try:
                    await channel.send(embed=embed)
                except discord.Forbidden:
                    print(f"⚠️ Cannot send to #{ALERTS_CHANNEL_NAME} in {guild.name}")
                except discord.HTTPException as e:
                    print(f"⚠️ Failed to send alert in {guild.name}: {e}")


async def setup(bot):
    await bot.add_cog(Alerts(bot))
