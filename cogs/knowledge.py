import discord
import base64
import asyncio
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from config import KB_DIR
from services.rag_service import rag_service
from services.llm_service import llm_service, LLMError, to_data_uri

ARTICLE_SUFFIX = "_article.md"
IMAGE_LIMIT = 10        # Keep the request well inside the provider size cap
HISTORY_LIMIT = 500


def article_path(channel_name: str) -> Path:
    """Where the knowledge article for a channel lives."""
    return KB_DIR / f"{channel_name}{ARTICLE_SUFFIX}"


def source_channel_name(article: Path) -> str | None:
    """The channel an article was generated from, or None for hand-written files."""
    name = Path(article).name
    return name[: -len(ARTICLE_SUFFIX)] if name.endswith(ARTICLE_SUFFIX) else None


def build_article_prompt(channel_name: str, combined_content: str) -> str:
    return f"""
        You are a Knowledge Management Expert specializing in Technical Trading Systems.
        I am providing you with chat history and screenshots from the channel '#{channel_name}'.
        
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


async def gather_channel(channel) -> tuple[list[str], list[str]]:
    """Collect human-written text and up to IMAGE_LIMIT chart images from a channel."""
    full_text_log = []
    images = []
    async for message in channel.history(limit=HISTORY_LIMIT, oldest_first=True):
        if message.content and not message.author.bot:
            timestamp = message.created_at.strftime("%Y-%m-%d")
            full_text_log.append(f"[{timestamp}] {message.author.name}: {message.content}")

        if message.attachments and len(images) < IMAGE_LIMIT:
            for attachment in message.attachments:
                if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                    img_bytes = await attachment.read()
                    raw_b64 = base64.b64encode(img_bytes).decode("utf-8")
                    images.append(to_data_uri(raw_b64, attachment.filename))
    return full_text_log, images


async def write_article(channel_name: str, content: str) -> Path:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    path = article_path(channel_name)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    return path


class Knowledge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def generate_article(self, channel) -> tuple[Path, int] | None:
        """
        Synthesize a channel into a knowledge article and save it, without
        indexing. Returns (path, image_count), or None if the channel has no
        usable content. Raises LLMError if the model call fails.

        Shared by /extract_knowledge and the full rebuild, which indexes every
        article in one pass at the end instead of one at a time.
        """
        text_log, images = await gather_channel(channel)
        if not text_log and not images:
            return None
        prompt = build_article_prompt(channel.name, "\n".join(text_log))
        content = await llm_service.complete(prompt, images=images)
        return await write_article(channel.name, content), len(images)

    @app_commands.command(name="extract_knowledge", description="Synthesize a channel into a Knowledge Article and index it.")
    @app_commands.describe(target_channel="The channel to extract knowledge from. Defaults to the current channel.")
    async def extract_knowledge(self, interaction: discord.Interaction, target_channel: discord.TextChannel = None):
        """Synthesize a channel into a Knowledge Article and index it for RAG."""
        await interaction.response.defer(thinking=True)

        if not target_channel:
            target_channel = interaction.channel

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.edit_original_response(content="❌ This command can only be used on Text Channels.")
            return

        await interaction.edit_original_response(content=f"📚 Gathering text and screenshots from **#{target_channel.name}**, then processing with AI...")

        try:
            result = await self.generate_article(target_channel)
        except LLMError as e:
            await interaction.edit_original_response(content=f"❌ AI Error: {str(e)[:300]}")
            return
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Processing Error: {str(e)}")
            return

        if result is None:
            await interaction.edit_original_response(content="❌ No content found to analyze.")
            return
        file_path, _ = result

        try:
            await interaction.edit_original_response(content="🚀 Article generated. Indexing into vector database...")
            await asyncio.to_thread(rag_service.index_markdown_file, file_path)

            embed = discord.Embed(
                title=f"📖 Knowledge Synthesized: #{target_channel.name}",
                color=discord.Color.blue()
            )
            embed.add_field(name="Source", value=f"#{target_channel.name}", inline=True)
            embed.add_field(name="Status", value="Indexed for `/ask`", inline=True)
            embed.set_footer(text=f"Saved to /knowledge_base/{file_path.name}")
            await interaction.edit_original_response(content=None, embed=embed)
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Processing Error: {str(e)}")

async def setup(bot):
    await bot.add_cog(Knowledge(bot))
