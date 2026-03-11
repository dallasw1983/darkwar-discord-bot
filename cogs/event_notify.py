from __future__ import annotations

import os
import asyncio
from typing import Optional
from datetime import timedelta

import discord
from discord.ext import commands
from datetime import datetime, timezone

from core.logger import write_log
from services.telegram import TelegramService


class EventNotifyCog(commands.Cog):
    """
    Sends Telegram notifications when events start.

    This cog now *detects* event start itself by listening for Discord Scheduled Event updates
    (status transition -> ACTIVE). When detected, it formats a short announcement and sends
    it to Telegram.

    Notes:
    - Requires the bot to have the `guild_scheduled_events` intent enabled.
      (discord.Intents.guild_scheduled_events = True)
    - Requires the bot to have permission to view scheduled events in the guild.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Optional Telegram notifier (send-only). If env vars are missing, cog still loads.
        self.telegram: Optional[TelegramService] = None
        tg_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        tg_chat = (os.getenv("TELEGRAM_CHAT_ANNOUNCEMENTS_ID") or "").strip()

        if tg_token and tg_chat:
            self.telegram = TelegramService(token=tg_token, chat_id=tg_chat)
            
        self._poll_task: asyncio.Task | None = None
        self._announced: set[int] = set()

        # Optional: restrict to a single guild (recommended if bot is in multiple guilds)
        raw_guild = (os.getenv("GUILD_ID") or "").strip()
        self.guild_id: int | None = int(raw_guild) if raw_guild.isdigit() else None

    async def cog_load(self) -> None:
        # Warn early if intent isn't enabled; the cog will load, but won't detect updates.
        write_log(f"[EventNotifyCog] Loaded. guild_id={self.guild_id} telegram={'yes' if self.telegram else 'no'}")
        try:
            self._poll_task = asyncio.create_task(self._poll_scheduled_events())
            intents = getattr(self.bot, "intents", None)
            if intents and not getattr(intents, "guild_scheduled_events", False):
                write_log(
                    "[EventNotifyCog] WARNING: bot intents.guild_scheduled_events is disabled; "
                    "scheduled event start detection will not work."
                )
        except Exception:
            pass

        if self.telegram:
            try:
                await self.telegram.start()
                write_log("[EventNotifyCog] Started.")
            except Exception as e:
                write_log(f"[EventNotifyCog] start failed: {e}")
                self.telegram = None
    async def cog_unload(self) -> None:
        if self.telegram:
            try:
                await self.telegram.close()
                write_log("[EventNotifyCog] Closed.")
            except Exception as e:
                write_log(f"[EventNotifyCog] close failed: {e}")
            if self._poll_task:
                self._poll_task.cancel()

    # ----------------------------
    # Discord event start detection
    # ----------------------------
    @commands.Cog.listener()
    async def on_guild_scheduled_event_update(
        self,
        before: discord.GuildScheduledEvent,
        after: discord.GuildScheduledEvent,
    ) -> None:
        
        print(discord.GuildScheduledEvent.status)
        """
        Fires whenever a scheduled event changes. We notify when it transitions to ACTIVE.
        """
        
        try:
            if self.guild_id is not None and getattr(after.guild, "id", None) != self.guild_id:
                return

            if before.status == after.status:
                return

            if after.status != discord.GuildScheduledEventStatus.active:
                return

            # Build message parts
            event_name = after.name or "Scheduled Event"
            when_text = self._format_when(after.start_time)
            details = self._format_details(after)

            await self.notify_event_start(
                event_name=event_name,
                details=details,
                when_text=when_text,
            )
        except Exception as e:
            write_log(f"[EventNotifyCog] scheduled event update handler failed: {e}")

    async def _poll_scheduled_events(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                now = datetime.now(timezone.utc)

                for guild in self.bot.guilds:
                    if self.guild_id is not None and guild.id != self.guild_id:
                        continue
                    
                    # Fetch scheduled events from API (more reliable than cache)
                    events = await guild.fetch_scheduled_events()

                    for ev in events:
                        if ev.id in self._announced:
                            continue
                            
                        # Get the start time and continue if not within
                        start = ev.start_time
                        if not start:
                            continue
                        delta = now - start
                    
                        # Only announce when it *should* be starting
                        if not (timedelta(0) <= delta <= timedelta(minutes=10)):
                            continue
                            
                        # If Discord never flips to ACTIVE automatically, we still announce.
                        event_name = ev.name or "Scheduled Event"
                        when_text = start
                        details = self._format_details(ev)

                        await self.notify_event_start(
                            event_name=event_name,
                            details=details,
                            when_text=start,
                        )
                        self._announced.add(ev.id)

            except Exception as e:
                write_log(f"[EventNotifyCog] scheduled-event poll failed: {e}")

            await asyncio.sleep(30)  # poll interval
            
    @commands.Cog.listener()
    async def on_guild_scheduled_event_create(self, event: discord.GuildScheduledEvent) -> None:
        """
        Optional: if an event is created already ACTIVE (rare), also notify.
        """
        try:
            if self.guild_id is not None and getattr(event.guild, "id", None) != self.guild_id:
                return
            if event.status == discord.GuildScheduledEventStatus.active:
                event_name = event.name or "Scheduled Event"
                when_text = self._format_when(event.start_time)
                details = self._format_details(event)
                await self.notify_event_start(event_name=event_name, details=details, when_text=when_text)
        except Exception as e:
            write_log(f"[EventNotifyCog] scheduled event create handler failed: {e}")

    # -------------------------
    # Telegram notification API
    # -------------------------
    async def notify_event_start(
        self,
        *,
        event_name: str,
        details: str | None = None,
        when_text: str | None = None,
    ) -> None:
        """
        Send the Telegram message.
        Non-blocking behavior like NoticeCog (won't break the event if Telegram is down).
        """

        if not self.telegram:
            return

        parts = [f"🚨 Event Started: {event_name}"]
        if when_text:
            parts.append(f"🕒 {when_text}")
        if details:
            parts.append(details)

        msg = "\n".join([p for p in parts if p])

        async def _send() -> None:
            try:
                await self.telegram.send_message(msg)
            except Exception as e:
                write_log(f"[EventNotifyCog] Telegram send failed: {e}")

        asyncio.create_task(_send())

    # -------------------------
    # Helpers
    # -------------------------
    def _format_when(self, dt: Optional[discord.utils.MISSING] | Optional[discord.datetime.datetime]) -> str | None:
        # discord.py uses datetime objects (timezone-aware). We'll print a readable string.
        if not dt:
            return None
        try:
            # Example: 2026-01-14 18:30 PST
            # tzinfo name varies; keep it simple and stable.
            return dt.strftime("%Y-%m-%d %H:%M %Z").strip()
        except Exception:
            return None

    def _format_details(self, event: discord.GuildScheduledEvent) -> str | None:
        lines: list[str] = []
        try:
            if event.location and not event.location.startswith("https://discord.com"):
                lines.append(f"📍 {event.location}")
        except Exception:
            pass

        try:
            desc = (event.description or "").strip()
            if desc:
                # Avoid huge walls of text in Telegram
                if len(desc) > 800:
                    desc = desc[:800].rstrip() + "…"
                lines.append(f"\n{desc}")
        except Exception:
            pass

        try:
            if event.url and not event.url.startswith("https://discord.com"):
                lines.append(f"\n🔗 {event.url}")
        except Exception:
            pass

        return "\n".join([l for l in lines if l]).strip() or None


async def setup(bot: commands.Bot):
    await bot.add_cog(EventNotifyCog(bot))
