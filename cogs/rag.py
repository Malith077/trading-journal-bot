import aiohttp
import discord
from discord.ext import commands
from scripts.reindex_all import flush_and_reindex
from services.rag_service import rag_service
from config import OLLAMA_API_URL, OLLAMA_MODEL

class RAG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="reindex")
    @commands.has_permissions(administrator=True)
    async def manual_reindex(self, ctx):
        """Force a full wipe and rebuild of the knowledge base."""
        await ctx.send("🧹 Wiping vector database and re-indexing all markdown files...")
        
        try:
            flush_and_reindex()
            await ctx.send("✅ Re-indexing successful! I am now up to date with the /knowledge_base folder.")
        except Exception as e:
            await ctx.send(f"❌ Re-indexing failed: {str(e)}")


    @commands.command(name="ask")
    async def ask_bot(self, ctx, *, question: str):
        """Queries the knowledge base and shows sources used."""
        status_msg = await ctx.send(f"🔎 Searching knowledge for: `{question}`...")

        # 1. Retrieve context and the list of source files
        context, sources = rag_service.query_knowledge(question)
        
        if not context:
            await status_msg.edit(content="I couldn't find any specific knowledge on that topic.")
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
                    
                    await ctx.send(embed=embed)
                    await status_msg.delete()
                else:
                    await status_msg.edit(content="❌ Error contacting the AI brain.")

async def setup(bot):
    await bot.add_cog(RAG(bot))