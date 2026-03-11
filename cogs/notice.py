from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

from core.config import NOTICE_CHANNEL_ID, NOTICE_ARCHIVE_CHANNEL_ID
from core.logger import write_log
from core.storage import DATA_DIR, load_json, save_json
from services.telegram import TelegramService


NOTICE_STATE_FILE = DATA_DIR / "notice_state.json"


@dataclass
class NoticeState:
    current_notice_message_id: Optional[int] = None
    # message_id -> set(user_id)
    ack_data: dict[int, set[int]] = None  # type: ignore

    def __post_init__(self):
        if self.ack_data is None:
            self.ack_data = {}

    @staticmethod
    def load() -> "NoticeState":
        raw = load_json(NOTICE_STATE_FILE, {})
        state = NoticeState()

        try:
            if isinstance(raw, dict):
                cid = raw.get("current_notice_message_id")
                state.current_notice_message_id = int(cid) if cid else None

                raw_acks = raw.get("acks", {})
                ack_data: dict[int, set[int]] = {}
                if isinstance(raw_acks, dict):
                    for mid_str, user_list in raw_acks.items():
                        try:
                            mid = int(mid_str)
                        except Exception:
                            continue
                        if isinstance(user_list, list):
                            ack_data[mid] = set(int(x) for x in user_list if str(x).isdigit())
                        else:
                            ack_data[mid] = set()
                state.ack_data = ack_data
        except Exception as e:
            write_log(f"NoticeState.load parse error: {e}")

        write_log(
            f"Loaded notice state: current_message_id={state.current_notice_message_id}, "
            f"acks for {len(state.ack_data)} message(s)."
        )
        return state

    def save(self) -> None:
        serializable_acks = {str(mid): sorted(list(uids)) for mid, uids in self.ack_data.items()}
        data = {
            "current_notice_message_id": self.current_notice_message_id,
            "acks": serializable_acks,
        }
        save_json(NOTICE_STATE_FILE, data)
        write_log(
            f"Saved notice state: current_message_id={self.current_notice_message_id}, "
            f"acks for {len(self.ack_data)} message(s)."
        )


