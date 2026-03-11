import os
import asyncio
import discord
from discord.ext import commands
from datetime import datetime

from core.config import GUILD_ID
from core.logger import write_log
from core.permissions import require_admin_or_owner

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.guild_scheduled_events = True

class MyBot(commands.Bot):
    async def setup_hook(self) -> None:
        # Load cogs/extensions here (async-safe, runs before on_ready)
        await self.load_extension("cogs.bubbleup")
        await self.load_extension("cogs.notice")
        await self.load_extension("cogs.translate")
        await self.load_extension("cogs.onboarding")
        await self.load_extension("cogs.admin")
        await self.load_extension("cogs.telegram_sync")
        await self.load_extension("cogs.event_notify")

bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    write_log(f"Logged in as {bot.user} (ID: {bot.user.id})")
    write_log(f"Current time: {datetime.now()}")

    # --- GLOBAL COMMANDS (defined in code) ---
    global_cmds = bot.tree.get_commands()
    write_log(f"Global commands loaded in code: {len(global_cmds)}")
    if global_cmds:
        write_log("Global commands: " + ", ".join(cmd.name for cmd in global_cmds))

    # --- GUILD SYNC ---
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)

        # Copy global → guild for instant availability
        bot.tree.copy_global_to(guild=guild)

        guild_cmds = await bot.tree.sync(guild=guild)
        write_log(f"Guild commands synced to {GUILD_ID}: {len(guild_cmds)}")

        if guild_cmds:
            write_log("Guild commands: " + ", ".join(cmd.name for cmd in guild_cmds))
    else:
        synced = await bot.tree.sync()
        write_log(f"Global commands synced: {len(synced)}")



async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in environment or .env file.")

    # Recommended modern start pattern
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
