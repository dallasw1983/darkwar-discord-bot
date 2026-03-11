from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from core.logger import write_log

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        write_log(f"Failed to load JSON {path}: {e}")
        return default

def save_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log(f"Failed to save JSON {path}: {e}")
