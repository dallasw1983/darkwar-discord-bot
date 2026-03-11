from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Literal

import discord
from discord.ext import commands, tasks
from discord import app_commands

from core.config import BUBBLE_CHANNEL_ID, env_msg
from core.logger import write_log
from core.storage import DATA_DIR, load_json, save_json
from core.permissions import require_any_role

# ---------- files ----------
BUBBLE_CONFIG_FILE = DATA_DIR / "bubble_config.json"
BUBBLE_STATE_FILE = DATA_DIR / "bubble_state.json"
USER_BUBBLE_DM_FILE = DATA_DIR / "user_bubble_dm.json"

# ---------- defaults ----------
DEFAULT_BUBBLE_CONFIG: dict[str, object] = {
    "BUBBLE_WEEKDAY": 4,          # Friday
    "HOURLY_START_HOUR": 16,
    "HOURLY_END_HOUR": 18,
    "HOURLY_MINUTE": 0,
    "BUBBLE_PERIOD_HOURS": 8,
    "BUBBLE_WARNING_MINUTES": 10,
    "CAMPAIGN_LENGTH_DAYS": 1,
    "CAMPAIGN_END_HOUR": 18,
    "CAMPAIGN_END_MINUTE": 0,
    "REMIND_ENABLED": True,
}

def _to_int(v: object, fallback: int) -> int:
    try:
        if isinstance(v, bool):
            return int(v)
        return int(v)  # handles int/float/str
    except Exception:
        return fallback

def _to_bool(v: object, fallback: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return fallback

# -------------- BUBBLE CONFIG UI (ported from legacy bot) --------------

class CampaignWindowModal(discord.ui.Modal, title="Edit Campaign Window"):
    """Modal to edit campaign length and end time."""

    days = discord.ui.TextInput(
        label="Campaign Length Days (1–7)",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=1,
        required=True,
    )
    end_hour = discord.ui.TextInput(
        label="End Hour (0–23)",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=2,
        required=True,
    )
    end_minute = discord.ui.TextInput(
        label="End Minute (0, 15, 30, 45)",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=2,
        required=True,
    )

    def __init__(self, cog: "BubbleUpCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        try:
            d = int(str(self.days.value).strip())
            h = int(str(self.end_hour.value).strip())
            m = int(str(self.end_minute.value).strip())

            if not (1 <= d <= 14):
                raise ValueError("days out of range")
            if not (0 <= h <= 23):
                raise ValueError("hour out of range")
            if m not in (0, 15, 30, 45):
                raise ValueError("minute must be 0, 15, 30, or 45")

            self.cog.bubble_config["CAMPAIGN_LENGTH_DAYS"] = d
            self.cog.bubble_config["CAMPAIGN_END_HOUR"] = h
            self.cog.bubble_config["CAMPAIGN_END_MINUTE"] = m
            self.cog.save_config()
            self.cog.maybe_start_new_cycle(datetime.now())

            await interaction.response.send_message(
                f"✅ Campaign updated: {d} day(s), ending at {h:02d}:{m:02d}.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Invalid values: {e}", ephemeral=True)


class WeekdaySelect(discord.ui.Select):
    def __init__(self, cog: "BubbleUpCog"):
        self.cog = cog

        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        current = cog.cfg_int("BUBBLE_WEEKDAY")
        options: list[discord.SelectOption] = []
        for idx, label in enumerate(weekday_labels):
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(idx),
                    default=(idx == current),
                )
            )

        super().__init__(
            placeholder="Bubble Day (Mon–Sun)",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        self.cog.bubble_config["BUBBLE_WEEKDAY"] = int(self.values[0])
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=view.build_description(), view=view)


class StartHourSelect(discord.ui.Select):
    def __init__(self, cog: "BubbleUpCog"):
        self.cog = cog

        current = cog.cfg_int("HOURLY_START_HOUR")
        options: list[discord.SelectOption] = []
        for h in [14, 15, 16, 17, 18]:
            options.append(
                discord.SelectOption(
                    label=f"{h:02d}:xx",
                    value=str(h),
                    default=(h == current),
                )
            )

        super().__init__(
            placeholder="Start Hour",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        self.cog.bubble_config["HOURLY_START_HOUR"] = int(self.values[0])
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=view.build_description(), view=view)


