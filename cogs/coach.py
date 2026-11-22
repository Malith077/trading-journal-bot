"""
Live Shull coaching on journal entries.

Posts grounded feedback when you write in a trade channel, and records whether
you found each one useful. That grading is not decoration — combined with the
eval suite it is how the coach earns or loses trust over time.

Commands:
  /coach          Coach an arbitrary piece of text on demand.
  /coach_auto     Turn automatic coaching on or off.
  /coach_trust    What the coach is grounded in, and how it has actually scored.
"""
import json
import time
import discord
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord import ui

from config import COACH_LOG_PATH, COACH_AUTO_DEFAULT, COACH_SETTINGS_PATH
from services import coach_service as cs
from services.guild_guard import is_allowed_guild
from services.coach_service import CoachError

# Don't coach the same channel more than once per this window. Journalling comes
# in bursts of several messages; one reading per burst is the useful cadence.
COACH_COOLDOWN_SECONDS = 90


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
