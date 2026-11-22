"""
Guild isolation for local development.

A Discord bot token identifies an *application*, not a server. Run a second
instance on a production token and it joins the gateway alongside the first,
receiving events from every guild that application was ever invited to — so a
"local test" happily auto-coaches, auto-syncs and replies inside production
channels, in duplicate.

DEV_GUILD_ID exists to make slash commands appear instantly. When it is set we
also treat it as a hard boundary: ignore everything from any other guild. That
turns a misconfigured token from a production incident into a log line.

With DEV_GUILD_ID unset (i.e. production) this is a no-op and nothing changes.
"""
from config import DEV_GUILD_ID

# Guilds we've already complained about, so the log doesn't scroll forever.
_reported: set[int] = set()


def is_locked() -> bool:
    """Whether guild isolation is active."""
    return DEV_GUILD_ID is not None


def guild_id_of(guild_or_id) -> int | None:
    """Accept a Guild, an Object, or a raw id."""
    if guild_or_id is None:
        return None
    return getattr(guild_or_id, "id", guild_or_id)


def is_allowed_guild(guild_or_id) -> bool:
    """
    True if the bot should act on events from this guild.

    Unlocked (production): always True.
    Locked (dev): True only for DEV_GUILD_ID. Blocked guilds are logged once.
    """
    if not is_locked():
        return True

    gid = guild_id_of(guild_or_id)
    if gid is None:
        return False  # No guild context: nothing to scope the action to.
    if gid == DEV_GUILD_ID:
        return True

    if gid not in _reported:
        _reported.add(gid)
        print(f"🔒 Ignoring guild {gid} — locked to DEV_GUILD_ID={DEV_GUILD_ID}. "
              f"Unset DEV_GUILD_ID to run against all guilds.")
    return False


def describe_lock(guilds) -> str:
    """A startup summary of what this instance will and won't touch."""
    if not is_locked():
        names = ", ".join(f"{g.name} ({g.id})" for g in guilds) or "none"
        return f"🌍 No guild lock — active in ALL guilds: {names}"

    allowed = [g for g in guilds if g.id == DEV_GUILD_ID]
    blocked = [g for g in guilds if g.id != DEV_GUILD_ID]

    lines = [f"🔒 Guild lock ACTIVE — DEV_GUILD_ID={DEV_GUILD_ID}"]
    if allowed:
        lines.append(f"   ✅ Acting in: {allowed[0].name} ({allowed[0].id})")
    else:
        lines.append(f"   ⚠️  Not a member of {DEV_GUILD_ID} — the bot will ignore EVERYTHING. "
                     f"Did you invite it to that server?")
    if blocked:
        lines.append(f"   🚫 Ignoring {len(blocked)} other guild(s): "
                     + ", ".join(f"{g.name} ({g.id})" for g in blocked))
        lines.append("      If any of those are production, you are running a "
                     "production token locally — use a separate dev application.")
    return "\n".join(lines)
