from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pyrogram


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _utf16_slice(s: str, start: int, end: int) -> str:
    result: list[str] = []
    pos = 0
    for ch in s:
        if pos >= end:
            break
        width = 2 if ord(ch) > 0xFFFF else 1
        if pos >= start:
            result.append(ch)
        pos += width
    return "".join(result)


def entities_to_markdown(text: str, entities: list[pyrogram.types.MessageEntity] | None) -> str:
    """Flatten Telegram message entities into a Markdown-like string."""
    if not entities:
        return text
    E = pyrogram.enums.MessageEntityType
    sorted_entities = sorted(entities, key=lambda e: (e.offset, -e.length))
    parts: list[str] = []
    cursor = 0
    total_utf16 = _utf16_len(text)
    for entity in sorted_entities:
        e_start = entity.offset
        e_end = min(entity.offset + entity.length, total_utf16)
        if e_start >= total_utf16 or e_start < cursor:
            continue
        if e_start > cursor:
            parts.append(_utf16_slice(text, cursor, e_start))
        span = _utf16_slice(text, e_start, e_end)
        match entity.type:
            case E.TEXT_LINK:
                parts.append(f"[{span}]({entity.url or ''})")
            case E.TEXT_MENTION:
                user_id = entity.user.id if entity.user else 0
                parts.append(f"[{span}](tg://user?id={user_id})")
            case E.BOLD:
                parts.append(f"**{span}**")
            case E.ITALIC:
                parts.append(f"_{span}_")
            case E.CODE:
                parts.append(f"`{span}`")
            case E.PRE:
                lang = entity.language or ""
                parts.append(f"```{lang}\n{span}\n```")
            case E.STRIKETHROUGH:
                parts.append(f"~~{span}~~")
            case E.SPOILER:
                parts.append(f"||{span}||")
            case E.BLOCKQUOTE:
                parts.append("\n".join(f"> {line}" for line in span.splitlines()))
            case _:
                parts.append(span)
        cursor = e_end
    if cursor < total_utf16:
        parts.append(_utf16_slice(text, cursor, total_utf16))
    return "".join(parts)


def _iter_values(value: Any) -> Iterable[Any]:
    if value is None or isinstance(value, str | int | float | bool):
        return ()
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, list | tuple | set):
        return value
    return ()


def _rich_type_name(obj: Any) -> str:
    return obj.__class__.__name__.lower()


def _get_field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _rich_text_to_markdown(obj: Any, seen: set[int]) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, int | float | bool):
        return str(obj)
    oid = id(obj)
    if oid in seen:
        return ""
    seen.add(oid)
    name = _rich_type_name(obj)
    direct_text = _get_field(obj, "text", "string", "content")
    url = _get_field(obj, "url")
    expression = _get_field(obj, "expression")
    alternative_text = _get_field(obj, "alternative_text")
    if isinstance(expression, str):
        text = expression
    elif isinstance(alternative_text, str):
        text = alternative_text
    elif isinstance(direct_text, str):
        text = direct_text
    else:
        children = _get_field(obj, "texts", "children", "parts", "rich_text", "caption")
        if children is None:
            children = list(_iter_values(obj))
        if isinstance(children, list | tuple | set):
            text = "".join(_rich_text_to_markdown(v, seen) for v in children)
        else:
            text = _rich_text_to_markdown(children, seen)
    if not text:
        return ""
    if "bold" in name:
        return f"**{text}**"
    if "italic" in name:
        return f"_{text}_"
    if "underline" in name:
        return f"<u>{text}</u>"
    if "strikethrough" in name:
        return f"~~{text}~~"
    if "spoiler" in name:
        return f"||{text}||"
    if "code" in name:
        return f"`{text}`"
    if "url" in name or "link" in name:
        return f"[{text}]({url})" if url else text
    if "mention" in name:
        return f"@{text.lstrip('@')}"
    if "mathematicalexpression" in name:
        return f"${text}$"
    if "subscript" in name:
        return f"~{text}~"
    if "superscript" in name:
        return f"^{text}^"
    if "marked" in name:
        return f"=={text}=="
    return text


def _rich_blocks_to_markdown(blocks: Any, seen: set[int] | None = None) -> str:
    seen = seen or set()
    if blocks is None:
        return ""
    if not isinstance(blocks, list | tuple | set):
        blocks = [blocks]
    rendered = [_rich_block_to_markdown(block, seen) for block in blocks]
    return "\n\n".join(part for part in rendered if part)


