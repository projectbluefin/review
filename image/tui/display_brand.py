"""The one image-owned display-brand setting shared by the TUI surfaces."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DISPLAY_BRAND = "Review"
IMAGE_DISPLAY_BRAND_FILE = Path("/opt/bluefin/config/display-brand")
SOURCE_DISPLAY_BRAND_FILE = Path(__file__).resolve().parents[1] / "config" / "display-brand"
MAX_DISPLAY_BRAND_LENGTH = 48


def _default_file() -> Path:
    if IMAGE_DISPLAY_BRAND_FILE.is_file():
        return IMAGE_DISPLAY_BRAND_FILE
    return SOURCE_DISPLAY_BRAND_FILE


def display_brand(path: str | os.PathLike[str] | None = None) -> str:
    """Read the image-owned display name, falling back to generic ``Review``."""
    try:
        content = Path(path) if path is not None else _default_file()
        lines = content.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return DEFAULT_DISPLAY_BRAND
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        safe = "".join(
            character
            for character in value
            if character >= " " and character != "\x7f"
        )[:MAX_DISPLAY_BRAND_LENGTH]
        return safe or DEFAULT_DISPLAY_BRAND
    return DEFAULT_DISPLAY_BRAND


def display_title(surface: str, path: str | os.PathLike[str] | None = None) -> str:
    """Prefix a functional screen label with the configured display name."""
    return f"{display_brand(path)} · {surface}"
