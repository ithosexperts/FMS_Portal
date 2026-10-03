"""
Maps a SQL data type (and, for text columns, the column name itself) to the
HTML input control used by the dynamic form renderer.

No form ever hardcodes an input type - everything goes through this module.
"""
from __future__ import annotations

import re

# Column-name hints checked before falling back to the raw SQL type -
# e.g. a `varchar` column named "Remarks" should render as a <textarea>.
_NAME_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"address|addr", re.IGNORECASE), "textarea"),
    (re.compile(r"remarks?|remark", re.IGNORECASE), "textarea"),
    (re.compile(r"description|dscription|descr", re.IGNORECASE), "textarea"),
    (re.compile(r"comments?|comment", re.IGNORECASE), "textarea"),
    (re.compile(r"reason", re.IGNORECASE), "textarea"),
    (re.compile(r"email", re.IGNORECASE), "email"),
    (re.compile(r"mobile|phone", re.IGNORECASE), "tel"),
    (re.compile(r"attachment|document|photo|image", re.IGNORECASE), "file"),
]

_TYPE_MAP: dict[str, str] = {
    "varchar": "text",
    "nvarchar": "text",
    "char": "text",
    "nchar": "text",
    "text": "textarea",
    "ntext": "textarea",
    "int": "number",
    "bigint": "number",
    "smallint": "number",
    "tinyint": "number",
    "decimal": "number",
    "numeric": "number",
    "money": "number",
    "smallmoney": "number",
    "float": "number",
    "real": "number",
    "date": "date",
    "datetime": "datetime-local",
    "datetime2": "datetime-local",
    "smalldatetime": "datetime-local",
    "time": "time",
    "bit": "checkbox",
}


def map_input_type(column_name: str, data_type: str) -> str:
    """Return the HTML input type for a given column name + SQL data type."""
    name_lower = (column_name or "").lower()
    for hint, input_type in _NAME_HINTS:
        if hint.search(name_lower):
            return input_type

    return _TYPE_MAP.get((data_type or "").lower().strip(), "text")


def input_step(input_type: str, data_type: str) -> str | None:
    """Return a sensible `step` attribute for numeric inputs."""
    if input_type != "number":
        return None
    if (data_type or "").lower() in ("decimal", "numeric", "money", "smallmoney", "float", "real"):
        return "0.01"
    return "1"
