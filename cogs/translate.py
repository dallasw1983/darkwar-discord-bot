from __future__ import annotations

from typing import Optional, TYPE_CHECKING

import discord
from discord.ext import commands
from discord import app_commands

from core.logger import write_log
from core.storage import DATA_DIR, load_json, save_json

LANG_PREFS_FILE = DATA_DIR / "user_lang_prefs.json"

# Translation backend (optional)
try:
    from deep_translator import GoogleTranslator  # type: ignore
except Exception:
    GoogleTranslator = None  # type: ignore


def _chunk_text(text: str, max_len: int = 1900) -> list[str]:
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""

    for line in lines:
        to_add = line if not current else "\n" + line
        if len(current) + len(to_add) > max_len:
            if current:
                chunks.append(current)
            if len(line) > max_len:
                start = 0
                while start < len(line):
                    chunks.append(line[start : start + max_len])
                    start += max_len
                current = ""
            else:
                current = line
        else:
            current += to_add

    if current:
        chunks.append(current)
    return chunks


class TranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_lang_prefs: dict[str, str] = {}
        self.load_lang_prefs()

    def load_lang_prefs(self) -> None:
        raw = load_json(LANG_PREFS_FILE, {})
        if isinstance(raw, dict):
            self.user_lang_prefs = {str(k): str(v) for k, v in raw.items()}
        else:
            self.user_lang_prefs = {}
        write_log(f"Loaded language prefs for {len(self.user_lang_prefs)} user(s).")

    def save_lang_prefs(self) -> None:
        save_json(LANG_PREFS_FILE, self.user_lang_prefs)
        write_log(f"Saved language prefs for {len(self.user_lang_prefs)} user(s).")

    def get_user_lang(self, user_id: int) -> Optional[str]:
        return self.user_lang_prefs.get(str(user_id))

    @app_commands.command(
        name="setlang",
        description="Set your preferred translation language (e.g. en, fr, es, ja).",
    )
    @app_commands.describe(lang_code="Language code like en, fr, es, ja")
    async def set_language(self, interaction: discord.Interaction, lang_code: str):
        lang_code = lang_code.lower().strip()
        if not (2 <= len(lang_code) <= 10):
            await interaction.response.send_message(
                "❌ That doesn’t look like a valid language code. "
                "Try `en`, `fr`, `es`, `ja`, etc.",
                ephemeral=True,
            )
            return

        self.user_lang_prefs[str(interaction.user.id)] = lang_code
        self.save_lang_prefs()
        await interaction.response.send_message(
            f"✅ Your preferred translation language is now set to `{lang_code}`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="tr",
        description="Translate the given text into your preferred language.",
    )
    @app_commands.describe(text="The text you want translated")
    async def translate_text(self, interaction: discord.Interaction, text: str):
        if GoogleTranslator is None:
            await interaction.response.send_message(
                "❌ Translation library not available on this bot instance.",
                ephemeral=True,
            )
            return

        target_lang = self.get_user_lang(interaction.user.id)
        if not target_lang:
            await interaction.response.send_message(
                "🌐 You haven't set a language yet. Use `/setlang xx` first (example: `/setlang en`).",
                ephemeral=True,
            )
            return

        try:
            translated = GoogleTranslator(source="auto", target=target_lang).translate(text)
            await interaction.response.send_message(
                f"🌐 **Translated → `{target_lang}` for {interaction.user.mention}:**\n{translated}"
            )
        except Exception as e:
            write_log(f"Translate error (tr): {e}")
            await interaction.response.send_message(
                "❌ Error translating that text. Please try again later.",
                ephemeral=True,
            )

    @app_commands.command(
        name="trl",
        description="Translate the most recent message(s) into your preferred language.",
    )
    @app_commands.describe(lines="How many recent user messages to translate (default 1).")
    async def translate_last(self, interaction: discord.Interaction, lines: Optional[int] = 1):
        if GoogleTranslator is None:
            await interaction.response.send_message(
                "❌ Translation library not available.",
                ephemeral=True,
            )
            return

        target_lang = self.get_user_lang(interaction.user.id)
        if not target_lang:
            await interaction.response.send_message(
                "🌐 You haven't set a language yet. Use `/setlang xx` first.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "⚠️ Couldn't access this channel's history.",
                ephemeral=True,
            )
            return

        if lines is None or lines <= 0:
            lines = 1
        if lines > 50:
            lines = 50

        collected: list[discord.Message] = []
        async for msg in channel.history(limit=200):
            if msg.author.bot:
                continue
            if not msg.content or not msg.content.strip():
                continue
            collected.append(msg)
            if len(collected) >= lines:
                break

        if not collected:
            await interaction.response.send_message(
                "⚠️ I could only find image/attachment messages with no text to translate.",
                ephemeral=True,
            )
            return

        collected.reverse()

        original_lines: list[str] = []
        translated_lines: list[str] = []

        for m in collected:
            original_lines.append(f"**{m.author.display_name}:** {m.content}")

            source_lang = self.get_user_lang(m.author.id)
            try:
                translated_text = GoogleTranslator(
                    source=source_lang or "auto",
                    target=target_lang,
                ).translate(m.content)
            except Exception as e:
                write_log(f"Translate error (trl per-message) user {m.author.id}: {e}")
                translated_text = "❌ (error translating this line)"

            translated_lines.append(f"**{m.author.display_name}:** {translated_text}")

        original_block = "\n".join(f"> {line}" for line in original_lines)
        translation_block = "\n".join(translated_lines)

        out = (
            f"🌐 **Translated → `{target_lang}` for {interaction.user.mention}:**\n\n"
            f"🧾 **Original ({len(collected)} message(s))**:\n"
            f"{original_block}\n\n"
            f"✅ **Translation:**\n"
            f"{translation_block}"
        )

        chunks = _chunk_text(out, 1900)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for extra in chunks[1:]:
            await interaction.followup.send(extra, ephemeral=True)


