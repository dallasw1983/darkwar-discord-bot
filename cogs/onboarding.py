from __future__ import annotations

import os
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

from core.config import WELCOME_CHANNEL_ID
from core.logger import write_log
from core.permissions import require_guild


class OnBoardingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- welcome event ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not WELCOME_CHANNEL_ID:
            write_log(f"Member joined ({member} / {member.id}) but WELCOME_CHANNEL_ID not set.")
            return

        channel = self.bot.get_channel(WELCOME_CHANNEL_ID)
        if channel is None or not isinstance(channel, discord.TextChannel):
            write_log(f"Welcome channel with ID {WELCOME_CHANNEL_ID} not found or invalid.")
            return

        msg = (
            f"🎉 Welcome to the server, {member.mention}!\n"
            "Please make sure your Discord name matches your Dark War name."
            "Once someone adds you, then you'll have full access to our server. 😊"
        )
        try:
            await channel.send(msg)
        except Exception as e:
            write_log(f"Failed to send welcome message: {e}")

    # ---- /new_member ----
    @app_commands.command(
        name="new_member",
        description="List members with no roles. Optionally ping a role and include a message.",
    )
    @app_commands.describe(
        role="Optional role to ping (e.g., duty staff).",
        message="Optional custom message to send with the list.",
    )
    @require_guild()
    @app_commands.default_permissions(manage_roles=True)
    async def new_member(
        self,
        interaction: discord.Interaction,
        role: Optional[discord.Role] = None,
        message: Optional[str] = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "⚠️ This command can only be used in a server.",
                ephemeral=True,
            )
            return

        # Members with only @everyone role (len(roles) == 1)
        unroled = [m for m in guild.members if not m.bot and len(m.roles) == 1]

        if not unroled:
            await interaction.response.send_message(
                "✅ Everyone in this server has at least one role.",
                ephemeral=True,
            )
            return

        if message is None:
            # Keep your existing env behavior
            message = (os.getenv("NEW_MEMBER_MSG") or "New members need roles:").replace("\\n", "\n")

        lines: list[str] = [message, ""]
        if role:
            lines.append(f"{role.mention} – here are the members without roles:")
        else:
            lines.append("Here are the members without roles:")

        for member in unroled:
            # discriminator is being phased out; keep readable tag
            lines.append(f"- {member.mention} (`{member.name}`)")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OnBoardingCog(bot))
