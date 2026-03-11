# cogs/telegram_sync.py
import os
import asyncio
import io
import time
import hashlib
import json
import json
from pathlib import Path
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from services.telegram import TelegramService

RELAY_MARKER = "\u2063[ABYS_RELAY]\u2063"
MAX_DEDUPE = 500

# Variation selector used by some platforms (e.g. ❤️ == ❤ + VS16)
VS16 = "\ufe0f"

def normalize_emoji(e: str) -> str:
    return (e or "").replace(VS16, "")


SYNC_EMOJI_ALLOWLIST = {
    normalize_emoji("👍"),
    normalize_emoji("👎"),
    normalize_emoji("❤"),     # normalized heart (covers ❤ and ❤️)
    normalize_emoji("🔥"),
    normalize_emoji("😂"),
    normalize_emoji("😮"),
    normalize_emoji("😢"),
    normalize_emoji("😡"),
    normalize_emoji("🎉"),
    normalize_emoji("👀"),
}


def _is_custom_discord_emoji(emoji: discord.PartialEmoji) -> bool:
    return emoji.id is not None  # custom emoji has an ID


def _is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    name = (att.filename or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class TelegramSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        self.name_map_path = Path(os.getenv("TG_NAME_MAP_PATH", "data/tg_name_map.json"))
        self._name_map = {}
        self._name_map_mtime = 0.0

        raw_channel = (os.getenv("DISCORD_SYNC_CHANNEL_ID") or "").strip()
        self.discord_channel_id = int(raw_channel) if raw_channel.isdigit() else 0
        if not self.discord_channel_id:
            raise RuntimeError("TelegramSync requires DISCORD_SYNC_CHANNEL_ID")

        tz_name = os.getenv("TIMEZONE", "America/Los_Angeles")
        self.tz = ZoneInfo(tz_name)

        self.telegram = TelegramService(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_GENERAL_ID", ""),
        )

        self.dedupe = []
        self._poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._map_path = Path(os.getenv("TELEGRAM_SYNC_MAP_PATH") or "data/telegram_sync_map.json")
        self._dc_to_tg: dict[str, int] = {}  # discord_message_id (str) -> telegram_message_id (int)
        self._tg_to_dc: dict[str, int] = {}  # telegram_message_id (str) -> discord_message_id (int)


        # Photo loop-prevention: recent sha256 hashes with TTL
        self._recent_photo_hashes: deque[tuple[float, str]] = deque()
        self._photo_hash_ttl_seconds = int(os.getenv("SYNC_PHOTO_HASH_TTL_SECONDS") or 90)
    def _load_name_map_if_needed(self) -> None:
        try:
            if not self.name_map_path.exists():
                self._name_map = {}
                return

            mtime = self.name_map_path.stat().st_mtime
            if mtime == self._name_map_mtime:
                return

            with self.name_map_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            # Ensure dict[str, str]
            if isinstance(data, dict):
                self._name_map = {str(k): str(v) for k, v in data.items()}
            else:
                self._name_map = {}

            self._name_map_mtime = mtime
            print(f"[TelegramSync] Loaded TG name map: {len(self._name_map)} entries from {self.name_map_path}")
        except Exception as e:
            print(f"[TelegramSync] Failed to load name map {self.name_map_path}: {e}")
            self._name_map = {}

    def _map_display_name(self, name: str) -> str:
        self._load_name_map_if_needed()

        if not name:
            return name

        # Direct match
        if name in self._name_map:
            return self._name_map[name]

        # Optional: try lowercase match if you want it more forgiving
        lower_map = getattr(self, "_lower_name_map_cache", None)
        if lower_map is None or len(lower_map) != len(self._name_map):
            self._lower_name_map_cache = {k.lower(): v for k, v in self._name_map.items()}
            lower_map = self._lower_name_map_cache

        return lower_map.get(name.lower(), name)


    def _prune_photo_hashes(self) -> None:
        cutoff = time.time() - self._photo_hash_ttl_seconds
        while self._recent_photo_hashes and self._recent_photo_hashes[0][0] < cutoff:
            self._recent_photo_hashes.popleft()

    def _remember_photo_hash(self, sha: str) -> None:
        self._prune_photo_hashes()
        self._recent_photo_hashes.append((time.time(), sha))

    def _has_photo_hash(self, sha: str) -> bool:
        self._prune_photo_hashes()
        return any(h == sha for _, h in self._recent_photo_hashes)

    async def cog_load(self):
        self._load_message_map()   # <-- ADD THIS
        await self.telegram.start()
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_telegram_forever())
        print("[TelegramSync] loaded. bot_id =", self.telegram.bot_id, "chat_id =", self.telegram.chat_id)

    async def cog_unload(self):
        self._stop_event.set()
        if self._poll_task:
            self._poll_task.cancel()
        await self.telegram.close()
        print("[TelegramSync] unloaded.")

    # -------------------------
    # Discord -> Telegram reactions
    # -------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):

        # Ignore bot’s own reactions to prevent loops
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        # Only watch the sync channel (and optionally threads under it if you want later)
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        in_target = (payload.channel_id == self.discord_channel_id)
        if isinstance(channel, discord.Thread):
            in_target = (channel.parent_id == self.discord_channel_id)

        if not in_target:
            return

        # Map Discord message -> Telegram message
        tg_id = self.get_tg_id_for_dc(payload.message_id)
        if not tg_id:
            return

        # Only sync plain unicode emoji
        if _is_custom_discord_emoji(payload.emoji):
            return

        emoji = normalize_emoji(str(payload.emoji))
        if emoji not in SYNC_EMOJI_ALLOWLIST:
            return

        try:
            await self.telegram.set_reaction(self.telegram.chat_id, tg_id, emoji)
        except Exception as e:
            print("[TelegramSync] Discord->Telegram reaction add failed:", e)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        # Ignore bot’s own reactions to prevent loops
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        # Only watch the sync channel (and optionally threads under it if you want later)
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return
        
        in_target = (payload.channel_id == self.discord_channel_id)
        if isinstance(channel, discord.Thread):
            in_target = (channel.parent_id == self.discord_channel_id)

        if not in_target:
            return

        tg_id = self.get_tg_id_for_dc(payload.message_id)
        if not tg_id:
            return

        if _is_custom_discord_emoji(payload.emoji):
            return

        emoji = normalize_emoji(str(payload.emoji))
        if emoji not in SYNC_EMOJI_ALLOWLIST:
            return


        # Telegram’s API is “set the reaction list” not “remove a specific user’s reaction”.
        # We’ll interpret Discord remove as “clear reaction” (keeps behavior consistent).
        try:
            await self.telegram.set_reaction(self.telegram.chat_id, tg_id, None)
        except Exception as e:
            print("[TelegramSync] Discord->Telegram reaction remove failed:", e)

    
    
    # -------------------------
    # Discord -> Telegram
    # -------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ignore bots (prevents loops / webhooks)
        if message.author.bot:
            return

        # allow parent channel OR threads under it
        in_target = (message.channel.id == self.discord_channel_id)
        if isinstance(message.channel, discord.Thread):
            in_target = (message.channel.parent_id == self.discord_channel_id)

        if not in_target:
            return

        # ignore relayed content
        if message.content and RELAY_MARKER in message.content:
            return

        # Dedup discord message id
        key = f"dc:{message.id}"
        if key in self.dedupe:
            return
        self.dedupe.append(key)
        if len(self.dedupe) > MAX_DEDUPE:
            self.dedupe.pop(0)

        ts = message.created_at.astimezone(self.tz)
        header = f"{RELAY_MARKER}[Discord • {ts:%Y-%m-%d %I:%M %p}]\n{message.author.display_name}:"

        # 1) If there are image attachments, upload them to Telegram (real photo send)
        sent_any_photo = False
        for att in message.attachments:
            if not _is_image_attachment(att):
                continue

            try:
                img_bytes = await att.read()
                sha = _sha256(img_bytes)

                # Loop prevention: if this photo was recently relayed FROM Telegram, skip it
                if self._has_photo_hash(sha):
                    continue

                self._remember_photo_hash(sha)

                caption_parts = []
                if message.content:
                    caption_parts.append(message.content.strip())
                caption = "\n".join(caption_parts).strip()

                # Include header in caption (keeps source + time)
                if caption:
                    caption = f"{header}\n{caption}"
                else:
                    caption = f"{header}\n(sent a photo)"

                tg_msg_id = await self.telegram.send_photo(
                    photo_bytes=img_bytes,
                    filename=att.filename or "photo.jpg",
                    caption=caption,
                )
                if tg_msg_id:
                    self._map_pair(message.id, tg_msg_id)

                sent_any_photo = True
            except Exception as e:
                print(f"[TelegramSync] Discord->Telegram photo send failed: {e}")

        # 2) Send any remaining text / non-image attachments as a normal Telegram message
        parts = []
        if message.content and not sent_any_photo:
            # If we already sent photos with caption that includes message.content,
            # don't duplicate it as a separate text message unless you want that behavior.
            parts.append(message.content)

        for a in message.attachments:
            if _is_image_attachment(a):
                continue
            parts.append(a.url)

        body = "\n".join(parts).strip()
        if not body:
            return

        out = f"{header}\n{body}"

        try:
            tg_msg_id = await self.telegram.send_message(out)
            if tg_msg_id:
                self._map_pair(message.id, tg_msg_id)

        except Exception as e:
            print(f"[TelegramSync] Discord->Telegram send failed: {e}")

    # -------------------------
    # Telegram -> Discord (single long-poll task)
    # -------------------------
    async def _poll_telegram_forever(self):
        await self.bot.wait_until_ready()

        while not self._stop_event.is_set():
            try:
                async with self._poll_lock:
                    channel = self.bot.get_channel(self.discord_channel_id)
                    if not channel:
                        await asyncio.sleep(2)
                        continue

                    async for msg in self.telegram.poll(timeout=30):
                        # Loop guard: ignore ONLY our bot messages (not GroupAnonymousBot)
                        if self.telegram.bot_id and msg.from_id == self.telegram.bot_id:
                            continue

                        # Text loop guard
                        if msg.text and RELAY_MARKER in msg.text:
                            continue

                        ts = datetime.fromtimestamp(msg.date, self.tz)

                        # REACTION: Telegram -> Discord
                        if getattr(msg, "event_type", "message") == "reaction":
                            # Dedupe reactions by (target_mid, emoji, add/remove, date) — NOT by message_id alone
                            target_mid = msg.reaction_target_message_id or msg.message_id
                            emoji_norm = normalize_emoji(msg.reaction_emoji or "")
                            add_flag = "1" if msg.reaction_is_added else "0"
                            key = f"tgr:{target_mid}:{emoji_norm}:{add_flag}:{msg.date}"

                            if key in self.dedupe:
                                continue
                            self.dedupe.append(key)
                            if len(self.dedupe) > MAX_DEDUPE:
                                self.dedupe.pop(0)

                            try:
                                dc_id = self.get_dc_id_for_tg(target_mid)
                                if not dc_id:
                                    # Optional debug:
                                    # print(f"[TelegramSync] No DC mapping for TG message_id={target_mid} (reaction ignored)")
                                    continue

                                emoji = normalize_emoji(msg.reaction_emoji or "")
                                if not emoji or emoji not in SYNC_EMOJI_ALLOWLIST:
                                    continue


                                dc_message = await channel.fetch_message(dc_id)

                                if msg.reaction_is_added:
                                    await dc_message.add_reaction(emoji_norm)
                                else:
                                    if self.bot.user:
                                        await dc_message.remove_reaction(emoji_norm, self.bot.user)
                            except Exception as e:
                                print("[TelegramSync] Telegram->Discord reaction sync failed:", e)
                            continue

                        # --- Normal messages/photos dedupe by message_id (unchanged) ---
                        key = f"tg:{msg.message_id}"
                        if key in self.dedupe:
                            continue
                        self.dedupe.append(key)
                        if len(self.dedupe) > MAX_DEDUPE:
                            self.dedupe.pop(0)



                        # PHOTO: download and upload to Discord
                        if getattr(msg, "has_photo", False) and getattr(msg, "photo_file_id", None):
                            try:
                                img_bytes = await self.telegram.download_file_bytes(msg.photo_file_id)
                                sha = _sha256(img_bytes)

                                # Remember this so Discord->Telegram won't bounce it back
                                self._remember_photo_hash(sha)

                                caption = (msg.photo_caption or "").strip()
                                header = (
                                    f"{RELAY_MARKER}**[Telegram • {ts:%Y-%m-%d %I:%M %p}]** "
                                    f"\n{msg.username}:"
                                )
                                content = f"{header}\n{caption}" if caption else f"{header}\n(sent a photo)"

                                fp = io.BytesIO(img_bytes)
                                fp.seek(0)
                                filename = "photo.jpg"
                                dc_msg = await channel.send(content=content, file=discord.File(fp, filename=filename))
                                self._map_pair(dc_msg.id, msg.message_id)
                            except Exception as e:
                                print("[TelegramSync] Telegram->Discord photo send failed:", e)
                            continue

                        # TEXT: normal message
                        if not msg.text:
                            continue
                        mapped = self._map_display_name(msg.username)
                        
                        content = (
                            f"{RELAY_MARKER}**[Telegram • {ts:%Y-%m-%d %I:%M %p}]** "
                            f"\n{mapped}:\n{msg.text}"
                        )
                        dc_msg = await channel.send(content)
                        self._map_pair(dc_msg.id, msg.message_id)

            except asyncio.CancelledError:
                return
            except Exception as e:
                print("[TelegramSync] poll error:", e)
                await asyncio.sleep(2)
    
    # -------------------------
    # Reactions Sync
    # -------------------------
    
    def _ensure_map_dir(self) -> None:
        self._map_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_message_map(self) -> None:
        self._ensure_map_dir()
        if not self._map_path.exists():
            self._dc_to_tg = {}
            self._tg_to_dc = {}
            return

        try:
            raw = json.loads(self._map_path.read_text(encoding="utf-8"))
            self._dc_to_tg = raw.get("dc_to_tg", {}) or {}
            self._tg_to_dc = raw.get("tg_to_dc", {}) or {}
        except Exception as e:
            print(f"[TelegramSync] Failed to load map {self._map_path}: {e}")
            self._dc_to_tg = {}
            self._tg_to_dc = {}

    def _save_message_map(self) -> None:
        # keep file from growing without bound
        max_entries = int(os.getenv("TELEGRAM_SYNC_MAP_MAX_ENTRIES") or 5000)

        if len(self._dc_to_tg) > max_entries:
            # Drop oldest-ish by insertion order (Python 3.7+ dict preserves order)
            drop = len(self._dc_to_tg) - max_entries
            for k in list(self._dc_to_tg.keys())[:drop]:
                tg_id = self._dc_to_tg.pop(k, None)
                if tg_id is not None:
                    self._tg_to_dc.pop(str(tg_id), None)

        if len(self._tg_to_dc) > max_entries:
            drop = len(self._tg_to_dc) - max_entries
            for k in list(self._tg_to_dc.keys())[:drop]:
                dc_id = self._tg_to_dc.pop(k, None)
                if dc_id is not None:
                    self._dc_to_tg.pop(str(dc_id), None)

        self._ensure_map_dir()
        tmp = self._map_path.with_suffix(self._map_path.suffix + ".tmp")
        payload = {"dc_to_tg": self._dc_to_tg, "tg_to_dc": self._tg_to_dc}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._map_path)

    def _map_pair(self, discord_message_id: int, telegram_message_id: int) -> None:
        # Store both directions
        self._dc_to_tg[str(discord_message_id)] = int(telegram_message_id)
        self._tg_to_dc[str(telegram_message_id)] = int(discord_message_id)
        self._save_message_map()

    def get_tg_id_for_dc(self, discord_message_id: int) -> int | None:
        v = self._dc_to_tg.get(str(discord_message_id))
        return int(v) if v is not None else None

    def get_dc_id_for_tg(self, telegram_message_id: int) -> int | None:
        v = self._tg_to_dc.get(str(telegram_message_id))
        return int(v) if v is not None else None

async def setup(bot: commands.Bot):
    await bot.add_cog(TelegramSync(bot))