# --- MODULE-LEVEL CONTEXT MENU (must NOT be inside a class) ---

@app_commands.context_menu(name="Translate")
async def translate_context_menu(interaction: discord.Interaction, message: discord.Message):
    if GoogleTranslator is None:
        await interaction.response.send_message(
            "❌ Translation library not available on this bot instance.",
            ephemeral=True,
        )
        return

    cog = interaction.client.get_cog("TranslateCog")
    if not isinstance(cog, TranslateCog):
        await interaction.response.send_message(
            "❌ Translate system not initialized properly (missing cog).",
            ephemeral=True,
        )
        return

    write_log(
        f"Context menu 'Translate' used by {interaction.user} (ID: {interaction.user.id}) "
        f"on message ID {message.id} in #{getattr(interaction.channel, 'name', 'unknown')}"
    )

    target_lang = cog.get_user_lang(interaction.user.id)
    if not target_lang:
        await interaction.response.send_message(
            "🌐 You haven't set a language yet. Use `/setlang xx` first (example: `/setlang en`).",
            ephemeral=True,
        )
        return

    if not message.content or not message.content.strip():
        await interaction.response.send_message(
            "⚠️ That message has no text content to translate.",
            ephemeral=True,
        )
        return

    try:
        translated = GoogleTranslator(source="auto", target=target_lang).translate(message.content)
        await interaction.response.send_message(
            f"🌐 **Translated → `{target_lang}` for {interaction.user.mention}:**\n"
            f"🧾 Original by {message.author.mention}:\n"
            f"> {message.content}\n\n"
            f"✅ Translation:\n{translated}",
            ephemeral=True,
        )
    except Exception as e:
        write_log(f"Translate error (context menu): {e}")
        await interaction.response.send_message(
            "❌ Error translating that message. Please try again later.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TranslateCog(bot))

    # Register context menu command on the command tree
    bot.tree.add_command(translate_context_menu)
