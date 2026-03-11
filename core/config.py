from __future__ import annotations
from dotenv import load_dotenv
import os
from typing import Optional

load_dotenv()

def get_int_env(name: str) -> Optional[int]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None

# Guild / channels
GUILD_ID: Optional[int] = get_int_env("GUILD_ID")
BUBBLE_CHANNEL_ID: Optional[int] = get_int_env("BUBBLE_CHANNEL_ID")

# Notice system channels
NOTICE_CHANNEL_ID: Optional[int] = get_int_env("NOTICE_CHANNEL_ID")
NOTICE_ARCHIVE_CHANNEL_ID: Optional[int] = get_int_env("NOTICE_ARCHIVE_CHANNEL_ID")

# Onboarding notice channels
WELCOME_CHANNEL_ID: Optional[int] = get_int_env("WELCOME_CHANNEL_ID")

# Defaults for messages (kept as env lookups so you can edit without deploy)
def env_msg(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).replace("\\n", "\n")
