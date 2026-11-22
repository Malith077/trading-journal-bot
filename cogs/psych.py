"""
Trading psychology cog — Denise Shull's method wired into the journal.

Shull's premise: emotions are data. Nothing here gates the trader out of a
trade for feeling something. The job is to capture the emotional context
precisely, then check it against results so felt certainty can be audited.

Commands:
  /state        Mark-to-market your current emotional state.
  /rethink      Post-loss processing — sit with it before acting on it.
  /outcome      Tag a trade channel win/loss/breakeven + R, so state correlates.
  /psych_report Correlate logged states against outcomes.
"""
import json
import discord
import aiofiles
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from discord import ui

from config import PSYCH_LOG_PATH, TRADES_DIR, MISSED_DIR, DEEPSEEK_REASONING_MODEL
from services.llm_service import llm_service, LLMError
from services import psych_service as ps


# ──────────────────────────────────────────────
# Per-channel persistence
# ──────────────────────────────────────────────

def _channel_dir(channel_name: str) -> Path:
    """Where a channel's journal lives — missed trades have their own tree."""
    base = MISSED_DIR if channel_name.startswith("missed_") else TRADES_DIR
    return base / channel_name


async def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, indent=4, ensure_ascii=False))


async def _read_json(path: Path) -> dict:
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            data = json.loads(await f.read())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        return {}


async def collect_correlation_entries() -> list[dict]:
    """
    Merge each trade channel's psych state with its recorded outcome.

    A channel contributes an entry only when it has both — state without an
    outcome has nothing to be checked against, and an outcome without state
    says nothing about psychology.
    """
    entries = []
    for base in (TRADES_DIR, MISSED_DIR):
        if not base.exists():
            continue
        for channel_dir in sorted(base.iterdir()):
            if not channel_dir.is_dir():
                continue

            outcome = await _read_json(channel_dir / "outcome.json")
            if not outcome.get("outcome"):
                continue

            state = await _read_json(channel_dir / "psych.json")
            if not state:
                continue

            entries.append({
                **state,
                "channel": channel_dir.name,
                "outcome": outcome["outcome"],
                "r_multiple": outcome.get("r_multiple"),
            })
    return entries


# ──────────────────────────────────────────────
# State capture UI
# ──────────────────────────────────────────────

class FractalContextModal(ui.Modal, title="🧠 Emotional Context"):
    """
    Captures Shull's F-eC probe plus a free note.

    "What does this remind you of?" is the question that reaches past the
    current chart to the recurring story underneath it.
    """

    reminds_me_of = ui.TextInput(
        label="What does this remind me of?",
        placeholder="An earlier trade, an earlier week — or something with nothing to do with markets.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000
    )
    note = ui.TextInput(
        label="Anything else worth marking down?",
        placeholder="e.g. Third red day running, and I can feel myself wanting it back.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000
    )

    def __init__(self, view: "StateView"):
        super().__init__()
        self.state_view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.state_view.reminds_me_of = self.reminds_me_of.value or None
        self.state_view.note = self.note.value or None
        await interaction.response.edit_message(embed=self.state_view.build_embed(), view=self.state_view)


