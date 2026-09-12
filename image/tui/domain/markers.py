"""Pure, dependency-free display-marking helpers for the review queue.

These decide which glyph and style a row or bar uses. They read only the
process environment (``NO_COLOR``, ``BLUEFIN_REVIEW_ASCII``, ...) and never
import Textual, so the queue rendering stays testable without the TUI stack.
"""

from __future__ import annotations

import os


def _enabled_environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def no_color_requested() -> bool:
    """Honor the standard opt-out without removing textual state words."""
    return "NO_COLOR" in os.environ


def ascii_ui_requested() -> bool:
    """Use printable markers when the operator explicitly requests ASCII."""
    return _enabled_environment_flag("BLUEFIN_REVIEW_ASCII")


def reduced_motion_requested() -> bool:
    """Disable decorative queue animation when the terminal requests it."""
    return _enabled_environment_flag("BLUEFIN_REVIEW_REDUCED_MOTION") or _enabled_environment_flag(
        "TEXTUAL_REDUCED_MOTION"
    )


def ui_glyph(unicode_glyph: str, ascii_glyph: str) -> str:
    return ascii_glyph if ascii_ui_requested() else unicode_glyph


def ui_style(style: str) -> str:
    return "" if no_color_requested() else style


def ui_span(text: str, style: str) -> str:
    return f"[{style}]{text}[/]" if style else text