def _rich_block_to_markdown(obj: Any, seen: set[int]) -> str:
    if obj is None:
        return ""
    oid = id(obj)
    if oid in seen:
        return ""
    seen.add(oid)
    name = _rich_type_name(obj)
    text_obj = _get_field(obj, "text", "rich_text", "caption", "title")
    text = _rich_text_to_markdown(text_obj, seen) if text_obj is not None else ""
    if "sectionheading" in name:
        size = _get_field(obj, "size") or 2
        return f"{'#' * max(1, min(int(size), 6))} {text}" if text else ""
    if "preformatted" in name:
        language = _get_field(obj, "language") or ""
        return f"```{language}\n{text}\n```" if text else ""
    if "mathematicalexpression" in name:
        expression = _get_field(obj, "expression") or text
        return f"$$\n{expression}\n$$" if expression else ""
    if "divider" in name:
        return "---"
    if "listitem" in name:
        body = _rich_blocks_to_markdown(_get_field(obj, "blocks") or [], seen)
        label = _get_field(obj, "label") or "-"
        checkbox = ""
        if _get_field(obj, "has_checkbox"):
            checkbox = "[x] " if _get_field(obj, "is_checked") else "[ ] "
        return f"{label} {checkbox}{body}".strip()
    if name.endswith("richblocklist") or name == "richblocklist":
        items = _get_field(obj, "items") or []
        return "\n".join(
            part for part in (_rich_block_to_markdown(item, seen) for item in items) if part
        )
    if "blockquotation" in name:
        body = _rich_blocks_to_markdown(_get_field(obj, "blocks") or [], seen)
        credit = _rich_text_to_markdown(_get_field(obj, "credit"), seen)
        quoted = "\n".join(f"> {line}" for line in body.splitlines()) if body else ""
        return "\n".join(part for part in (quoted, f"> — {credit}" if credit else "") if part)
    if "pullquotation" in name:
        credit = _rich_text_to_markdown(_get_field(obj, "credit"), seen)
        quoted = "\n".join(f"> {line}" for line in text.splitlines()) if text else ""
        return "\n".join(part for part in (quoted, f"> — {credit}" if credit else "") if part)
    if "tablecell" in name:
        return text
    if name.endswith("richblocktable") or name == "richblocktable":
        rows = _get_field(obj, "cells") or []
        rendered = []
        for row in rows:
            rendered.append(
                " | ".join(_rich_block_to_markdown(cell, seen) for cell in row)
            )
        caption = _rich_text_to_markdown(_get_field(obj, "caption"), seen)
        return "\n".join(part for part in (caption, *rendered) if part)
    if "details" in name:
        summary = _rich_text_to_markdown(_get_field(obj, "summary"), seen)
        body = _rich_blocks_to_markdown(_get_field(obj, "blocks") or [], seen)
        return "\n".join(part for part in (summary, body) if part)
    if any(kind in name for kind in ("collage", "slideshow")):
        body = _rich_blocks_to_markdown(_get_field(obj, "blocks") or [], seen)
        caption = _rich_text_to_markdown(_get_field(obj, "caption"), seen)
        return "\n".join(part for part in (caption, body) if part)
    if any(kind in name for kind in ("photo", "video", "audio", "voicenote", "animation", "map")):
        caption = _rich_text_to_markdown(_get_field(obj, "caption"), seen)
        label = name.removeprefix("richblock")
        return f"[{label}{': ' + caption if caption else ''}]"
    if "thinking" in name:
        body = _rich_blocks_to_markdown(_get_field(obj, "blocks") or [], seen)
        return f"[thinking]\n{body}" if body else "[thinking]"
    if text:
        return text
    blocks = _get_field(obj, "blocks", "children", "items")
    if blocks is not None:
        return _rich_blocks_to_markdown(blocks, seen)
    return ""


def rich_message_to_markdown(rich_message: Any) -> str:
    """Best-effort RichMessage/RichBlock tree flattener for model input."""
    if rich_message is None:
        return ""
    blocks = _get_field(rich_message, "blocks", "content", "children")
    if blocks is not None:
        return _rich_blocks_to_markdown(blocks)
    text = _get_field(rich_message, "text", "rich_text")
    if text is not None:
        return _rich_text_to_markdown(text, set())
    return _rich_block_to_markdown(rich_message, set())


def get_message_text_markdown(message: pyrogram.types.Message) -> str:
    """Return message text/caption as Markdown-like text, including RichMessage."""
    raw_text = message.text or message.caption or ""
    if raw_text:
        return entities_to_markdown(raw_text, message.entities or message.caption_entities)
    return rich_message_to_markdown(getattr(message, "rich_message", None)).strip()
