from __future__ import annotations

from typing import Iterable
import discord
from discord import app_commands


def has_any_role(member: discord.abc.User, role_names: Iterable[str]) -> bool:
    """True if member has any role matching role_names (by role.name)."""
    roles = getattr(member, "roles", None)
    if not roles:
        return False
    wanted = set(role_names)
    return any(getattr(r, "name", None) in wanted for r in roles)


def is_r4_or_r5(interaction: discord.Interaction) -> bool:
    """Convenience helper for your common admin tier."""
    return has_any_role(interaction.user, ("R4", "R5"))


def require_any_role(*role_names: str) -> app_commands.Check:
    """
    App-command check that requires the invoking user to have any of the given roles.
    Usage:
        @require_any_role("R4", "R5")
        async def cmd(...):
            ...
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        return has_any_role(interaction.user, role_names)
    return app_commands.check(predicate)


def require_guild() -> app_commands.Check:
    """App-command check: must be used in a server (not in DMs)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None
    return app_commands.check(predicate)

BOT_OWNER_ID = 367409804676956171  # <-- your Discord ID

def require_admin_or_owner() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == BOT_OWNER_ID:
            return True
        return has_any_role(interaction.user, ("Admin",))
    return app_commands.check(predicate)
