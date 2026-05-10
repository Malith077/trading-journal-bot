import discord
import aiohttp
import base64
from pathlib import Path
from discord.ext import commands
from config import OLLAMA_API_URL, OLLAMA_MODEL, KB_DIR
from services.rag_service import rag_service

class Knowledge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="extract_knowledge")
    async def extract_knowledge(self, ctx, *, channel_input: str = None):
        """Synthesize a channel into a Knowledge Article and index it for RAG."""
        target_channel = None
        
        # 1. Resolve the Target Channel
        if channel_input:
            try:
                target_channel = await commands.TextChannelConverter().convert(ctx, channel_input)
            except commands.ChannelNotFound:
                target_channel = discord.utils.get(ctx.guild.text_channels, name=channel_input)
        else:
            target_channel = ctx.channel

        if not target_channel:
            await ctx.send(f"❌ I couldn't find channel `{channel_input}`. Check permissions!")
            return

        status_msg = await ctx.send(f"📚 Gathering text and screenshots from **#{target_channel.name}**...")

        full_text_log = []
        images_b64 = []
        IMAGE_LIMIT = 10  # Limit to avoid payload size errors with Ollama

        # 2. Collect History
        async for message in target_channel.history(limit=500, oldest_first=True):
            # Collect Text
            if message.content and not message.author.bot:
                timestamp = message.created_at.strftime("%Y-%m-%d")
                full_text_log.append(f"[{timestamp}] {message.author.name}: {message.content}")
            
            # Collect Screenshots
            if message.attachments and len(images_b64) < IMAGE_LIMIT:
                for attachment in message.attachments:
                    if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                        img_bytes = await attachment.read()
                        images_b64.append(base64.b64encode(img_bytes).decode("utf-8"))

        if not full_text_log and not images_b64:
            await status_msg.edit(content="❌ No content found to analyze.")
            return

        combined_content = "\n".join(full_text_log)

        # 3. Build the Vision/Text Prompt
        prompt = f"""
        You are a Knowledge Management Expert specializing in Technical Trading Systems.
        I am providing you with chat history and screenshots from the channel '#{target_channel.name}'.
        
        TASK:
        1. Analyze the text AND the attached screenshots (charts/diagrams).
        2. Extract core trading logic, rules, and 'If/Then' scenarios visible in the images.
        3. Synthesize everything into a structured 'Knowledge Article' in Markdown.
        
        STRUCTURE:
        - # [Title: Strategy/Concept Name]
        - ## Summary: What is the core purpose of this concept?
        - ## Visual Identifiers: Describe exactly what the trader should look for on a chart.
        - ## Rules of Engagement: Step-by-step logic for using this knowledge.
        - ## Common Pitfalls: What to avoid (invalid setups).

        DATA:
        {combined_content}
        """

        # 4. API Call to Ollama
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "images": images_b64,
            "stream": False
        }

        await status_msg.edit(content=f"🧠 Processing {len(images_b64)} images and history with AI...")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(OLLAMA_API_URL, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        article_content = result["response"]

                        # 5. Save to Knowledge Base Directory
                        KB_DIR.mkdir(exist_ok=True)
                        file_name = f"{target_channel.name}_article.md"
                        file_path = KB_DIR / file_name
                        
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(article_content)

                        # 6. Index for RAG
                        await status_msg.edit(content="🚀 Article generated. Indexing into vector database...")
                        rag_service.index_markdown_file(file_path)

                        # 7. Final Confirmation
                        embed = discord.Embed(
                            title=f"📖 Knowledge Synthesized: #{target_channel.name}", 
                            color=discord.Color.blue()
                        )
                        embed.add_field(name="Source", value=f"#{target_channel.name}", inline=True)
                        embed.add_field(name="Status", value="Indexed for `!ask`", inline=True)
                        embed.set_footer(text=f"Saved to /knowledge_base/{file_name}")
                        await ctx.send(embed=embed)
                        await status_msg.delete()
                    else:
                        err_text = await response.text()
                        await ctx.send(f"❌ Ollama Error {response.status}: {err_text[:200]}")
            except Exception as e:
                await ctx.send(f"❌ Processing Error: {str(e)}")

async def setup(bot):
    await bot.add_cog(Knowledge(bot))