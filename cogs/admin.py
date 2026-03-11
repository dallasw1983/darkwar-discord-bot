from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands

from core.config import GUILD_ID
from core.logger import write_log
from core.permissions import require_admin_or_owner


def _format_ext_name(name: str) -> str:
    """Allow users to type 'bubbleup' or 'cogs.bubbleup'."""
    name = name.strip()
    if not name:
        return name
    return name if "." in name else f"cogs.{name}"


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------- /listcogs --------
    @app_commands.command(name="listcogs", description="List loaded extensions (cogs).")
    @require_admin_or_owner()
    @app_commands.default_permissions(administrator=True)
    async def listcogs(self, interaction: discord.Interaction):
        loaded = sorted(self.bot.extensions.keys())
        if not loaded:
            await interaction.response.send_message("No extensions are currently loaded.", ephemeral=True)
            return

        text = "✅ **Loaded extensions:**\n" + "\n".join(f"- `{x}`" for x in loaded)
        await interaction.response.send_message(text, ephemeral=True)

    # -------- /reload --------
    @app_commands.command(name="reload", description="Reload a cog/extension (example: bubbleup or cogs.bubbleup).")
    @require_admin_or_owner()
    @app_commands.describe(cog_name="The extension name, e.g. bubbleup or cogs.bubbleup")
    @app_commands.default_permissions(administrator=True)
    async def reload(self, interaction: discord.Interaction, cog_name: str):
        ext = _format_ext_name(cog_name)

        await interaction.response.defer(ephemeral=True)

        try:
            await self.bot.reload_extension(ext)
            write_log(f"/reload by {interaction.user} reloaded {ext}")

            # ✅ Dev convenience: auto sync to your dev guild after reload
            synced_info = ""
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                synced_info = f"\n🔁 Also synced **{len(synced)}** command(s) to guild `{GUILD_ID}`."

            await interaction.followup.send(f"✅ Reloaded `{ext}`.{synced_info}", ephemeral=True)

        except commands.ExtensionNotLoaded:
            await interaction.followup.send(f"❌ `{ext}` is not loaded.", ephemeral=True)
        except commands.ExtensionNotFound:
            await interaction.followup.send(f"❌ `{ext}` not found (check filename/module path).", ephemeral=True)
        except commands.NoEntryPointError:
            await interaction.followup.send(f"❌ `{ext}` has no `setup(bot)` entry point.", ephemeral=True)
        except commands.ExtensionFailed as e:
            await interaction.followup.send(
                f"❌ `{ext}` failed to load:\n`{type(e.original).__name__}: {e.original}`",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Reload failed: `{type(e).__name__}: {e}`", ephemeral=True)



    # -------- /sync --------
    @app_commands.command(name="sync", description="Sync application commands (guild for instant, or global).")
    @require_admin_or_owner()
    @app_commands.describe(scope="guild = instant in your dev guild, global = can take time to propagate")
    @app_commands.default_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction, scope: str = "guild"):
        scope = (scope or "guild").lower().strip()
        await interaction.response.defer(ephemeral=True)

        try:
            if scope == "guild":
                if not GUILD_ID:
                    await interaction.followup.send("❌ GUILD_ID is not set, cannot guild-sync.", ephemeral=True)
                    return

                guild = discord.Object(id=GUILD_ID)
                # copy global -> guild so guild sync actually includes everything
                self.bot.tree.copy_global_to(guild=guild)

                synced = await self.bot.tree.sync(guild=guild)
                write_log(f"/sync guild by {interaction.user}: {len(synced)} commands to {GUILD_ID}")
                names = ", ".join(c.name for c in synced) if synced else "(none)"
                await interaction.followup.send(
                    f"✅ Synced **{len(synced)}** command(s) to guild `{GUILD_ID}`.\n"
                    f"Commands: {names}",
                    ephemeral=True,
                )

            elif scope == "global":
                synced = await self.bot.tree.sync()
                write_log(f"/sync global by {interaction.user}: {len(synced)} commands")
                names = ", ".join(c.name for c in synced) if synced else "(none)"
                await interaction.followup.send(
                    f"✅ Synced **{len(synced)}** global command(s).\n"
                    f"Commands: {names}",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send("❌ scope must be `guild` or `global`.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Sync failed: `{type(e).__name__}: {e}`", ephemeral=True)
            
    # ----------------- Error handling
    @listcogs.error
    @reload.error
    @sync.error
    async def admin_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ You must have the **Admin** role to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