class AcknowledgeView(discord.ui.View):
    """Persistent View with a single acknowledge button."""

    def __init__(self, cog: "NoticeCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Acknowledge",
        style=discord.ButtonStyle.green,
        custom_id="notice_ack_button",  # must stay stable for persistent view
    )
    async def acknowledge(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.cog.handle_ack(interaction)


class NoticeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = NoticeState.load()

        # Optional Telegram notifier (send-only). If env vars are missing, Notices will still work.
        self.telegram: TelegramService | None = None
        tg_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        tg_chat = (os.getenv("TELEGRAM_CHAT_ANNOUNCEMENTS_ID") or "").strip()
        if tg_token and tg_chat:
            self.telegram = TelegramService(token=tg_token, chat_id=tg_chat)

        # Register persistent view so button works after restarts
        self.bot.add_view(AcknowledgeView(self))

    async def cog_load(self) -> None:
        if self.telegram:
            try:
                await self.telegram.start()
                write_log("[NoticeCog] Notice Service started,")
            except Exception as e:
                write_log(f"NoticeCog: Notice start failed: {e}")
                self.telegram = None

    async def cog_unload(self) -> None:
        if self.telegram:
            try:
                await self.telegram.close()
            except Exception as e:
                write_log(f"NoticeCog: TelegramService close failed: {e}")

    async def handle_ack(self, interaction: discord.Interaction) -> None:
        msg = interaction.message
        mid = msg.id

        users = self.state.ack_data.setdefault(mid, set())
        if interaction.user.id in users:
            await interaction.response.send_message(
                "You already acknowledged this notice. 👍",
                ephemeral=True,
            )
            return

        users.add(interaction.user.id)
        self.state.save()

        await interaction.response.send_message(
            "Thanks for acknowledging this notice! ✅",
            ephemeral=True,
        )

    # ---------- /notice ----------
    @app_commands.command(
        name="notice",
        description="Post a tracked notice message and archive the previous one.",
    )
    @app_commands.describe(text="The notice text to send as the new notice.")
    @app_commands.default_permissions(manage_messages=True)
    async def notice(self, interaction: discord.Interaction, text: str):
        if NOTICE_CHANNEL_ID is None or NOTICE_ARCHIVE_CHANNEL_ID is None:
            await interaction.response.send_message(
                "⚠️ NOTICE_CHANNEL_ID or NOTICE_ARCHIVE_CHANNEL_ID is not set in the environment.",
                ephemeral=True,
            )
            return

        notice_channel = self.bot.get_channel(NOTICE_CHANNEL_ID)
        archive_channel = self.bot.get_channel(NOTICE_ARCHIVE_CHANNEL_ID)

        if not isinstance(notice_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "⚠️ Notice channel not found or not a text channel.",
                ephemeral=True,
            )
            return

        if not isinstance(archive_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "⚠️ Archive channel not found or not a text channel.",
                ephemeral=True,
            )
            return

        # 1) Archive previous notice if one exists
        old_id = self.state.current_notice_message_id
        if old_id is not None:
            old_msg = None
            try:
                old_msg = await notice_channel.fetch_message(old_id)
            except discord.NotFound:
                old_msg = None
            except Exception as e:
                write_log(f"Error fetching previous notice message: {e}")
                old_msg = None

            if old_msg is not None:
                users = self.state.ack_data.get(old_id, set())
                if users:
                    ack_lines = ", ".join(f"<@{uid}>" for uid in users)
                    ack_text = f"✅ Acknowledged by {len(users)} member(s): {ack_lines}"
                else:
                    ack_text = "⚪ No one acknowledged this notice."

                archive_content = (
                    "-# 📦 **Archived Notice**\n"
                    f"-# Originally posted in {notice_channel.mention} on "
                    f"{discord.utils.format_dt(old_msg.created_at, style='F')}\n\n"
                    f"-# {old_msg.content}\n\n"
                    f"-# {ack_text}"
                )

                # prevent accidental pings
                archive_content = archive_content.replace("@everyone", "`@everyone`")
                archive_content = archive_content.replace("@here", "`@here`")

                try:
                    await archive_channel.send(archive_content)
                    write_log(f"Archived notice message {old_id} to channel {NOTICE_ARCHIVE_CHANNEL_ID}.")
                except Exception as e:
                    write_log(f"Error sending archived notice: {e}")

                try:
                    await old_msg.delete()
                    write_log(f"Deleted previous notice message {old_id} from channel {NOTICE_CHANNEL_ID}.")
                except Exception as e:
                    write_log(f"Error deleting old notice message: {e}")

        # 2) Post new notice with an Acknowledge button
        view = AcknowledgeView(self)
        try:
            new_msg = await notice_channel.send(f"@everyone {text}", view=view)

            # Telegram notify (non-blocking). Won't fail the Discord notice if Telegram is down.
            if self.telegram:
                ts = discord.utils.utcnow().astimezone().strftime("%Y-%m-%d %I:%M %p")

                async def _send_telegram() -> None:
                    try:
                        await self.telegram.send_message(f"[Notice • {ts}]\n{text}")
                    except Exception as e:
                        write_log(f"NoticeCog: Telegram send failed: {e}")

                asyncio.create_task(_send_telegram())

        except Exception as e:
            write_log(f"Error sending new notice message: {e}")
            await interaction.response.send_message(
                "❌ Failed to send the notice message. Check my permissions in that channel.",
                ephemeral=True,
            )
            return

        self.state.current_notice_message_id = new_msg.id
        self.state.ack_data[self.state.current_notice_message_id] = set()
        self.state.save()

        write_log(
            f"New notice posted by {interaction.user} "
            f"in channel {NOTICE_CHANNEL_ID}, message_id={self.state.current_notice_message_id}."
        )

        await interaction.response.send_message(
            f"📢 New notice posted in {notice_channel.mention} and previous notice archived.",
            ephemeral=True,
        )

    # ---------- /notice_read ----------
    @app_commands.command(
        name="notice_read",
        description="Show who has acknowledged the current notice.",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def notice_read(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "⚠️ This command can only be used in a server.",
                ephemeral=True,
            )
            return

        mid = self.state.current_notice_message_id
        if mid is None:
            await interaction.response.send_message(
                "ℹ️ There is no active notice right now.",
                ephemeral=True,
            )
            return

        user_ids = self.state.ack_data.get(mid, set())
        if not user_ids:
            await interaction.response.send_message(
                "⚪ No one has acknowledged the current notice yet.",
                ephemeral=True,
            )
            return

        members_lines: list[str] = []
        for uid in sorted(user_ids):
            member = guild.get_member(uid)
            if member:
                # discriminator is being phased out, but display_name is fine
                members_lines.append(f"- {member.mention} (`{member.name}`)")
            else:
                members_lines.append(f"- <@{uid}> (no longer in this server?)")

        content = "\n".join(
            [f"✅ **{len(user_ids)} member(s) have acknowledged the current notice.**", ""]
            + members_lines
        )

        await interaction.response.send_message(content, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(NoticeCog(bot))
