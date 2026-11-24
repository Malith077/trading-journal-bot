"""
#coach — an ongoing conversation with a coach that knows your trades.

Every message in the channel named COACH_CHAT_CHANNEL_NAME is a turn. The coach
looks up your journal through tools (services/coach_chat_service.py), keeps
notes about you, and remembers earlier conversations.

Commands:
  /coach_chat_setup   Create the #coach channel.
  /coach_notes        What the coach has chosen to remember about you.
  /coach_forget       Delete one of those notes.
"""
import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from config import COACH_CHAT_CHANNEL_NAME, COACH_CHAT_USER_ID, DEEPSEEK_CHAT_EFFORT
from services import coach_chat_service as ccs
from services import coach_service as cs
from services import deepseek_hours as dh
from services.coach_service import CoachError
from services.guild_guard import is_allowed_guild
from services.llm_service import llm_service, LLMError

# Lookups per reply before the coach must answer with what it has.
MAX_TOOL_ROUNDS = 8
MAX_REPLY_TOKENS = 16000
DISCORD_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on paragraph, then line, then word boundaries to fit Discord."""
    chunks = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def peak_line(now: dt.datetime, cost: float, first_in_window: bool) -> str:
    """The peak-pricing notice: in full once per window, then as a short footer."""
    ends = dh.next_off_peak(now)
    if first_in_window:
        return (f"⏱️ **DeepSeek peak pricing (2×) is on until {dh.discord_time(ends, 't')}** "
                f"({dh.discord_time(ends, 'R')}). This reply cost ≈ ${cost:.3f}; "
                "anything that can wait is half price after that.")
    return f"-# ⏱️ peak pricing · ≈ ${cost:.3f}"


class CoachChat(commands.Cog):
    def __init__(self, bot, memory=None, index=None, now=None):
        self.bot = bot
        self.memory = memory or ccs.ChatMemory()
        self.index = index or ccs.TradeIndex()
        self._now = now or dh.utcnow
        # One conversation, one memory: answer strictly in order.
        self._lock = asyncio.Lock()
        self._corpus = None
        self._corpus_error = None
        try:
            self._corpus = cs.load_corpus()
        except CoachError as e:
            self._corpus_error = str(e)
            print(f"❌ Coach chat disabled: {e}")

    # --- filters ---

    def _bot_names(self) -> set[str]:
        user = getattr(self.bot, "user", None)
        return {user.name} if user and getattr(user, "name", None) else set()

    @staticmethod
    def is_coach_channel(channel) -> bool:
        return getattr(channel, "name", None) == COACH_CHAT_CHANNEL_NAME

    def _should_answer(self, message: discord.Message) -> bool:
        if message.author.bot or not message.guild:
            return False
        if not self.is_coach_channel(message.channel):
            return False
        if not is_allowed_guild(message.guild):
            return False
        if COACH_CHAT_USER_ID and message.author.id != COACH_CHAT_USER_ID:
            return False
        return True

    # --- the conversation ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self._should_answer(message):
            return
        if not self._corpus:
            await message.channel.send(f"❌ The coach is disabled: {self._corpus_error}")
            return

        text = (message.content or "").strip()
        if not text:
            if message.attachments:
                await message.channel.send(
                    "I can't see images in here. Tell me what you're looking at, "
                    "or point me at the trade channel and I'll read your notes.")
            return

        async with self._lock:
            async with message.channel.typing():
                try:
                    reply, footer = await self.respond(text, message.created_at)
                except LLMError as e:
                    print(f"[coach_chat] reply failed: {e}")
                    await message.channel.send(f"❌ I couldn't reply just now ({str(e)[:200]}). "
                                               "Nothing was saved, so just send it again.")
                    return

            for chunk in split_message(f"{reply}\n{footer}" if footer else reply):
                await message.channel.send(chunk)

            await self.fold_memory()

    async def respond(self, text: str, sent_at) -> tuple[str, str]:
        """
        Answer one message. Returns (reply, footer). The exchange is saved only
        when a reply was produced, so a failed call leaves memory untouched.
        """
        now = ccs.to_utc(self._now())
        sent_at = ccs.to_utc(sent_at) or now
        memory = await self.memory.load()

        user_message = {"role": "user", "content": ccs.stamp_user_message(text, sent_at)}
        messages = ([{"role": "system", "content": ccs.build_system_prompt(self._corpus, memory)}]
                    + await self.memory.replay() + [user_message])
        new_messages = [user_message]
        usages = []
        tools = ccs.CoachTools(self.memory, self.index, now, self._bot_names(),
                               knowledge_search=self._search_knowledge)

        reply = ""
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            final = round_no == MAX_TOOL_ROUNDS
            response = await llm_service.chat(
                list(messages), tools=ccs.TOOLS, tool_choice="none" if final else None,
                reasoning_effort=DEEPSEEK_CHAT_EFFORT, max_tokens=MAX_REPLY_TOKENS)
            usages.append(response.get("usage") or {})

            # On the forced last round a stray tool call can't be answered, and an
            # unanswered tool call would break every later request, so drop it.
            calls = [] if final else response.get("tool_calls") or []
            assistant = ccs.api_message(response, drop_tool_calls=not calls)
            messages.append(assistant)
            new_messages.append(assistant)

            if not calls:
                reply = (response.get("content") or "").strip()
                break

            for call in calls:
                function = call.get("function") or {}
                result = await tools.run(function.get("name", ""), function.get("arguments"))
                tool_message = {"role": "tool", "tool_call_id": call.get("id"), "content": result}
                messages.append(tool_message)
                new_messages.append(tool_message)

        if not reply:
            raise LLMError("the coach looked a lot up but didn't come back with an answer")

        footer_lines = []
        hits = cs.find_forbidden(reply, self._corpus)
        if hits:
            footer_lines.append(
                f"-# 🚨 This reply used language Shull's framework rejects (`{', '.join(hits)}`). "
                "Treat it with suspicion.")

        if dh.is_peak(now):
            ends = dh.next_off_peak(now).isoformat()
            first = memory.get("peak_notice_until") != ends
            footer_lines.append(peak_line(now, ccs.usage_cost(usages, peak=True), first))
            if first:
                latest = await self.memory.load()
                latest["peak_notice_until"] = ends
                await self.memory.save(latest)

        await self.memory.append({
            "time": sent_at.isoformat(),
            "user": text,
            "reply": reply,
            "messages": new_messages,
            "usage": usages,
            "cost_usd": round(ccs.usage_cost(usages, peak=dh.is_peak(now)), 5),
        })
        return reply, "\n".join(footer_lines)

    async def fold_memory(self):
        """Roll older exchanges into the summary once enough have built up."""
        pending = await self.memory.unsummarized()
        if len(pending) <= ccs.FOLD_AT:
            return
        to_fold = pending[: len(pending) - ccs.KEEP_RECENT]
        memory = await self.memory.load()
        try:
            summary = await llm_service.complete(
                ccs.build_fold_prompt(memory["summary"], to_fold),
                reasoning_effort="off", max_tokens=1500)
        except LLMError as e:
            # Not fatal: the exchanges stay unsummarized and are retried next time.
            print(f"[coach_chat] summary fold failed: {e}")
            return
        memory = await self.memory.load()
        memory["summary"] = summary.strip()
        memory["summarized_through"] = to_fold[-1]["id"]
        await self.memory.save(memory)

    @staticmethod
    async def _search_knowledge(query: str):
        from services.rag_service import rag_service
        return await asyncio.to_thread(rag_service.query_knowledge, query)

    # --- commands ---

    @app_commands.command(name="coach_chat_setup", description="Create the #coach channel for talking with your coach.")
    @app_commands.default_permissions(manage_channels=True)
    async def coach_chat_setup(self, interaction: discord.Interaction):
        existing = discord.utils.get(interaction.guild.text_channels, name=COACH_CHAT_CHANNEL_NAME)
        if existing:
            await interaction.response.send_message(f"✅ {existing.mention} already exists — just talk there.")
            return
        try:
            channel = await interaction.guild.create_text_channel(
                COACH_CHAT_CHANNEL_NAME, topic="Talk with your trading coach. Every message here is a conversation turn.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to create channels.", ephemeral=True)
            return
        await channel.send(
            "🧠 **This is your coaching channel.** Just talk — about a trade, a week, how you're feeling.\n"
            "I can look up every trade you've journaled (notes, reflections, `/state`, `/outcome`), your "
            "missed trades, your insights and your knowledge base, and I remember our conversations.\n"
            "-# `/coach_notes` shows what I've noted about you · `/coach_forget` removes a note")
        await interaction.response.send_message(f"✅ Created {channel.mention}.")

    @app_commands.command(name="coach_notes", description="What the coach has chosen to remember about you.")
    async def coach_notes(self, interaction: discord.Interaction):
        memory = await self.memory.load()
        notes = memory["notes"]
        if not notes:
            await interaction.response.send_message("The coach hasn't noted anything about you yet.", ephemeral=True)
            return
        lines = [f"`{n['id']}` · {ccs.fmt_date(ccs.local_date(n['created']))} — {n['text']}" for n in notes]
        embed = discord.Embed(title="🧠 Coach notes", description="\n".join(lines)[:4000],
                              color=discord.Color.purple())
        embed.set_footer(text="/coach_forget <id> removes one")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="coach_forget", description="Delete one of the coach's notes about you.")
    @app_commands.describe(note_id="The number shown by /coach_notes.")
    async def coach_forget(self, interaction: discord.Interaction, note_id: int):
        removed = await self.memory.remove_note(note_id)
        if removed:
            await interaction.response.send_message(f"🗑️ Forgot note {note_id}: “{removed['text']}”", ephemeral=True)
        else:
            await interaction.response.send_message(f"There's no note {note_id}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CoachChat(bot))
