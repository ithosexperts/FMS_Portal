"""
Turns a raw database column name into a human-friendly caption.

Examples
--------
Customer_Name      -> Customer Name
DRAWING_NO          -> Drawing No
inspectionDate      -> Inspection Date
S01_PLANNED         -> S01 Planned
remarks             -> Remarks
"""
from __future__ import annotations

import re

# Words that should keep a specific casing when they appear in a caption.
_ACRONYMS = {"id", "po", "no", "qty", "sap", "gst", "pan", "qr", "url", "api", "fms"}


def _split_words(name: str) -> list[str]:
    # camelCase / PascalCase boundaries -> insert a separator first.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    # Split on any run of non-alphanumeric characters (_, -, space, ...).
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    return [p for p in parts if p]


def format_caption(column_name: str) -> str:
    """Beautify a raw column name into a display caption."""
    if not column_name:
        return ""

    words = _split_words(column_name)
    formatted = []
    for word in words:
        lower = word.lower()
        if lower in _ACRONYMS:
            formatted.append(lower.upper())
        elif word.isupper() and len(word) > 1:
            formatted.append(word.capitalize())
        else:
            formatted.append(word[:1].upper() + word[1:].lower())
    return " ".join(formatted)
