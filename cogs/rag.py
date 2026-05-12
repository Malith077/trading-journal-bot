import aiohttp
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from scripts.reindex_all import flush_and_reindex
from services.rag_service import rag_service
from config import OLLAMA_API_URL, OLLAMA_MODEL

class RAG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="reindex", description="Force a full wipe and rebuild of the knowledge base.")
    @app_commands.default_permissions(administrator=True)
    async def manual_reindex(self, interaction: discord.Interaction):
        """Force a full wipe and rebuild of the knowledge base."""
        await interaction.response.defer()
        await interaction.edit_original_response(content="🧹 Wiping vector database and re-indexing all markdown files...")
        
        try:
            await asyncio.to_thread(flush_and_reindex)
            await interaction.edit_original_response(content="✅ Re-indexing successful! I am now up to date with the /knowledge_base folder.")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Re-indexing failed: {str(e)}")


    @app_commands.command(name="ask", description="Queries the knowledge base and shows sources used.")
    @app_commands.describe(question="The question to ask the knowledge base.")
    async def ask_bot(self, interaction: discord.Interaction, question: str):
        """Queries the knowledge base and shows sources used."""
        await interaction.response.defer(thinking=True)
        await interaction.edit_original_response(content=f"🔎 Searching knowledge for: `{question}`...")

        # 1. Retrieve context and the list of source files
        context, sources = await asyncio.to_thread(rag_service.query_knowledge, question)
        
        if not context:
            await interaction.edit_original_response(content="I couldn't find any specific knowledge on that topic.")
            return

        # 2. Build the RAG Prompt
        prompt = f"""
        You are an expert analyst for the TTrades Fractal Model.
        Below is context from the user's trading journals. 

        QUESTION: {question}

        CONTEXT:
        {context}

        INSTRUCTION:
        Analyze the context deeply. Even if the exact term "{question}" isn't used, 
        look for synonyms or visual descriptions that match the concept. 
        If the context mentions setups, levels, or rules related to the query, 
        summarize them as the answer.
        """

        # 3. Call Ollama
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
        async with aiohttp.ClientSession() as session:
            async with session.post(OLLAMA_API_URL, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    answer = result["response"]
                    
                    # 4. Build a clean Embed with Sources
                    embed = discord.Embed(
                        title="🧠 AI Strategy Insight",
                        description=answer[:4000], # Discord limit safety
                        color=discord.Color.green()
                    )
                    
                    # List which files were actually pulled from ChromaDB
                    source_list = "\n".join([f"• `{s}`" for s in sources])
                    embed.add_field(name="📚 Sources Used", value=source_list, inline=False)
                    
                    await interaction.edit_original_response(content=None, embed=embed)
                else:
                    await interaction.edit_original_response(content="❌ Error contacting the AI brain.")

async def setup(bot):
    await bot.add_cog(RAG(bot))