import ast
import asyncio
import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyrogram
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from waku import database
from waku.config import app_config, reload_config
from waku.logger import logger
from waku.version import runtime_info_telegram_text

_SETTINGS_PATH = Path("settings.toml")
_PENDING_RELOAD_STATUS_PATH = Path("data/pending_reload_status.json")
_CALLBACK_PREFIX = "runtime_config"
_GROUP_PAGE_SIZE = 8
_VAR_PAGE_SIZE = 10
_EDIT_TIMEOUT_SECONDS = 60
_RESTART_DELAY_SECONDS = 2
_SKIP_GROUP_NUMBERS = {3, 5}
_SENSITIVE_PARTS = ("token", "secret", "password", "hash", "api_key", "key", "db_url")
_PROVIDER_DEFAULTS = {
    "url": "https://api.openai.com/v1",
    "key": "",
    "type": "chat_completions",
}
_PENDING_EDITS: dict[int, "PendingEdit"] = {}
_SESSIONS: dict[int, "ConfigSession"] = {}
_DIRTY_KEYS: dict[int, set[str]] = {}


@dataclass(frozen=True)
class ConfigGroup:
    group_id: str
    title: str
    keys: list[str]
    number: int | None = None


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    full_key: str
    value: Any
    line_index: int
    table: str | None = None


@dataclass
class ConfigSession:
    groups: dict[str, str] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    providers: dict[str, str] = field(default_factory=dict)


@dataclass
class PendingEdit:
    user_id: int
    message: pyrogram.types.Message
    mode: str
    group_id: str | None = None
    page: int = 0
    entry: ConfigEntry | None = None
    provider: str | None = None
    expires_task: asyncio.Task | None = None


def _session(user_id: int) -> ConfigSession:
    return _SESSIONS.setdefault(user_id, ConfigSession())


def _token(prefix: str, index: int) -> str:
    return f"{prefix}{index}"


def _cb(*parts: object) -> str:
    return "|".join([_CALLBACK_PREFIX, *(str(part) for part in parts)])


def _settings_text() -> str:
    return _SETTINGS_PATH.read_text(encoding="utf-8")


def _parse_scalar(raw: str) -> Any:
    value = raw.split("#", 1)[0].strip()
    try:
        return ast.literal_eval(value)
    except Exception:
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        return value.strip('"')


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _coerce_value(raw: str, old_value: Any) -> Any:
    value = raw.strip()
    if isinstance(old_value, bool):
        lowered = value.lower()
        if lowered not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("Giá trị boolean phải là true/false")
        return lowered in {"true", "1", "yes", "on"}
    if isinstance(old_value, int) and not isinstance(old_value, bool):
        return int(value)
    if isinstance(old_value, float):
        return float(value)
    if isinstance(old_value, list):
        if value.startswith("["):
            parsed = ast.literal_eval(value)
            if not isinstance(parsed, list):
                raise ValueError("Giá trị phải là list")
            return parsed
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _mask_value(key: str, value: Any) -> str:
    key_lower = key.lower()
    if any(part in key_lower for part in _SENSITIVE_PARTS):
        text = str(value or "")
        if not text:
            return ""
        if len(text) <= 8:
            return "••••"
        return f"{text[:4]}…{text[-4:]}"
    return str(value)


def _parse_settings() -> tuple[list[ConfigGroup], dict[str, ConfigEntry], list[str]]:
    groups: list[ConfigGroup] = []
    entries: dict[str, ConfigEntry] = {}
    providers: list[str] = []
    current: ConfigGroup | None = None
    current_table: str | None = None
    heading_re = re.compile(r"^#+\s*(\d+)\.\s*(.+?)\s*$")
    table_re = re.compile(r"^\[([^\]]+)]\s*$")
    assign_re = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.+)$")

    for index, line in enumerate(_settings_text().splitlines()):
        stripped = line.strip()
        heading = heading_re.match(stripped)
        if heading:
            number = int(heading.group(1))
            current_table = None
            current = None
            if number not in _SKIP_GROUP_NUMBERS:
                current = ConfigGroup(f"g{number}", heading.group(2).strip(), [], number)
                groups.append(current)
            continue

        table = table_re.match(stripped)
        if table:
            current_table = table.group(1)
            if current_table.startswith("agent_providers."):
                provider = current_table.split(".", 1)[1]
                providers.append(provider)
                current = None
            continue

        match = assign_re.match(stripped)
        if not match:
            continue
        key = match.group(1)
        full_key = f"{current_table}.{key}" if current_table else key
        entry = ConfigEntry(key, full_key, _parse_scalar(match.group(2)), index, current_table)
        entries[full_key] = entry
        if current is not None and not (current_table or "").startswith("agent_providers."):
            current.keys.append(full_key)

    if providers:
        groups.append(ConfigGroup("providers", "agent_providers", [], None))
    return [group for group in groups if group.keys or group.group_id == "providers"], entries, providers


