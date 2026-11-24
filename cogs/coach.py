"""
Live Shull coaching on journal entries.

Posts grounded feedback when you write in a trade channel, and records whether
you found each one useful. That grading is not decoration — combined with the
eval suite it is how the coach earns or loses trust over time.

Commands:
  /coach          Coach an arbitrary piece of text on demand.
  /coach_auto     Turn automatic coaching on or off.
  /coach_trust    What the coach is grounded in, and how it has actually scored.
  /shull_review   Review a whole trade channel once you've finished writing it.
                  The 🧠 Shull Review button on each new trade channel does the same.
"""
import json
import time
import discord
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord import ui

import datetime

from config import (COACH_LOG_PATH, COACH_AUTO_DEFAULT, COACH_SETTINGS_PATH,
                    TRADES_DIR, MISSED_DIR)
from services import coach_service as cs
from services.guild_guard import is_allowed_guild
from services.coach_service import CoachError

# Don't coach the same channel more than once per this window. Journalling comes
# in bursts of several messages; one reading per burst is the useful cadence.
COACH_COOLDOWN_SECONDS = 90

# A channel review reads everything you wrote in the channel. These bound the
# prompt: the whole principles corpus already rides along with every call.
REVIEW_HISTORY_LIMIT = 200
MAX_REVIEW_CHARS = 8000

REFLECTION_LABELS = (
    ("why_entered", "Why I entered"),
    ("how_felt", "How I felt"),
    ("what_learned", "What I learned"),
    ("reminds_me_of", "What it reminded me of"),
)


async def _read_json(path, default):
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            data = json.loads(await f.read())
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except Exception:
        return default


async def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, indent=4, ensure_ascii=False))


async def log_grade(log_path, record: dict):
    """Append one coaching record (and its grade) to the JSONL log."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(log_path, "a", encoding="utf-8") as f:
        await f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def read_grades(log_path) -> list[dict]:
    """Read every graded coaching record, skipping corrupt lines."""
    log_path = Path(log_path)
    if not log_path.exists():
        return []
    async with aiofiles.open(log_path, "r", encoding="utf-8") as f:
        content = await f.read()

    records = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


async def gather_trade_entry(channel, trades_dir=None) -> tuple[str, bool]:
    """
    Assemble a trade channel into one journal entry for a Shull review.

    Your own messages come first, oldest first, then the saved reflection form
    if there is one. Bot messages (checklists, earlier readings) are left out so
    the coach only reads what you wrote.

    Returns (entry, coachable). `coachable` is judged on your words alone, not
    on the section labels added here.
    """
    notes = []
    async for message in channel.history(limit=REVIEW_HISTORY_LIMIT, oldest_first=True):
        if message.author.bot:
            continue
        text = (message.content or "").strip()
        if text:
            notes.append(text)

    folder = Path(trades_dir) / channel.name if trades_dir else journal_dir(channel.name)
    reflections = await _read_json(folder / "reflections.json", {})
    reflection_lines = [
        f"- {label}: {reflections[key].strip()}"
        for key, label in REFLECTION_LABELS
        if isinstance(reflections.get(key), str) and reflections[key].strip()
    ]

    notes_text = "\n\n".join(notes)
    if len(notes_text) > MAX_REVIEW_CHARS:
        # Keep the end: the last things written are the closest to "done".
        notes_text = "…" + notes_text[-MAX_REVIEW_CHARS:]

    sections = []
    if notes_text:
        sections.append(f"TRADE NOTES:\n{notes_text}")
    if reflection_lines:
        sections.append("REFLECTION FORM:\n" + "\n".join(reflection_lines))

    own_words = notes_text + " ".join(reflections.get(k) or "" for k, _ in REFLECTION_LABELS
                                      if isinstance(reflections.get(k), str))
    return "\n\n".join(sections), cs.is_coachable(own_words)


REVIEW_FILE = "shull_review.json"


def journal_dir(channel_name: str) -> Path:
    """Where a channel's journal files live — missed trades have their own tree."""
    base = MISSED_DIR if channel_name.startswith("missed_") else TRADES_DIR
    return base / channel_name


