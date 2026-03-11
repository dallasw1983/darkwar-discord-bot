# services/telegram.py
import os
import aiohttp
import hashlib
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class TelegramMessage:
    chat_id: str
    message_id: int
    text: str
    username: str
    is_bot: bool
    from_id: int | None
    date: int  # unix epoch seconds

    # Photo support 
    has_photo: bool = False
    photo_file_id: str | None = None
    photo_unique_id: str | None = None
    photo_caption: str | None = None
    
    # Photo Reaction Support
    event_type: str = "message"          # "message" | "reaction"
    reaction_emoji: str | None = None    # e.g. "👍" (single emoji only)
    reaction_is_added: bool | None = None
    reaction_target_message_id: int | None = None



class TelegramService:
    """
    Minimal Telegram Bot API wrapper for:
      - sending text
      - sending photos
      - polling updates (text + photo)
    Includes loop prevention using recent photo hashes.
    """

    def __init__(self, token: str, chat_id: str):
        token = (token or "").strip()
        chat_id = (chat_id or "").strip()

        if not token:
            raise RuntimeError("TelegramService: TELEGRAM_BOT_TOKEN is missing")
        if not chat_id:
            raise RuntimeError("TelegramService: chat_id is missing")

        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{self.token}"

        self.session: aiohttp.ClientSession | None = None
        self.offset: Optional[int] = None

        # Used for loop prevention (ignore only messages sent by OUR bot)
        raw_id = (os.getenv("TELEGRAM_BOT_ID") or "").strip()
        self.bot_id: int | None = int(raw_id) if raw_id.isdigit() else None

        # NEW: recent photo hash cache for loop prevention
        # Store (timestamp, sha256hex). Keep short TTL to prevent ping-pong loops.
        self._recent_photo_hashes: deque[tuple[float, str]] = deque()
        self._photo_hash_ttl_seconds = int(os.getenv("TELEGRAM_PHOTO_HASH_TTL_SECONDS") or 90)

    async def start(self) -> None:
        if self.session and not self.session.closed:
            return
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def send_message(self, text: str) -> int | None:
        text = (text or "").strip()
        if not text:
            return None
        data = await self._post("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        })
        return (data.get("result") or {}).get("message_id")


    # -------------------------
    # NEW: Send Photo
    # -------------------------
    async def send_photo(self, photo_bytes: bytes, filename: str = "photo.jpg", caption: str = "") -> int | None:
        if not photo_bytes:
            return None

        # (keep your hash-cache remember here if you added it)

        if not self.session:
            raise RuntimeError("TelegramService not started (call await start())")

        url = f"{self.base_url}/sendPhoto"
        form = aiohttp.FormData()
        form.add_field("chat_id", self.chat_id)
        if caption:
            form.add_field("caption", caption[:1024])
        form.add_field("photo", photo_bytes, filename=filename, content_type="application/octet-stream")

        async with self.session.post(url, data=form) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error calling sendPhoto: {data}")
            return (data.get("result") or {}).get("message_id")

    # -------------------------
    # Senc Reactions
    # -------------------------
    async def set_reaction(self, chat_id: str, message_id: int, emoji: str | None) -> None:
        payload = {"chat_id": str(chat_id), "message_id": int(message_id)}
        if emoji:
            payload["reaction"] = [{"type": "emoji", "emoji": emoji}]
        else:
            payload["reaction"] = []  # clear reaction(s)
        await self._post("setMessageReaction", payload)
        
    # -------------------------
    # Poll Updates
    # -------------------------
    async def poll(self, timeout: int = 30) -> AsyncIterator[TelegramMessage]:
        """
        Long-poll getUpdates and yield TelegramMessage items.
        Call this from a single loop (do not overlap).
        """
        payload = {
            "timeout": int(timeout),
            "allowed_updates": ["message", "channel_post", "message_reaction"],
        }

        if self.offset is not None:
            payload["offset"] = self.offset

        data = await self._post("getUpdates", payload)

        for update in data.get("result", []):
            self.offset = update["update_id"] + 1

            # --- REACTIONS FIRST (these updates do NOT have "message") ---
            mr = update.get("message_reaction")
            if mr:
                #Debug 
                # print("TG reaction update seen:", mr)
                chat = mr.get("chat", {}) or {}
                if str(chat.get("id")) != self.chat_id:
                    continue

                target_mid = int(mr.get("message_id"))
                new_reactions = mr.get("new_reaction") or []
                old_reactions = mr.get("old_reaction") or []

                def _first_emoji(lst):
                    for r in lst:
                        if r.get("type") == "emoji" and r.get("emoji"):
                            return r["emoji"]
                    return None

                new_emoji = _first_emoji(new_reactions)
                old_emoji = _first_emoji(old_reactions)

                yield TelegramMessage(
                    chat_id=str(chat.get("id")),
                    message_id=target_mid,
                    text="",
                    username="Telegram",
                    is_bot=False,
                    from_id=None,
                    date=int(time.time()),
                    event_type="reaction",
                    reaction_emoji=(new_emoji or old_emoji),
                    reaction_is_added=bool(new_emoji),
                    reaction_target_message_id=target_mid,
                )
                continue

            # --- NORMAL MESSAGES AFTER ---
            msg = update.get("message") or update.get("channel_post")
            if not msg:
                continue

            chat = msg.get("chat", {}) or {}
            if str(chat.get("id")) != self.chat_id:
                continue

            frm = msg.get("from") or {}
            sender_chat = msg.get("sender_chat") or {}

            # Determine display username
            if sender_chat:
                username = sender_chat.get("title") or chat.get("title") or "Telegram"
            elif not frm.get("is_bot"):
                username = frm.get("username") or frm.get("first_name") or "Telegram User"
            else:
                username = chat.get("title") or "Telegram"

            is_bot = bool(frm.get("is_bot", False))
            from_id = frm.get("id")
            date = int(msg.get("date", 0))
            message_id = int(msg.get("message_id"))

            # TEXT or CAPTION
            text = (msg.get("text") or msg.get("caption") or "").strip()

            # PHOTO handling
            photos = msg.get("photo")  # array of sizes, last usually best
            if photos and isinstance(photos, list):
                best = photos[-1]
                file_id = best.get("file_id")
                unique_id = best.get("file_unique_id")
                caption = (msg.get("caption") or "").strip() or None

                # Loop prevention via recent hashes:
                #  - If this came from our bot, we *still* might want to ignore it.
                #  - To be clean and reliable, hash the bytes (download) and compare.
                try:
                    if file_id and await self._is_recent_photo_by_hash(file_id):
                        continue
                except Exception:
                    # If hashing fails (network hiccup), don't drop the message silently.
                    # Fall back to only ignoring our own bot messages if we can detect it.
                    if self.bot_id is not None and from_id == self.bot_id:
                        continue

                yield TelegramMessage(
                    chat_id=str(chat.get("id")),
                    message_id=message_id,
                    text=text,  # often same as caption; kept for compatibility
                    username=username,
                    is_bot=is_bot,
                    from_id=from_id,
                    date=date,
                    has_photo=True,
                    photo_file_id=file_id,
                    photo_unique_id=unique_id,
                    photo_caption=caption,
                )
                continue

            # If no photo, require text content (same behavior you had before)
            if not text:
                continue

            yield TelegramMessage(
                chat_id=str(chat.get("id")),
                message_id=message_id,
                text=text,
                username=username,
                is_bot=is_bot,
                from_id=from_id,
                date=date,
            )

    # -------------------------
    # File Helpers (NEW)
    # -------------------------
    async def download_file_bytes(self, file_id: str) -> bytes:
        """
        Given a Telegram file_id, fetch file_path via getFile and download bytes.
        """
        if not file_id:
            raise ValueError("download_file_bytes: file_id is required")
        info = await self._post("getFile", {"file_id": file_id})
        result = info.get("result") or {}
        file_path = result.get("file_path")
        if not file_path:
            raise RuntimeError(f"Telegram getFile missing file_path: {info}")

        if not self.session:
            raise RuntimeError("TelegramService not started (call await start())")

        url = f"{self.file_base_url}/{file_path}"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Telegram file download failed {resp.status}: {url}")
            return await resp.read()

    async def _is_recent_photo_by_hash(self, file_id: str) -> bool:
        """
        Download Telegram photo bytes, hash them, and check against our recent cache.
        If it matches, remember again (freshen) and return True (ignore).
        """
        self._prune_photo_hashes()
        b = await self.download_file_bytes(file_id)
        sha = self._sha256(b)
        if self._has_photo_hash(sha):
            self._remember_photo_hash(sha)  # freshen
            return True
        return False

    def _sha256(self, b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    def _remember_photo_hash(self, sha: str) -> None:
        self._prune_photo_hashes()
        self._recent_photo_hashes.append((time.time(), sha))

    def _has_photo_hash(self, sha: str) -> bool:
        self._prune_photo_hashes()
        return any(h == sha for _, h in self._recent_photo_hashes)

    def _prune_photo_hashes(self) -> None:
        ttl = self._photo_hash_ttl_seconds
        cutoff = time.time() - ttl
        while self._recent_photo_hashes and self._recent_photo_hashes[0][0] < cutoff:
            self._recent_photo_hashes.popleft()

    async def _post(self, method: str, payload: dict) -> dict:
        if not self.session:
            raise RuntimeError("TelegramService not started (call await start())")

        url = f"{self.base_url}/{method}"
        async with self.session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error calling {method}: {data}")
            return data