def _find_group(group_id: str) -> ConfigGroup | None:
    groups, _, _ = _parse_settings()
    return next((group for group in groups if group.group_id == group_id), None)


def _is_dirty(owner_id: int) -> bool:
    return bool(_DIRTY_KEYS.get(owner_id))


def _is_dirty_key(owner_id: int, full_key: str) -> bool:
    return full_key in _DIRTY_KEYS.get(owner_id, set())


def _mark_dirty(owner_id: int, full_key: str) -> None:
    _DIRTY_KEYS.setdefault(owner_id, set()).add(full_key)


def _root_markup(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("⚙️", callback_data=_cb("groups", owner_id, 0)),
            InlineKeyboardButton("✖️", callback_data=_cb("close", owner_id)),
        ]]
    )


def _footer_row(
    owner_id: int,
    back_data: str | None = None,
    prev_data: str | None = None,
    next_data: str | None = None,
    include_reload: bool = False,
) -> list[InlineKeyboardButton]:
    row = []
    if prev_data:
        row.append(InlineKeyboardButton("◀️", callback_data=prev_data))
    if back_data:
        row.append(InlineKeyboardButton("↩️", callback_data=back_data))
    if include_reload and _is_dirty(owner_id):
        row.append(InlineKeyboardButton("🔄", callback_data=_cb("reload", owner_id)))
    row.append(InlineKeyboardButton("✖️", callback_data=_cb("close", owner_id)))
    if next_data:
        row.append(InlineKeyboardButton("▶️", callback_data=next_data))
    return row


def _button_rows(buttons: list[InlineKeyboardButton], columns: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + columns] for index in range(0, len(buttons), columns)]


def _entry_label(owner_id: int, entry: ConfigEntry) -> str:
    prefix = "🔄 " if _is_dirty_key(owner_id, entry.full_key) else ""
    if isinstance(entry.value, bool):
        prefix += "✅ " if entry.value else "❌ "
    return f"{prefix}{entry.key}"