def build_feedback_embed(feedback: dict, corpus: dict) -> discord.Embed:
    """Render coaching, with every observation showing the principle it cites."""
    titles = {p["id"]: p["title"] for p in corpus["principles"]}
    confidence = {p["id"]: p["confidence"] for p in corpus["principles"]}

    if feedback.get("not_enough_signal") and not feedback.get("observations"):
        embed = discord.Embed(
            title="🧠 Nothing to read here",
            description=(
                "That entry is technical rather than emotional, so there's no "
                "psychological signal to work with. Said plainly rather than "
                "inventing something."
            ),
            color=discord.Color.light_grey()
        )
        return embed

    embed = discord.Embed(
        title="🧠 Shull Reading",
        description=feedback.get("reading") or discord.utils.MISSING,
        color=discord.Color.purple()
    )

    for obs in feedback.get("observations", [])[:3]:
        pid = obs.get("principle_id")
        title = titles.get(pid, pid)
        # A medium-confidence principle is flagged inline so the reader knows
        # the citation rests on synthesis rather than direct attestation.
        marker = " ·  ~" if confidence.get(pid) == "medium" else ""
        quote = obs.get("quote", "")
        body = f"> {quote}\n\n{obs.get('point', '')}" if quote else obs.get("point", "")
        embed.add_field(name=f"[{pid}] {title}{marker}", value=body[:1024], inline=False)

    if feedback.get("probe"):
        embed.add_field(name="Sit with this", value=feedback["probe"], inline=False)

    if feedback.get("forbidden_hits"):
        embed.add_field(
            name="🚨 Trust warning",
            value=(
                "This response contained language Shull explicitly rejects "
                f"(`{', '.join(feedback['forbidden_hits'])}`). The coach has drifted "
                "off-framework — treat this reading as unreliable and run "
                "`/coach_trust`."
            ),
            inline=False
        )
        embed.color = discord.Color.red()

    embed.set_footer(text="Every claim cites a principle from coaching/shull_principles.json · ~ = medium confidence")
    return embed


class GradeView(ui.View):
    """👍/👎 on a piece of coaching. This is the empirical half of trust."""

    def __init__(self, cog: "Coach", record: dict):
        super().__init__(timeout=86400)  # a day to react
        self.cog = cog
        self.record = record
        self.graded = False

    async def _grade(self, interaction: discord.Interaction, useful: bool):
        if self.graded:
            await interaction.response.send_message("Already graded.", ephemeral=True)
            return
        self.graded = True

        await log_grade(COACH_LOG_PATH, {**self.record, "useful": useful,
                                         "graded_by": str(interaction.user)})
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "👍 Logged as useful." if useful else
            "👎 Logged as unhelpful. This counts against the coach in `/coach_trust`.",
            ephemeral=True
        )

    @ui.button(label="👍 Useful", style=discord.ButtonStyle.success)
    async def useful(self, interaction: discord.Interaction, button: ui.Button):
        await self._grade(interaction, True)

    @ui.button(label="👎 Off the mark", style=discord.ButtonStyle.secondary)
    async def not_useful(self, interaction: discord.Interaction, button: ui.Button):
        await self._grade(interaction, False)