class StateView(ui.View):
    """
    Interactive capture of one psychological state reading.

    Not persistent: a state reading belongs to the moment it was taken, so the
    view is bound to the invoking user and expires.
    """

    def __init__(self, author_id: int, channel_name: str | None):
        super().__init__(timeout=900)
        self.author_id = author_id
        self.channel_name = channel_name

        self.feelings: str | None = None
        self.emotions: list[str] = []
        self.fear: int = 0
        self.conviction: int | None = None
        self.probes: dict[str, bool] = {}
        self.reminds_me_of: str | None = None
        self.note: str | None = None
        self.saved = False

        self._build_selects()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This check-in belongs to someone else — run `/state` for your own.",
                ephemeral=True
            )
            return False
        return True

    def _build_selects(self):
        fc = ui.Select(
            placeholder="fC — how does the body feel?",
            min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label=label, value=key, emoji=emoji)
                     for key, label, emoji in ps.FEELINGS_CONTEXT],
        )
        fc.callback = self._on_feelings
        self.add_item(fc)

        ec = ui.Select(
            placeholder="eC — name the emotions (pick all that fit)",
            min_values=1, max_values=4, row=1,
            options=[discord.SelectOption(label=label, value=key, emoji=emoji)
                     for key, label, emoji, _ in ps.EMOTIONAL_CONTEXT],
        )
        ec.callback = self._on_emotions
        self.add_item(ec)

        fear = ui.Select(
            placeholder="Where am I between fear of losing and FOMO?",
            min_values=1, max_values=1, row=2,
            options=[
                discord.SelectOption(label=ps.fear_label(v), value=str(v))
                for v in range(ps.FEAR_MIN, ps.FEAR_MAX + 1)
            ],
        )
        fear.callback = self._on_fear
        self.add_item(fear)

        probes = ui.Select(
            placeholder="Intuition or impulse? (tick everything true)",
            min_values=0, max_values=len(ps.IMPULSE_PROBES), row=3,
            options=[discord.SelectOption(label=text[:100], value=key)
                     for key, text in ps.IMPULSE_PROBES],
        )
        probes.callback = self._on_probes
        self.add_item(probes)

    async def _on_feelings(self, interaction: discord.Interaction):
        self.feelings = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_emotions(self, interaction: discord.Interaction):
        self.emotions = list(interaction.data["values"])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_fear(self, interaction: discord.Interaction):
        self.fear = int(interaction.data["values"][0])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_probes(self, interaction: discord.Interaction):
        selected = set(interaction.data["values"])
        self.probes = {key: (key in selected) for key in ps.IMPULSE_PROBE_KEYS}
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @ui.button(label="🧠 What does this remind me of?", style=discord.ButtonStyle.secondary, row=4)
    async def open_fractal(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(FractalContextModal(self))

    @ui.button(label="💾 Save", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, button: ui.Button):
        state = self.to_state(author=str(interaction.user))

        await ps.append_state(PSYCH_LOG_PATH, state)
        # Bind the reading to the trade channel it was taken in, so it can be
        # correlated with that trade's outcome later.
        if self.channel_name:
            await _write_json(_channel_dir(self.channel_name) / "psych.json", state)

        self.saved = True
        for child in self.children:
            child.disabled = True

        embed = self.build_embed()
        embed.color = discord.Color.green()
        embed.set_footer(text="Logged. Emotions become data once they're written down.")
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    def to_state(self, author: str | None = None) -> dict:
        return ps.build_state(
            feelings=self.feelings,
            emotions=self.emotions,
            fear=self.fear,
            conviction=self.conviction,
            probes=self.probes,
            reminds_me_of=self.reminds_me_of,
            note=self.note,
            channel=self.channel_name,
            author=author,
        )

    def build_embed(self) -> discord.Embed:
        signal, fired = ps.classify_signal(self.probes)

        embed = discord.Embed(
            title="🧠 Mark-to-Market: Emotional State",
            description=(
                "Shull's premise: this is *data*, not something to fix. "
                "Name it precisely, then we check it against your results."
            ),
            color=discord.Color.purple()
        )

        embed.add_field(
            name="fC · Physical",
            value=(f"{ps.FEELINGS_EMOJI[self.feelings]} {ps.FEELINGS_LABELS[self.feelings]}"
                   if self.feelings else "*not set*"),
            inline=True
        )
        embed.add_field(
            name="eC · Emotional",
            value=(", ".join(f"{ps.EMOTION_EMOJI[e]} {ps.EMOTION_LABELS[e]}" for e in self.emotions)
                   if self.emotions else "*not set*"),
            inline=True
        )
        embed.add_field(
            name="Fear spectrum",
            value=f"{ps.fear_label(self.fear)}\n`{ps.fear_bar(self.fear)}`",
            inline=False
        )
        embed.add_field(
            name=f"{ps.SIGNAL_LABELS[signal]}  ({fired}/{len(ps.IMPULSE_PROBES)} probes fired)",
            value=ps.SIGNAL_GUIDANCE[signal],
            inline=False
        )
        if self.reminds_me_of:
            embed.add_field(name="F-eC · What this reminds me of", value=self.reminds_me_of, inline=False)
        if self.note:
            embed.add_field(name="Note", value=self.note, inline=False)

        return embed


# ──────────────────────────────────────────────
# Outcome capture
# ──────────────────────────────────────────────

class OutcomeModal(ui.Modal, title="📌 Trade Outcome"):
    """R multiple is optional — the outcome tag alone is enough to correlate."""

    r_multiple = ui.TextInput(
        label="R multiple (optional)",
        placeholder="e.g. 2.5, -1, 0",
        required=False,
        max_length=10
    )

    def __init__(self, channel_name: str, outcome: str):
        super().__init__()
        self.channel_name = channel_name
        self.outcome = outcome

    async def on_submit(self, interaction: discord.Interaction):
        r_value = None
        raw = (self.r_multiple.value or "").strip().replace("R", "").replace("r", "")
        if raw:
            try:
                r_value = float(raw)
            except ValueError:
                await interaction.response.send_message(
                    f"⚠️ `{self.r_multiple.value}` isn't a number — saving the outcome without an R multiple.",
                    ephemeral=True
                )

        await _write_json(
            _channel_dir(self.channel_name) / "outcome.json",
            {"outcome": self.outcome, "r_multiple": r_value},
        )

        label = f"{ps.OUTCOME_EMOJI[self.outcome]} {ps.OUTCOME_LABELS[self.outcome]}"
        suffix = f" at **{r_value:+g}R**" if r_value is not None else ""

        if interaction.response.is_done():
            await interaction.followup.send(f"📌 Recorded **{label}**{suffix} for `{self.channel_name}`.")
        else:
            await interaction.response.send_message(f"📌 Recorded **{label}**{suffix} for `{self.channel_name}`.")


class Psych(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- /state ---

    @app_commands.command(name="state", description="Mark-to-market your emotional state before or during a trade.")
    @app_commands.describe(conviction="How sure do you feel, 1-10? (optional)")
    async def state(self, interaction: discord.Interaction, conviction: app_commands.Range[int, 1, 10] | None = None):
        """Capture fC / eC / F-eC, the fear spectrum, and an intuition-vs-impulse read."""
        channel_name = getattr(interaction.channel, "name", None)
        # Only bind to journal channels; a check-in in #general is standalone.
        if channel_name and not (channel_name.startswith("trade_") or channel_name.startswith("missed_")):
            channel_name = None

        view = StateView(interaction.user.id, channel_name)
        view.conviction = conviction
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=False)

    # --- /rethink ---

    @app_commands.command(name="rethink", description="Post-loss processing — sit with it before acting on it.")
    async def rethink(self, interaction: discord.Interaction):
        """
        Shull's post-loss instruction: feel the discomfort fully *before* doing
        something. The most expensive trade is usually the next one.
        """
        embed = discord.Embed(
            title="🫷 Rethink — before the next click",
            description=(
                "Shull's post-loss instruction is deliberately not *do something*. "
                "It's **feel it, fully, first**. The urge to act right now is the loss "
                "talking, and acting on it is how one loss becomes three."
            ),
            color=discord.Color.dark_red()
        )
        embed.add_field(
            name="1 · Name it precisely",
            value="Not \"bad\" — *which* feeling? Angry? Embarrassed? Deflated? "
                  "Wanting it back? Precision is what makes it data.",
            inline=False
        )
        embed.add_field(
            name="2 · Locate it",
            value="Where is it in your body? Jaw, chest, gut, shoulders? "
                  "The physical read (fC) is usually honest before the story is.",
            inline=False
        )
        embed.add_field(
            name="3 · What does this remind you of?",
            value="This is the F-eC probe. If the reaction feels outsized for the "
                  "size of the loss, the extra almost always belongs to something older.",
            inline=False
        )
        embed.add_field(
            name="4 · Only then, decide",
            value="Once it's named, it loses most of its grip. Log it with `/state`, "
                  "then decide about the next trade — in that order.",
            inline=False
        )
        embed.set_footer(text="Not a rule against trading. A rule against trading unconsciously.")
        await interaction.response.send_message(embed=embed)

    # --- /outcome ---

    @app_commands.command(name="outcome", description="Tag this trade channel's result so emotions can be correlated.")
    @app_commands.describe(result="How did it end?", r_multiple="R multiple, e.g. 2.5 or -1 (optional)")
    @app_commands.choices(result=[
        app_commands.Choice(name=f"{emoji} {label}", value=key)
        for key, label, emoji in ps.OUTCOMES
    ])
    async def outcome(
        self,
        interaction: discord.Interaction,
        result: app_commands.Choice[str],
        r_multiple: float | None = None,
    ):
        channel_name = getattr(interaction.channel, "name", None)
        if not channel_name or not (channel_name.startswith("trade_") or channel_name.startswith("missed_")):
            await interaction.response.send_message(
                "❌ Run this inside a trade channel (`trade_*` or `missed_*`).",
                ephemeral=True
            )
            return

        await _write_json(
            _channel_dir(channel_name) / "outcome.json",
            {"outcome": result.value, "r_multiple": r_multiple},
        )

        label = f"{ps.OUTCOME_EMOJI[result.value]} {ps.OUTCOME_LABELS[result.value]}"
        suffix = f" at **{r_multiple:+g}R**" if r_multiple is not None else ""
        await interaction.response.send_message(f"📌 Recorded **{label}**{suffix} for `{channel_name}`.")

    # --- /psych_report ---

    @app_commands.command(name="psych_report", description="Correlate your logged emotional states against results.")
    @app_commands.describe(deep="Add an AI synthesis of the patterns (slower, uses the reasoning model).")
    async def psych_report(self, interaction: discord.Interaction, deep: bool = False):
        await interaction.response.defer()

        entries = await collect_correlation_entries()
        if not entries:
            await interaction.followup.send(
                "📭 Nothing to correlate yet. Log a state with `/state` inside a trade channel, "
                "then tag the result with `/outcome`. Two trades is enough to start."
            )
            return

        stats = ps.correlate(entries)
        embed = self._build_report_embed(stats, len(entries))
        await interaction.followup.send(embed=embed)

        if deep:
            await self._send_synthesis(interaction, stats, entries)

    @staticmethod
    def _fmt(bucket: dict) -> str:
        """Render one tally as 'n trades · win rate · avg R'."""
        parts = [f"**{bucket['n']}** trade(s)"]
        if bucket["win_rate"] is not None:
            parts.append(f"{bucket['win_rate']:.0%} win")
        if bucket["avg_r"] is not None:
            parts.append(f"{bucket['avg_r']:+.2f}R avg")
        return " · ".join(parts)

    def _build_report_embed(self, stats: dict, n_entries: int) -> discord.Embed:
        overall = stats["overall"]
        embed = discord.Embed(
            title="🧠 Psychological Edge Report",
            description=(
                f"Across **{n_entries}** trade(s) with both a logged state and a result.\n"
                f"Overall: {self._fmt(overall)}"
            ),
            color=discord.Color.purple()
        )

        if stats["by_fear_zone"]:
            lines = []
            for key, label, emoji, _, _ in ps.FEAR_ZONES:
                bucket = stats["by_fear_zone"].get(key)
                if bucket:
                    lines.append(f"{emoji} **{label}** — {self._fmt(bucket)}")
            if lines:
                embed.add_field(name="By fear spectrum", value="\n".join(lines), inline=False)

        if stats["by_signal"]:
            lines = []
            for key in (ps.SIGNAL_INTUITION, ps.SIGNAL_MIXED, ps.SIGNAL_IMPULSE):
                bucket = stats["by_signal"].get(key)
                if bucket:
                    lines.append(f"{ps.SIGNAL_LABELS[key]} — {self._fmt(bucket)}")
            embed.add_field(name="Intuition vs impulse", value="\n".join(lines), inline=False)

        if stats["by_emotion"]:
            ranked = sorted(stats["by_emotion"].items(), key=lambda kv: kv[1]["n"], reverse=True)[:6]
            lines = [
                f"{ps.EMOTION_EMOJI.get(k, '•')} **{ps.EMOTION_LABELS.get(k, k)}** — {self._fmt(v)}"
                for k, v in ranked
            ]
            embed.add_field(name="By emotional context (eC)", value="\n".join(lines), inline=False)

        if stats["by_feelings"]:
            ranked = sorted(stats["by_feelings"].items(), key=lambda kv: kv[1]["n"], reverse=True)[:4]
            lines = [
                f"{ps.FEELINGS_EMOJI.get(k, '•')} **{ps.FEELINGS_LABELS.get(k, k)}** — {self._fmt(v)}"
                for k, v in ranked
            ]
            embed.add_field(name="By physical state (fC)", value="\n".join(lines), inline=False)

        embed.add_field(name="Conviction calibration", value=self._conviction_text(stats["conviction"]), inline=False)
        embed.set_footer(text="Emotions as data — Denise Shull, Market Mind Games")
        return embed

    @staticmethod
    def _conviction_text(conviction: dict) -> str:
        """Explain whether felt certainty actually predicts being right."""
        if not conviction["n"]:
            return "*No conviction scores logged yet — pass `conviction:` to `/state`.*"

        lines = []
        for name, low, high in ps.CONVICTION_BUCKETS:
            bucket = conviction["buckets"][name]
            if bucket["n"]:
                lines.append(f"`{name:<6}` ({low}-{high}) — {Psych._fmt(bucket)}")

        verdict = conviction["predictive"]
        if verdict is True:
            lines.append("\n✅ Your conviction **tracks reality** — you win more when you feel surer. That's a real signal; size with it.")
        elif verdict is False:
            lines.append("\n⚠️ Your conviction **does not predict outcome** — you're no more right when you feel sure. "
                         "Shull's read: that confidence is an emotional state, not information. Don't size on it.")
        else:
            lines.append("\n*Not enough decided trades at both ends of the scale to judge yet.*")

        return "\n".join(lines)

    async def _send_synthesis(self, interaction, stats: dict, entries: list[dict]):
        """Ask the reasoning model to interpret the numbers through Shull's lens."""
        status = await interaction.followup.send(
            f"🧠 Synthesizing with `{DEEPSEEK_REASONING_MODEL}`..."
        )

        prompt = f"""You are a trading performance coach trained in Denise Shull's method
(Market Mind Games / The ReThink Group).

Core commitments you must honour:
- Emotions are DATA, not noise. Never tell the trader to "be less emotional",
  "stay disciplined", or "remove emotion" — Shull explicitly rejects that framing.
  It is not about controlling emotions, it is about controlling behaviour.
- Distinguish fC (physical/feelings context), eC (emotional context) and
  F-eC (fractal emotional context — the recurring story from much earlier in life).
- Treat the fear spectrum (fear of losing ⟷ fear of missing out) as the main axis.
- Intuition is quiet and pattern-based; impulse is urgent and usually about
  recovering something. Do not conflate them.

Here is the trader's own logged data, correlated against results (json):
{json.dumps(stats, indent=2, default=str)}

Here are the individual records (json):
{json.dumps(entries[:40], indent=2, default=str)}

TASK — respond in json with this exact shape:
{{"headline": "one sentence naming the single clearest pattern",
  "patterns": ["specific observations tied to the numbers above"],
  "fractal_probe": ["questions that push toward the F-eC — what recurring story might be running"],
  "experiments": ["concrete, checkable things to try over the next 10 trades"]}}

Be specific and quantitative — cite the actual win rates and R figures. If the
sample is too small for a claim, say so rather than inventing a pattern."""

        fallback = {"headline": "", "patterns": [], "fractal_probe": [], "experiments": []}
        try:
            # deep=True runs at max reasoning effort, and thinking tokens count
            # against max_tokens. The budget has to cover the reasoning as well
            # as the ~800 tokens of JSON we actually want back.
            result = await llm_service.complete_json(prompt, fallback, deep=True, max_tokens=16000)
        except LLMError as e:
            await status.edit(content=f"❌ Synthesis failed: {e}")
            return

        if not any(result.get(k) for k in fallback):
            await status.edit(content="⚠️ The model didn't return a usable synthesis. The numbers above still stand.")
            return

        embed = discord.Embed(
            title="🔬 Pattern Synthesis",
            description=result.get("headline") or discord.utils.MISSING,
            color=discord.Color.dark_purple()
        )
        for name, key in [("Patterns", "patterns"),
                          ("F-eC probes — sit with these", "fractal_probe"),
                          ("Experiments for the next 10 trades", "experiments")]:
            items = result.get(key) or []
            if items:
                embed.add_field(name=name, value="\n".join(f"• {i}" for i in items[:6]), inline=False)

        await status.edit(content=None, embed=embed)


async def setup(bot):
    await bot.add_cog(Psych(bot))