def _groups_markup(owner_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    groups, _, _ = _parse_settings()
    sess = _session(owner_id)
    sess.groups.clear()
    for index, group in enumerate(groups):
        sess.groups[_token("g", index)] = group.group_id
    total_pages = max(1, (len(groups) + _GROUP_PAGE_SIZE - 1) // _GROUP_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * _GROUP_PAGE_SIZE
    rows = []
    for index, group in enumerate(groups[start : start + _GROUP_PAGE_SIZE], start):
        rows.append([InlineKeyboardButton(group.title, callback_data=_cb("group", owner_id, _token("g", index), 0))])
    prev_data = _cb("groups", owner_id, page - 1) if page > 0 else None
    next_data = _cb("groups", owner_id, page + 1) if page < total_pages - 1 else None
    rows.append(
        _footer_row(
            owner_id,
            _cb("root", owner_id),
            prev_data,
            next_data,
            include_reload=True,
        )
    )
    dirty = "Yes" if _is_dirty(owner_id) else "No"
    text = "\n".join([
        "⌬ <b>Config Variables :</b>",
        "│",
        f"┟ <b>Groups</b> → {len(groups)}",
        f"┠ <b>Page</b> → {page + 1}/{total_pages}",
        f"┖ <b>Changed</b> → {dirty}",
    ])
    return text, InlineKeyboardMarkup(rows)


def _group_markup(owner_id: int, group_id: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    if group_id == "providers":
        return _providers_markup(owner_id)
    group = _find_group(group_id)
    if group is None:
        return _groups_markup(owner_id, 0)
    _, entries, _ = _parse_settings()
    sess = _session(owner_id)
    for index, key in enumerate(group.keys):
        sess.keys[f"{group_id}_{index}"] = key
    buttons = []
    lines = [f"⌬ <b>{html.escape(group.title)} Settings :</b>", "│"]
    for idx, key in enumerate(group.keys):
        entry = entries.get(key)
        if entry is None:
            continue
        token = f"{group_id}_{idx}"
        action = "toggle" if isinstance(entry.value, bool) else "view"
        buttons.append(
            InlineKeyboardButton(
                _entry_label(owner_id, entry),
                callback_data=_cb(action, owner_id, token, group_id, 0),
            )
        )
        branch = "┖" if idx == len(group.keys) - 1 else "┠"
        value = html.escape(_mask_value(entry.full_key, entry.value))
        label = html.escape(_entry_label(owner_id, entry))
        lines.append(f"{branch} <b>{label}</b> → <code>{value}</code>")
    rows = _button_rows(buttons, 2)
    rows.append(_footer_row(owner_id, _cb("groups", owner_id, 0)))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _group_token(owner_id: int, group_id: str) -> str:
    sess = _session(owner_id)
    for token, value in sess.groups.items():
        if value == group_id:
            return token
    token = _token("g", len(sess.groups))
    sess.groups[token] = group_id
    return token


def _providers_markup(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
    _, _, providers = _parse_settings()
    sess = _session(owner_id)
    sess.providers.clear()
    buttons = []
    for index, provider in enumerate(providers):
        token = _token("p", index)
        sess.providers[token] = provider
        buttons.append(InlineKeyboardButton(provider, callback_data=_cb("provider", owner_id, token)))
    rows = _button_rows(buttons, 2)
    rows.append([InlineKeyboardButton("➕", callback_data=_cb("add_provider", owner_id))])
    rows.append(_footer_row(owner_id, _cb("groups", owner_id, 0)))
    default_status = "configured" if "default" in providers else "missing"
    text = "\n".join([
        "⌬ <b>Agent Providers :</b>",
        "│",
        f"┟ <b>Total</b> → {len(providers)}",
        f"┖ <b>Default</b> → {default_status}",
    ])
    return text, InlineKeyboardMarkup(rows)


def _provider_markup(owner_id: int, provider: str) -> tuple[str, InlineKeyboardMarkup]:
    _, entries, _ = _parse_settings()
    buttons = []
    lines = [f"⌬ <b>Provider: {html.escape(provider)} :</b>", "│"]
    for idx, field_name in enumerate(("url", "key", "type")):
        full_key = f"agent_providers.{provider}.{field_name}"
        entry = entries.get(full_key)
        if entry is None:
            continue
        token = f"provider_{provider}_{field_name}"
        _session(owner_id).keys[token] = full_key
        buttons.append(InlineKeyboardButton(_entry_label(owner_id, entry), callback_data=_cb("view", owner_id, token, "providers", 0)))
        branch = "┖" if idx == 2 else "┠"
        lines.append(f"{branch} <b>{html.escape(_entry_label(owner_id, entry))}</b> → <code>{html.escape(_mask_value(full_key, entry.value))}</code>")
    rows = _button_rows(buttons, 2)
    rows.append(_footer_row(owner_id, _cb("group", owner_id, _group_token(owner_id, "providers"), 0)))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _entry_text(entry: ConfigEntry) -> str:
    value = html.escape(_mask_value(entry.full_key, entry.value))
    return "\n".join([
        "⌬ <b>Config Variable :</b>",
        "│",
        f"┟ <b>Key</b> → <code>{html.escape(entry.full_key)}</code>",
        f"┠ <b>Type</b> → <code>{type(entry.value).__name__}</code>",
        f"┖ <b>Value</b> → <code>{value}</code>",
    ])


def _entry_markup(owner_id: int, key_token: str, group_id: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️", callback_data=_cb("edit", owner_id, key_token, group_id, page))],
        _footer_row(owner_id, _cb("group", owner_id, _group_token(owner_id, group_id), page)),
    ])


def _write_entry(entry: ConfigEntry, value: Any) -> None:
    lines = _settings_text().splitlines()
    old_line = lines[entry.line_index]
    prefix = old_line.split("=", 1)[0].rstrip()
    lines[entry.line_index] = f"{prefix} = {_toml_value(value)}"
    _SETTINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _add_provider(provider: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider):
        raise ValueError("Provider name chỉ dùng chữ/số/_/-")
    _, _, providers = _parse_settings()
    if provider in providers:
        raise ValueError("Provider này đã tồn tại")
    block = [f"[agent_providers.{provider}]"]
    block.extend(f"{key} = {_toml_value(value)}" for key, value in _PROVIDER_DEFAULTS.items())
    text = _settings_text().rstrip() + "\n\n" + "\n".join(block) + "\n"
    _SETTINGS_PATH.write_text(text, encoding="utf-8")


async def _is_admin(user: pyrogram.types.User | None) -> bool:
    if user is None:
        return False
    if user.id in app_config.owners:
        return True
    db_user = await database.get_user_by_id(user.id)
    return bool(db_user and db_user.is_bot_global_admin)


async def _edit_menu(message: pyrogram.types.Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except MessageNotModified:
        pass


async def _save_pending_reload_status(reply: pyrogram.types.Message, user_id: int, changed: list[str]) -> None:
    _PENDING_RELOAD_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"chat_id": reply.chat.id, "message_id": reply.id, "user_id": user_id, "changed": changed, "created_at": datetime.now(UTC).isoformat()}
    _PENDING_RELOAD_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _exit_after_reload(user_id: int) -> None:
    await asyncio.sleep(_RESTART_DELAY_SECONDS)
    logger.warning(f"Restarting process after config menu reload by {user_id}")
    os._exit(0)


async def _run_reload(message: pyrogram.types.Message, owner_id: int) -> None:
    success, msg, changed = reload_config()
    if not success:
        await _edit_menu(message, f"<b>❌ Waku Reload</b>\n\n<code>{html.escape(msg)}</code>", _root_markup(owner_id))
        return
    _DIRTY_KEYS.pop(owner_id, None)
    await _edit_menu(
        message,
        "\n".join(["<b>✅ Waku Reload</b>", "", "<b>Status:</b> <code>Restarting...</code>", f"<b>Changed:</b> <code>{len(changed)} fields</code>", "", "<i>Bot is restarting to apply runtime settings.</i>"]),
        None,
    )
    await _save_pending_reload_status(message, owner_id, changed)
    asyncio.create_task(_exit_after_reload(owner_id))


def _reload_confirm_markup(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅", callback_data=_cb("reload_confirm", owner_id)),
            InlineKeyboardButton("❌", callback_data=_cb("groups", owner_id, 0)),
        ]]
    )


def _reload_confirm_text(owner_id: int) -> str:
    changed = len(_DIRTY_KEYS.get(owner_id, set()))
    return "\n".join([
        "<b>🔄 Xác nhận reload</b>",
        "",
        "Reload sẽ đọc lại config và restart bot để áp dụng runtime settings.",
        f"<b>Pending:</b> <code>{changed} variables</code>",
        "",
        "Bấm ✅ để tiếp tục hoặc ❌ để huỷ.",
    ])


async def _expire_edit(user_id: int) -> None:
    await asyncio.sleep(_EDIT_TIMEOUT_SECONDS)
    pending = _PENDING_EDITS.pop(user_id, None)
    if pending:
        await _edit_menu(pending.message, "<b>⌛ Edit timeout</b>", InlineKeyboardMarkup([_footer_row(user_id)]))


@pyrogram.Client.on_message(pyrogram.filters.command("config") & pyrogram.filters.private, group=0)
async def config_command(client: pyrogram.Client, message: pyrogram.types.Message):
    user = message.from_user
    if not await _is_admin(user):
        await message.reply_text("Permission denied", quote=True)
        return
    _SESSIONS[user.id] = ConfigSession()
    await message.reply_text(runtime_info_telegram_text(), reply_markup=_root_markup(user.id), parse_mode=ParseMode.HTML, disable_web_page_preview=True, quote=True)


@pyrogram.Client.on_callback_query(pyrogram.filters.regex(rf"^{_CALLBACK_PREFIX}\|"), group=0)
async def config_callback(client: pyrogram.Client, query: pyrogram.types.CallbackQuery):
    if query.message is None or query.data is None:
        return
    parts = str(query.data).split("|")
    action = parts[1]
    owner_id = int(parts[2])
    if query.from_user is None or query.from_user.id != owner_id:
        await query.answer("Nút này không dành cho bạn.", show_alert=True, cache_time=30)
        return

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return
    if action == "reload":
        await query.answer()
        await _edit_menu(query.message, _reload_confirm_text(owner_id), _reload_confirm_markup(owner_id))
        return
    if action == "reload_confirm":
        await query.answer("Đang reload...")
        await _run_reload(query.message, owner_id)
        return
    if action == "root":
        await query.answer()
        await _edit_menu(query.message, runtime_info_telegram_text(), _root_markup(owner_id))
        return
    if action == "groups":
        text, markup = _groups_markup(owner_id, int(parts[3]))
        await query.answer()
        await _edit_menu(query.message, text, markup)
        return
    if action == "group":
        group_id = _session(owner_id).groups.get(parts[3], parts[3])
        text, markup = _group_markup(owner_id, group_id, int(parts[4]))
        await query.answer()
        await _edit_menu(query.message, text, markup)
        return
    if action == "provider":
        provider = _session(owner_id).providers.get(parts[3])
        if not provider:
            await query.answer("Không tìm thấy provider.", show_alert=True)
            return
        text, markup = _provider_markup(owner_id, provider)
        await query.answer()
        await _edit_menu(query.message, text, markup)
        return
    if action == "add_provider":
        old = _PENDING_EDITS.pop(owner_id, None)
        if old and old.expires_task:
            old.expires_task.cancel()
        task = asyncio.create_task(_expire_edit(owner_id))
        _PENDING_EDITS[owner_id] = PendingEdit(owner_id, query.message, "add_provider", expires_task=task)
        await query.answer("Gửi tên provider mới.")
        await _edit_menu(query.message, "<b>➕ Add Agent Provider</b>\n\n<i>Gửi tên provider mới trong 60s.</i>", InlineKeyboardMarkup([_footer_row(owner_id, _cb("group", owner_id, _group_token(owner_id, "providers"), 0))]))
        return
    if action in {"view", "edit", "toggle"}:
        key_token, group_id, page = parts[3], parts[4], int(parts[5])
        full_key = _session(owner_id).keys.get(key_token)
        _, entries, _ = _parse_settings()
        entry = entries.get(full_key or "")
        if entry is None:
            await query.answer("Không tìm thấy biến.", show_alert=True)
            return
        if action == "toggle":
            if not isinstance(entry.value, bool):
                await query.answer("Biến này không phải boolean.", show_alert=True)
                return
            _write_entry(entry, not entry.value)
            _mark_dirty(owner_id, entry.full_key)
            text, markup = _group_markup(owner_id, group_id, page)
            await query.answer("Đã đổi, bấm 🔄 ở Config Variables để áp dụng.")
            await _edit_menu(query.message, text, markup)
            return
        if action == "view":
            await query.answer()
            await _edit_menu(query.message, _entry_text(entry), _entry_markup(owner_id, key_token, group_id, page))
            return
        old = _PENDING_EDITS.pop(owner_id, None)
        if old and old.expires_task:
            old.expires_task.cancel()
        task = asyncio.create_task(_expire_edit(owner_id))
        _PENDING_EDITS[owner_id] = PendingEdit(owner_id, query.message, "edit", group_id, page, entry, expires_task=task)
        await query.answer("Gửi giá trị mới.")
        await _edit_menu(query.message, "\n".join(["<b>✏️ Edit Config Variable</b>", "", f"<b>Key:</b> <code>{html.escape(entry.full_key)}</code>", f"<b>Current:</b> <code>{html.escape(_mask_value(entry.full_key, entry.value))}</code>", "<i>Gửi giá trị mới trong 60s.</i>"]), InlineKeyboardMarkup([_footer_row(owner_id, _cb("view", owner_id, key_token, group_id, page))]))


@pyrogram.Client.on_message(pyrogram.filters.private & pyrogram.filters.text, group=-1)
async def config_edit_message(client: pyrogram.Client, message: pyrogram.types.Message):
    user = message.from_user
    if user is None or message.text is None or message.text.startswith("/"):
        return
    pending = _PENDING_EDITS.pop(user.id, None)
    if pending is None:
        return
    if pending.expires_task:
        pending.expires_task.cancel()
    try:
        if pending.mode == "add_provider":
            provider = message.text.strip()
            _add_provider(provider)
            for field_name in _PROVIDER_DEFAULTS:
                _mark_dirty(user.id, f"agent_providers.{provider}.{field_name}")
            text, markup = _provider_markup(user.id, provider)
        else:
            if pending.entry is None or pending.group_id is None:
                raise ValueError("Missing pending edit state")
            value = _coerce_value(message.text, pending.entry.value)
            _write_entry(pending.entry, value)
            _mark_dirty(user.id, pending.entry.full_key)
            text, markup = _group_markup(user.id, pending.group_id, pending.page)
    except Exception as e:
        text = f"<b>❌ Update failed</b>\n\n<code>{html.escape(str(e))}</code>"
        markup = InlineKeyboardMarkup([_footer_row(user.id)])
    await _edit_menu(pending.message, text, markup)
    try:
        await message.delete()
    except Exception:
        pass
