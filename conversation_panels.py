"""Pure validation and readable text for the offline Navren panel format.

This module performs no I/O, state lookup, computation of user data, or actions.
An invalid panel returns ``None`` so its caller can preserve the complete source.
It is a format adapter, not an ASR/TTS or business-acceptance check.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, DecimalException
from typing import Any


_MAX_SOURCE_LENGTH = 6000
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Match ECMAScript String.trim(), including BOM, rather than Python's wider strip.
_JS_WHITESPACE = "\u0009\u000b\u000c\u0020\u00a0\ufeff\u000a\u000d\u2028\u2029\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"


class _InvalidPanel(ValueError):
    pass


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _InvalidPanel("duplicate object key")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> Any:
    raise _InvalidPanel("non-JSON numeric constant")


def _object(value: Any, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise _InvalidPanel("object keys")
    return value


def _text(value: Any, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or _CONTROLS.search(value):
        raise _InvalidPanel("text type or control character")
    value = value.strip(_JS_WHITESPACE)
    if (not value and not allow_empty) or len(value) > maximum:
        raise _InvalidPanel("text length")
    return value


def _optional_text(value: dict[str, Any], key: str, maximum: int) -> str | None:
    if key not in value:
        return None
    return _text(value[key], maximum, allow_empty=True)


def _items(value: Any, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise _InvalidPanel("item count")
    return value


def _number(value: Any, maximum: int) -> int | float:
    if type(value) is Decimal:
        # Check the JSON token before binary floating-point conversion, so an
        # out-of-range value cannot round down to the accepted upper bound.
        if not value.is_finite() or not 0 <= value <= maximum:
            raise _InvalidPanel("numeric value")
        converted = float(value)
        if not math.isfinite(converted) or (value != 0 and converted == 0):
            raise _InvalidPanel("numeric underflow")
        return converted
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise _InvalidPanel("numeric value")
    return value


def _number_text(value: int | float) -> str:
    """Use the decimal/exponent boundaries of JavaScript String(number)."""
    if value == 0:
        return "0"
    if type(value) is int:
        return str(value)
    source = repr(value)
    if abs(value) >= 1e-6:
        result = format(Decimal(source), "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    if "e" in source:
        significand, exponent = source.split("e")
        if significand.endswith(".0"):
            significand = significand[:-2]
        return significand + "e" + str(int(exponent))
    return source


def _line(lines: list[str], value: str | None) -> None:
    if value:
        lines.append(value)


def _value_with_unit(label: str, value: str, unit: str | None) -> str:
    return label + "：" + value + (" " + unit if unit else "")


def _block_lines(block: Any) -> list[str]:
    block = _object(block, {"type"}, {"title", "text", "items", "unit"})
    kind = block["type"]
    if not isinstance(kind, str):
        raise _InvalidPanel("block type")
    required = {"type", "text"} if kind == "text" else {"type", "items"}
    optional = {"title", "unit"} if kind == "bars" else {"title"}
    _object(block, required, optional)
    lines: list[str] = []
    _line(lines, _optional_text(block, "title", 40))

    if kind == "text":
        lines.append(_text(block["text"], 600))
    elif kind == "metrics":
        for item in _items(block["items"], 3):
            item = _object(item, {"label", "value"}, {"unit", "note"})
            lines.append(_value_with_unit(_text(item["label"], 40), _text(item["value"], 80), _optional_text(item, "unit", 12)))
            _line(lines, _optional_text(item, "note", 160))
    elif kind == "timeline":
        for item in _items(block["items"], 6):
            item = _object(item, {"at", "title"}, {"detail"})
            lines.append(_text(item["at"], 40) + "：" + _text(item["title"], 80))
            _line(lines, _optional_text(item, "detail", 160))
    elif kind == "checklist":
        for item in _items(block["items"], 6):
            item = _object(item, {"text"}, {"detail", "due"})
            lines.append(_text(item["text"], 120))
            _line(lines, _optional_text(item, "detail", 160))
            due = _optional_text(item, "due", 40)
            _line(lines, "截止：" + due if due else None)
    elif kind == "progress":
        for item in _items(block["items"], 6):
            item = _object(item, {"label", "value"}, {"note"})
            lines.append(_text(item["label"], 40) + "：" + _number_text(_number(item["value"], 100)) + "%")
            _line(lines, _optional_text(item, "note", 160))
    elif kind == "bars":
        unit = _optional_text(block, "unit", 12)
        for item in _items(block["items"], 6):
            item = _object(item, {"label", "value"}, set())
            lines.append(_value_with_unit(_text(item["label"], 40), _number_text(_number(item["value"], 1_000_000_000)), unit))
    elif kind == "quiz":
        for item in _items(block["items"], 5):
            item = _object(item, {"question", "answer"}, set())
            lines.append("问：" + _text(item["question"], 120))
            lines.append("答：" + _text(item["answer"], 300))
    else:
        raise _InvalidPanel("unknown block type")
    return lines


def panel_plain_text(text: str) -> str | None:
    """Return readable content only if the entire panel obeys the strict schema.

    ``text`` is the raw JSON body of a ``navren-panel`` fence, not Markdown.
    All lengths are Unicode code points; strings are trimmed but never truncated.
    This function deliberately adds no computed totals or completion state.
    """
    if not isinstance(text, str) or not text or len(text) > _MAX_SOURCE_LENGTH:
        return None
    try:
        panel = json.loads(text, object_pairs_hook=_pairs_without_duplicates,
                           parse_constant=_invalid_constant, parse_float=Decimal)
        panel = _object(panel, {"title", "blocks"}, {"summary"})
        lines = [_text(panel["title"], 40)]
        _line(lines, _optional_text(panel, "summary", 120))
        for block in _items(panel["blocks"], 3):
            lines.extend(_block_lines(block))
        return "\n".join(lines)
    except (ValueError, TypeError, OverflowError, RecursionError, DecimalException):
        return None