class EndHourSelect(discord.ui.Select):
    def __init__(self, cog: "BubbleUpCog"):
        self.cog = cog

        current = cog.cfg_int("HOURLY_END_HOUR")
        options: list[discord.SelectOption] = []
        for h in [14, 15, 16, 17, 18]:
            options.append(
                discord.SelectOption(
                    label=f"{h:02d}:xx",
                    value=str(h),
                    default=(h == current),
                )
            )

        super().__init__(
            placeholder="End Hour",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        self.cog.bubble_config["HOURLY_END_HOUR"] = int(self.values[0])
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=view.build_description(), view=view)


class BubbleConfigView(discord.ui.View):
    """Interactive UI for Bubble Up config (dropdowns + buttons)."""

    def __init__(self, cog: "BubbleUpCog"):
        super().__init__(timeout=300)
        self.cog = cog

        # Row 0–2: three selects (one per row)
        self.add_item(WeekdaySelect(cog))
        self.add_item(StartHourSelect(cog))
        self.add_item(EndHourSelect(cog))

        # Display-only status button (Row 4)
        enabled = cog.cfg_bool("REMIND_ENABLED")
        self.add_item(
            discord.ui.Button(
                label="Reminders: ON" if enabled else "Reminders: OFF",
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger,
                disabled=True,
                row=4,
            )
        )

    def build_description(self) -> str:
        weekday = self.cog.cfg_int("BUBBLE_WEEKDAY")
        weekday_name_list = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        weekday_name = weekday_name_list[weekday] if 0 <= weekday < len(weekday_name_list) else str(weekday)

        return (
            "🫧 **Bubble Up Configuration**\n"
            "\n"
            f"-# **Weekday:** {weekday_name} (index {weekday})\n"
            f"-# **Hourly Start:** {self.cog.cfg_int('HOURLY_START_HOUR'):02d}:{self.cog.cfg_int('HOURLY_MINUTE'):02d}\n"
            f"-# **Hourly End:** {self.cog.cfg_int('HOURLY_END_HOUR'):02d}:{self.cog.cfg_int('HOURLY_MINUTE'):02d}\n"
            f"-# **Bubble Period:** {self.cog.cfg_int('BUBBLE_PERIOD_HOURS')} hours\n"
            f"-# **Warning Before Expiry:** {self.cog.cfg_int('BUBBLE_WARNING_MINUTES')} minutes\n"
            f"-# **Campaign Length:** {self.cog.cfg_int('CAMPAIGN_LENGTH_DAYS')} day(s)\n"
            f"-# **Campaign End Time:** {self.cog.cfg_int('CAMPAIGN_END_HOUR'):02d}:{self.cog.cfg_int('CAMPAIGN_END_MINUTE'):02d}\n"
            f"-# **Reminders Enabled:** {'Yes' if self.cog.cfg_bool('REMIND_ENABLED') else 'No'}\n"
            "\n"
            "Use the dropdowns and buttons below to change values."
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Allow anyone to open/view, but callbacks enforce R4/R5 for changes
        return True

    # ---- Row 3: Minute & Period buttons ----

    @discord.ui.button(label=":00", style=discord.ButtonStyle.secondary, row=3)
    async def minute_00(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_minute(interaction, 0)

    @discord.ui.button(label=":15", style=discord.ButtonStyle.secondary, row=3)
    async def minute_15(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_minute(interaction, 15)

    @discord.ui.button(label=":30", style=discord.ButtonStyle.secondary, row=3)
    async def minute_30(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_minute(interaction, 30)

    @discord.ui.button(label=":45", style=discord.ButtonStyle.secondary, row=3)
    async def minute_45(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_minute(interaction, 45)

    @discord.ui.button(label="Period 8/24h", style=discord.ButtonStyle.primary, row=3)
    async def toggle_period(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        current = self.cog.cfg_int("BUBBLE_PERIOD_HOURS")
        self.cog.bubble_config["BUBBLE_PERIOD_HOURS"] = 24 if current == 8 else 8
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        new_view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=new_view.build_description(), view=new_view)

    async def _set_minute(self, interaction: discord.Interaction, minute: int):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        self.cog.bubble_config["HOURLY_MINUTE"] = minute
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        new_view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=new_view.build_description(), view=new_view)

    # ---- Row 4: Warning, Campaign Window, Toggle Reminders ----

    @discord.ui.button(label="Cycle Warning (10/20/30/60m)", style=discord.ButtonStyle.secondary, row=4)
    async def cycle_warning(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        options = [10, 20, 30, 60]
        current = self.cog.cfg_int("BUBBLE_WARNING_MINUTES")
        try:
            idx = options.index(current)
            new_val = options[(idx + 1) % len(options)]
        except ValueError:
            new_val = options[0]

        self.cog.bubble_config["BUBBLE_WARNING_MINUTES"] = new_val
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        new_view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=new_view.build_description(), view=new_view)

    @discord.ui.button(label="Edit Campaign Window", style=discord.ButtonStyle.primary, row=4)
    async def edit_campaign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(CampaignWindowModal(self.cog))

    @discord.ui.button(label="Toggle Reminders", style=discord.ButtonStyle.secondary, row=4)
    async def toggle_reminders(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.user_has_bubble_admin(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to edit Bubble Up config.",
                ephemeral=True,
            )
            return

        self.cog.bubble_config["REMIND_ENABLED"] = not self.cog.cfg_bool("REMIND_ENABLED")
        self.cog.save_config()
        self.cog.maybe_start_new_cycle(datetime.now())

        new_view = BubbleConfigView(self.cog)
        await interaction.response.edit_message(content=new_view.build_description(), view=new_view)


@dataclass
class BubbleState:
    cycle_start: Optional[datetime] = None
    cycle_end: Optional[datetime] = None
    last_sent: Optional[datetime] = None
    ended_notified: bool = False

    @staticmethod
    def from_json(data: dict) -> "BubbleState":
        def parse_dt(s: Optional[str]) -> Optional[datetime]:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return None

        return BubbleState(
            cycle_start=parse_dt(data.get("cycle_start")),
            cycle_end=parse_dt(data.get("cycle_end")),
            last_sent=parse_dt(data.get("last_sent")),
            ended_notified=bool(data.get("ended_notified", False)),
        )

    def to_json(self) -> dict:
        return {
            "cycle_start": self.cycle_start.isoformat() if self.cycle_start else None,
            "cycle_end": self.cycle_end.isoformat() if self.cycle_end else None,
            "last_sent": self.last_sent.isoformat() if self.last_sent else None,
            "ended_notified": self.ended_notified,
        }


class BubbleUpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # config/state loaded at runtime
        self.bubble_config: dict[str, object] = DEFAULT_BUBBLE_CONFIG.copy()
        self.state: BubbleState = BubbleState()
        self.user_bubble_dm: dict[str, bool] = {}

        self.load_all()

        # start loop
        if not self.bubble_up_reminder.is_running():
            self.bubble_up_reminder.start()
            write_log("[BubbleUpCog] Started bubble_up_reminder loop.")

    # ---------- config helpers ----------
    def cfg_int(self, key: str) -> int:
        return _to_int(self.bubble_config.get(key), _to_int(DEFAULT_BUBBLE_CONFIG[key], 0))

    def cfg_bool(self, key: str) -> bool:
        return _to_bool(self.bubble_config.get(key), bool(DEFAULT_BUBBLE_CONFIG[key]))

    def save_config(self) -> None:
        save_json(BUBBLE_CONFIG_FILE, self.bubble_config)

    def load_config(self) -> None:
        raw = load_json(BUBBLE_CONFIG_FILE, DEFAULT_BUBBLE_CONFIG.copy())
        merged = DEFAULT_BUBBLE_CONFIG.copy()
        if isinstance(raw, dict):
            merged.update(raw)
        self.bubble_config = merged

    def save_state(self) -> None:
        save_json(BUBBLE_STATE_FILE, self.state.to_json())

    def load_state(self) -> None:
        raw = load_json(BUBBLE_STATE_FILE, {})
        if isinstance(raw, dict):
            self.state = BubbleState.from_json(raw)
        else:
            self.state = BubbleState()

    def save_user_dm_prefs(self) -> None:
        save_json(USER_BUBBLE_DM_FILE, self.user_bubble_dm)

    def load_user_dm_prefs(self) -> None:
        raw = load_json(USER_BUBBLE_DM_FILE, {})
        if isinstance(raw, dict):
            # force bool
            self.user_bubble_dm = {str(k): bool(v) for k, v in raw.items()}
        else:
            self.user_bubble_dm = {}

    def load_all(self) -> None:
        self.load_config()
        self.load_state()
        self.load_user_dm_prefs()

        write_log(
            "BubbleUpCog config loaded: "
            f"weekday={self.cfg_int('BUBBLE_WEEKDAY')}, "
            f"hourly {self.cfg_int('HOURLY_START_HOUR')}-{self.cfg_int('HOURLY_END_HOUR')} "
            f"@:{self.cfg_int('HOURLY_MINUTE'):02d}, "
            f"period={self.cfg_int('BUBBLE_PERIOD_HOURS')}h "
            f"warn={self.cfg_int('BUBBLE_WARNING_MINUTES')}m, "
            f"campaign={self.cfg_int('CAMPAIGN_LENGTH_DAYS')}d "
            f"end={self.cfg_int('CAMPAIGN_END_HOUR'):02d}:{self.cfg_int('CAMPAIGN_END_MINUTE'):02d}, "
            f"enabled={self.cfg_bool('REMIND_ENABLED')}"
        )

    # ---------- permissions ----------
    def user_has_bubble_admin(self, interaction: discord.Interaction) -> bool:
        # Your current logic: R4 or R5
        return any(r.name in ("R4", "R5") for r in getattr(interaction.user, "roles", []))

    # ---------- cycle logic ----------
    def maybe_start_new_cycle(self, now: datetime) -> None:
        s = self.state

        # recompute end if cycle_start exists
        if s.cycle_start:
            end_date = (s.cycle_start + timedelta(days=self.cfg_int("CAMPAIGN_LENGTH_DAYS"))).date()
            s.cycle_end = datetime(
                year=end_date.year,
                month=end_date.month,
                day=end_date.day,
                hour=self.cfg_int("CAMPAIGN_END_HOUR"),
                minute=self.cfg_int("CAMPAIGN_END_MINUTE"),
                second=0,
                microsecond=0,
            )
        else:
            s.cycle_end = None

        # active campaign?
        if s.cycle_start and s.cycle_end and now <= s.cycle_end:
            return

        # only start at weekly tick
        if (
            now.weekday() == self.cfg_int("BUBBLE_WEEKDAY")
            and now.hour == self.cfg_int("HOURLY_START_HOUR")
            and now.minute == self.cfg_int("HOURLY_MINUTE")
        ):
            s.cycle_start = now.replace(second=0, microsecond=0)
            end_date = (s.cycle_start + timedelta(days=self.cfg_int("CAMPAIGN_LENGTH_DAYS"))).date()
            s.cycle_end = datetime(
                year=end_date.year,
                month=end_date.month,
                day=end_date.day,
                hour=self.cfg_int("CAMPAIGN_END_HOUR"),
                minute=self.cfg_int("CAMPAIGN_END_MINUTE"),
                second=0,
                microsecond=0,
            )
            s.last_sent = None
            s.ended_notified = False

            write_log(f"BubbleUpCog: started new campaign start={s.cycle_start}, end={s.cycle_end}")
            self.save_state()

    def should_send_bubble(self, now: datetime) -> Optional[Literal["hourly", "expiry"]]:
        s = self.state
        if not (s.cycle_start and s.cycle_end):
            return None
        if not (s.cycle_start <= now <= s.cycle_end):
            return None

        start_hour = self.cfg_int("HOURLY_START_HOUR")
        end_hour = self.cfg_int("HOURLY_END_HOUR")
        minute = self.cfg_int("HOURLY_MINUTE")

        # phase 1 hourly (start day)
        if (
            now.date() == s.cycle_start.date()
            and start_hour <= now.hour <= end_hour
            and now.minute == minute
        ):
            return "hourly"

        # phase 2 expiry warnings
        elapsed = now - s.cycle_start
        elapsed_minutes = int(elapsed.total_seconds() // 60)

        period_minutes = self.cfg_int("BUBBLE_PERIOD_HOURS") * 60
        warning_offset = self.cfg_int("BUBBLE_WARNING_MINUTES")
        first_warning_min = period_minutes - warning_offset

        if elapsed_minutes >= first_warning_min:
            if (elapsed_minutes - first_warning_min) % period_minutes == 0:
                return "expiry"
        return None

    async def send_bubble_dm_to_opted_in_users(self, mode: str, msg: str) -> None:
        if not self.user_bubble_dm:
            return

        for uid_str, enabled in list(self.user_bubble_dm.items()):
            if not enabled:
                continue
            try:
                uid = int(uid_str)
            except ValueError:
                continue

            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except Exception as e:
                    write_log(f"BubbleUpCog DM: could not fetch user {uid}: {e}")
                    continue

            if user is None:
                continue

            dm_text = f"🫧 **Bubble Up reminder ({mode})**\n\n{msg}"
            try:
                await user.send(dm_text)
            except Exception as e:
                write_log(f"BubbleUpCog DM: failed to DM user {uid}: {e}")

    # ---------- loop ----------
    @tasks.loop(minutes=1)
    async def bubble_up_reminder(self):
        now = datetime.now()

        if not self.cfg_bool("REMIND_ENABLED"):
            return

        self.maybe_start_new_cycle(now)

        s = self.state

        # cycle ended notice (once)
        if s.cycle_start and s.cycle_end:
            if now > s.cycle_end and not s.ended_notified:
                if BUBBLE_CHANNEL_ID:
                    channel = self.bot.get_channel(BUBBLE_CHANNEL_ID)
                    if isinstance(channel, (discord.TextChannel, discord.Thread)):
                        msg = env_msg("BUBBLE_CYCLE_COMPLETE_MSG", "🫧 Bubble cycle complete.")
                        end_mode = "cycle_end"
                        try:
                            await channel.send(msg)
                            await self.send_bubble_dm_to_opted_in_users(end_mode, msg)
                            s.last_sent = now
                            self.save_state()
                            write_log(f"BubbleUpCog: sent cycle_end to channel {BUBBLE_CHANNEL_ID} + DMs")
                        except Exception as e:
                            write_log(f"BubbleUpCog: failed to send cycle_end: {e}")

                s.ended_notified = True
                self.save_state()
                s.last_sent = None

        mode = self.should_send_bubble(now)
        if not mode:
            return

        # anti-double-send
        if s.last_sent and (now - s.last_sent).total_seconds() < 50:
            return

        if not BUBBLE_CHANNEL_ID:
            write_log("BubbleUpCog: reminder wanted to fire but BUBBLE_CHANNEL_ID not set.")
            return

        channel = self.bot.get_channel(BUBBLE_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            write_log(f"BubbleUpCog: bubble channel {BUBBLE_CHANNEL_ID} not found/invalid.")
            return

        msg = (
            env_msg("BUBLE_UP_REMINDER_MSG", "🫧 Bubble up!")
            if mode == "hourly"
            else env_msg("BUBBLE_EXPIRE_MSG", "⚠️ Bubble expiring soon!")
        )

        try:
            await channel.send(msg)
            await self.send_bubble_dm_to_opted_in_users(mode, msg)
            s.last_sent = now
            self.save_state()
            write_log(f"BubbleUpCog: sent bubble reminder ({mode}) to channel {BUBBLE_CHANNEL_ID} + DMs")
        except Exception as e:
            write_log(f"BubbleUpCog: failed to send reminder: {e}")

    # ---------- commands ----------
    @app_commands.command(name="bubbleup", description="Send the Bubble Up reminder message in this channel.")
    async def bubbleup(self, interaction: discord.Interaction):
        msg = env_msg("BUBLE_MANUAL_MSG", "🫧 Bubble up!")
        await interaction.response.send_message(msg)
        write_log(f"/bubbleup used by {interaction.user} in channel {getattr(interaction.channel, 'id', 'unknown')}")

    @app_commands.command(name="bubble_dm", description="Opt in or out of personal Bubble Up DM reminders.")
    @app_commands.describe(enable="True to receive DMs when Bubble Up reminders fire, false to stop them.")
    async def bubble_dm(self, interaction: discord.Interaction, enable: bool):
        user_id = str(interaction.user.id)
        self.user_bubble_dm[user_id] = enable
        self.save_user_dm_prefs()

        if enable:
            text = (
                "✅ You will now receive **DMs** whenever Bubble Up reminders are sent.\n"
                "You can run `/bubble_dm enable:false` to turn these off anytime."
            )
        else:
            text = "✅ Bubble Up DM reminders have been **disabled** for you."

        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="bubble_config", description="View or update the global Bubble Up reminder schedule.")
    @require_any_role("R4", "R5")
    async def bubble_config(
        self,
        interaction: discord.Interaction,
        weekday: Optional[int] = None,
        start_hour: Optional[int] = None,
        end_hour: Optional[int] = None,
        minute: Optional[int] = None,
        period_hours: Optional[int] = None,
        warning_minutes: Optional[int] = None,
        campaign_days: Optional[int] = None,
        campaign_end_hour: Optional[int] = None,
        campaign_end_minute: Optional[int] = None,
    ):

        changes: list[str] = []

        def update(name: str, value: int, ok: bool, label: str):
            if ok:
                self.bubble_config[name] = value
                changes.append(f"{label} → {value}")
                return True
            return False

        if weekday is not None and not update("BUBBLE_WEEKDAY", weekday, 0 <= weekday <= 6, "Weekday"):
            return await interaction.response.send_message("❌ weekday must be 0–6.", ephemeral=True)

        if start_hour is not None and not update("HOURLY_START_HOUR", start_hour, 0 <= start_hour <= 23, "Start hour"):
            return await interaction.response.send_message("❌ start_hour must be 0–23.", ephemeral=True)

        if end_hour is not None and not update("HOURLY_END_HOUR", end_hour, 0 <= end_hour <= 23, "End hour"):
            return await interaction.response.send_message("❌ end_hour must be 0–23.", ephemeral=True)

        if minute is not None and not update("HOURLY_MINUTE", minute, 0 <= minute <= 59, "Minute"):
            return await interaction.response.send_message("❌ minute must be 0–59.", ephemeral=True)

        if period_hours is not None and not update("BUBBLE_PERIOD_HOURS", period_hours, 1 <= period_hours <= 72, "Bubble period"):
            return await interaction.response.send_message("❌ period_hours must be 1–72.", ephemeral=True)

        if warning_minutes is not None and not update("BUBBLE_WARNING_MINUTES", warning_minutes, 1 <= warning_minutes <= 240, "Warning minutes"):
            return await interaction.response.send_message("❌ warning_minutes must be 1–240.", ephemeral=True)

        if campaign_days is not None and not update("CAMPAIGN_LENGTH_DAYS", campaign_days, 1 <= campaign_days <= 14, "Campaign days"):
            return await interaction.response.send_message("❌ campaign_days must be 1–14.", ephemeral=True)

        if campaign_end_hour is not None and not update("CAMPAIGN_END_HOUR", campaign_end_hour, 0 <= campaign_end_hour <= 23, "Campaign end hour"):
            return await interaction.response.send_message("❌ campaign_end_hour must be 0–23.", ephemeral=True)

        if campaign_end_minute is not None and not update(
            "CAMPAIGN_END_MINUTE",
            campaign_end_minute,
            campaign_end_minute in (0, 15, 30, 45),
            "Campaign end minute",
        ):
            return await interaction.response.send_message("❌ campaign_end_minute must be 0, 15, 30, or 45.", ephemeral=True)

        if changes:
            self.save_config()
            self.maybe_start_new_cycle(datetime.now())  # apply immediately

        summary = (
            "🫧 **Bubble Up Configuration**\n\n"
            f"- **Weekday:** {self.cfg_int('BUBBLE_WEEKDAY')}\n"
            f"- **Hourly Start:** {self.cfg_int('HOURLY_START_HOUR'):02d}:{self.cfg_int('HOURLY_MINUTE'):02d}\n"
            f"- **Hourly End:** {self.cfg_int('HOURLY_END_HOUR'):02d}:{self.cfg_int('HOURLY_MINUTE'):02d}\n"
            f"- **Bubble Period:** {self.cfg_int('BUBBLE_PERIOD_HOURS')} hours\n"
            f"- **Warning Before Expiry:** {self.cfg_int('BUBBLE_WARNING_MINUTES')} minutes\n"
            f"- **Campaign Length:** {self.cfg_int('CAMPAIGN_LENGTH_DAYS')} day(s)\n"
            f"- **Campaign End:** {self.cfg_int('CAMPAIGN_END_HOUR'):02d}:{self.cfg_int('CAMPAIGN_END_MINUTE'):02d}\n"
            f"- **Reminders Enabled:** {self.cfg_bool('REMIND_ENABLED')}\n"
        )
        if changes:
            summary += "\n✅ Updated:\n- " + "\n- ".join(changes)

        view = BubbleConfigView(self)
        await interaction.response.send_message(summary, view=view, ephemeral=True)


    @bubble_config.error
    async def bubble_config_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ You must have role **R4** or **R5** to use this command.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(BubbleUpCog(bot))