class Coach(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_coached: dict[int, float] = {}   # channel_id → monotonic time
        self._reviewing: set[int] = set()            # channels with a review in flight
        self._corpus = None
        self._corpus_error = None
        try:
            self._corpus = cs.load_corpus()
        except CoachError as e:
            # Never crash the bot over a bad corpus; degrade loudly instead.
            self._corpus_error = str(e)
            print(f"❌ Coach disabled: {e}")

    @property
    def corpus(self):
        return self._corpus

    # --- settings ---

    async def _auto_enabled(self) -> bool:
        settings = await _read_json(COACH_SETTINGS_PATH, {})
        return bool(settings.get("auto", COACH_AUTO_DEFAULT))

    async def _set_auto(self, enabled: bool):
        settings = await _read_json(COACH_SETTINGS_PATH, {})
        settings["auto"] = enabled
        await _write_json(COACH_SETTINGS_PATH, settings)

    # --- automatic coaching ---

    @staticmethod
    def _is_journal_channel(channel) -> bool:
        category = getattr(channel, "category", None)
        if not category:
            return False
        return (category.name.startswith("Fractal_Trades")
                or category.name.startswith("Missed_Trades"))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Coach substantive journal entries as they're written."""
        if message.author.bot or not message.guild:
            return
        if not self._corpus or not self._is_journal_channel(message.channel):
            return
        if not is_allowed_guild(message.guild):
            return
        if not cs.is_coachable(message.content):
            return
        if not await self._auto_enabled():
            return

        # Cooldown: journalling arrives in bursts; one reading per burst.
        now = time.monotonic()
        last = self._last_coached.get(message.channel.id, 0)
        if now - last < COACH_COOLDOWN_SECONDS:
            return
        self._last_coached[message.channel.id] = now

        try:
            async with message.channel.typing():
                feedback = await cs.coach(message.content, corpus=self._corpus)
        except CoachError as e:
            print(f"[coach] failed in #{message.channel.name}: {e}")
            return

        await self._send_feedback(message.channel, message.content, feedback,
                                  channel_name=message.channel.name)

    async def _send_feedback(self, destination, entry: str, feedback: dict,
                             channel_name: str | None = None):
        record = {
            "channel": channel_name,
            "entry": entry[:2000],
            "cited": [o.get("principle_id") for o in feedback.get("observations", [])],
            "warnings": feedback.get("warnings", []),
            "forbidden_hits": feedback.get("forbidden_hits", []),
            "corpus_version": self._corpus.get("version"),
        }
        embed = build_feedback_embed(feedback, self._corpus)
        view = GradeView(self, record)
        return await destination.send(embed=embed, view=view)

    # --- channel review (button + /shull_review) ---

    async def review_channel(self, interaction: discord.Interaction):
        """
        Run a Shull review over everything written in the interaction's channel.

        Triggered by the 🧠 Shull Review button on a trade channel, or by
        /shull_review. Unlike auto-coaching, which reads one message as you
        write it, this waits until you say you're done and reads the whole trade.
        """
        channel = interaction.channel
        if not is_allowed_guild(interaction.guild):
            await interaction.response.send_message(
                "This bot instance isn't handling this server.", ephemeral=True)
            return
        if not self._corpus:
            await interaction.response.send_message(
                f"❌ Coaching is disabled: {self._corpus_error}", ephemeral=True)
            return
        if not self._is_journal_channel(channel):
            await interaction.response.send_message(
                "❌ Run this in a trade channel — there's no journal here to review.",
                ephemeral=True)
            return
        if channel.id in self._reviewing:
            await interaction.response.send_message(
                "⏳ A Shull review of this channel is already running.", ephemeral=True)
            return

        self._reviewing.add(channel.id)
        try:
            # Ephemeral: the only public message is the reading itself.
            await interaction.response.defer(ephemeral=True, thinking=True)
            entry, coachable = await gather_trade_entry(channel)
            if not coachable:
                await interaction.followup.send(
                    "📭 Not enough written here to review yet (under "
                    f"{cs.MIN_ENTRY_CHARS} characters of your own words). Add your "
                    "thoughts — what you saw, what you felt — then press the button again.",
                    ephemeral=True,
                )
                return

            # Imported here: the trades cog is the owner of per-trade state files.
            from cogs.trades import Trades
            context = await Trades._get_psych_context(journal_dir(channel.name))
            try:
                feedback = await cs.coach(entry, context=context, corpus=self._corpus)
            except CoachError as e:
                await interaction.followup.send(f"❌ Shull review failed: {e}", ephemeral=True)
                return

            message, updated = await self._publish_review(channel, entry, feedback)
        finally:
            self._reviewing.discard(channel.id)

        await interaction.followup.send(
            f"🔄 Updated the reading: {message.jump_url}" if updated
            else "✅ Reading posted below.",
            ephemeral=True,
        )

    async def _publish_review(self, channel, entry: str, feedback: dict):
        """
        Post the reading, or replace the previous one in place on a re-run.

        It goes out as a plain channel message, not an interaction follow-up:
        follow-ups can only be edited for 15 minutes, and a re-run may come
        days later. Returns (message, updated).
        """
        folder = journal_dir(channel.name)
        saved = await _read_json(folder / REVIEW_FILE, {})
        record = {
            "channel": channel.name,
            "entry": entry[:2000],
            "cited": [o.get("principle_id") for o in feedback.get("observations", [])],
            "warnings": feedback.get("warnings", []),
            "forbidden_hits": feedback.get("forbidden_hits", []),
            "corpus_version": self._corpus.get("version"),
        }
        now = datetime.datetime.now(datetime.timezone.utc)
        runs = int(saved.get("runs") or 0) + 1
        embed = build_feedback_embed(feedback, self._corpus)
        embed.timestamp = now
        if runs > 1:
            embed.set_footer(text=f"{embed.footer.text} · review #{runs}")
        view = GradeView(self, record)

        message, updated = None, False
        if saved.get("message_id"):
            try:
                message = await channel.fetch_message(int(saved["message_id"]))
                await message.edit(embed=embed, view=view)
                updated = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                message = None  # deleted or unreachable: post a fresh one
        if message is None:
            message = await channel.send(embed=embed, view=view)

        await _write_json(folder / REVIEW_FILE, {
            "message_id": str(message.id),
            "runs": runs,
            "updated_at": now.isoformat(),
            "feedback": {k: feedback.get(k) for k in
                         ("reading", "observations", "probe", "not_enough_signal",
                          "warnings", "forbidden_hits")},
        })
        return message, updated

    @app_commands.command(name="shull_review",
                          description="Shull review of everything you've written in this trade channel.")
    async def shull_review(self, interaction: discord.Interaction):
        await self.review_channel(interaction)

    # --- /coach ---

    @app_commands.command(name="coach", description="Get Shull-grounded feedback on anything you write.")
    @app_commands.describe(entry="What you're thinking, or what happened.")
    async def coach_cmd(self, interaction: discord.Interaction, entry: str):
        if not self._corpus:
            await interaction.response.send_message(
                f"❌ Coaching is disabled: {self._corpus_error}", ephemeral=True)
            return

        await interaction.response.defer()

        if not cs.is_coachable(entry):
            await interaction.followup.send(
                f"That's too short to read anything from (under {cs.MIN_ENTRY_CHARS} characters). "
                "Give me the situation and how it felt."
            )
            return

        try:
            feedback = await cs.coach(entry, corpus=self._corpus)
        except CoachError as e:
            await interaction.followup.send(f"❌ Coaching failed: {e}")
            return

        embed = build_feedback_embed(feedback, self._corpus)
        record = {
            "channel": getattr(interaction.channel, "name", None),
            "entry": entry[:2000],
            "cited": [o.get("principle_id") for o in feedback.get("observations", [])],
            "warnings": feedback.get("warnings", []),
            "forbidden_hits": feedback.get("forbidden_hits", []),
            "corpus_version": self._corpus.get("version"),
        }
        await interaction.followup.send(embed=embed, view=GradeView(self, record))

    # --- /coach_auto ---

    @app_commands.command(name="coach_auto", description="Turn automatic coaching on trade channels on or off.")
    @app_commands.describe(enabled="Whether to coach journal entries automatically.")
    async def coach_auto(self, interaction: discord.Interaction, enabled: bool):
        await self._set_auto(enabled)
        await interaction.response.send_message(
            f"🧠 Automatic coaching is now **{'on' if enabled else 'off'}**."
            + ("" if enabled else " `/coach` still works on demand.")
        )

    # --- /coach_trust ---

    @app_commands.command(name="coach_trust", description="What the coach is grounded in, and how it has actually scored.")
    async def coach_trust(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._corpus:
            await interaction.followup.send(f"❌ Coaching is disabled: {self._corpus_error}")
            return

        corpus = self._corpus
        principles = corpus["principles"]
        high = sum(1 for p in principles if p["confidence"] == "high")

        embed = discord.Embed(
            title="🔍 Can you trust the coach?",
            description=(
                "Three separate questions, with three different answers. "
                "Only the first two are things a machine can settle."
            ),
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="1 · Is it actually applying Shull? — testable",
            value=(
                f"Every claim must cite one of **{len(principles)}** principles in "
                f"`coaching/shull_principles.json` (v{corpus.get('version')}). "
                "Citations that don't exist are dropped before you see them.\n"
                f"**{high}/{len(principles)}** are high-confidence; the rest are marked `~`.\n"
                "Run `python scripts/run_coach_eval.py` for a score — it includes "
                "traps where generic trading psychology contradicts Shull."
            ),
            inline=False
        )

        embed.add_field(
            name="2 · Is it drifting off-framework? — automatic",
            value=(
                f"Every response is scanned for **{len(cs.forbidden_phrases(corpus))}** phrases "
                "Shull rejects (*eliminate emotion*, *you need more discipline*…). "
                "A hit is flagged in red on the reading itself, not hidden."
            ),
            inline=False
        )

        grades = await read_grades(COACH_LOG_PATH)
        graded = [g for g in grades if "useful" in g]
        if graded:
            useful = sum(1 for g in graded if g["useful"])
            incidents = sum(1 for g in grades if g.get("forbidden_hits"))
            fabrications = sum(
                1 for g in grades
                if any(w.startswith("Dropped") for w in g.get("warnings", []))
            )
            verdict = (
                f"**{useful}/{len(graded)}** ({useful / len(graded):.0%}) of readings "
                "you graded were useful.\n"
                f"Fabricated citations caught and dropped: **{fabrications}**\n"
                f"Anti-Shull language incidents: **{incidents}**"
            )
        else:
            verdict = (
                "*No readings graded yet.* Hit 👍/👎 on each one — "
                "that's the only measure of whether it helps **you**, "
                "as opposed to whether it follows the framework."
            )
        embed.add_field(name="3 · Is it useful to you? — only you can say", value=verdict, inline=False)

        embed.add_field(
            name="⚠️ The limit none of this closes",
            value=(
                "The principles file was compiled from published summaries of Shull's "
                "work, **not transcribed from her book**. If a principle in it misreads "
                "her, the coach will repeat that misreading perfectly and every check "
                "above will still pass. Read the file against *Market Mind Games* and "
                "correct it — that's the one link only you can verify."
            ),
            inline=False
        )
        embed.set_footer(text=f"Corpus v{corpus.get('version')} · model: {corpus.get('framework')}")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Coach(bot))
