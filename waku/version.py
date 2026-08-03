"""Runtime version and deployment metadata helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from waku.config import app_config


def _env(name: str, default: str = "unknown") -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _short_commit(commit: str) -> str:
    if commit == "unknown":
        return commit
    return commit[:12]


def _discord_status() -> str:
    if not app_config.discord_token:
        return "disabled"
    try:
        from waku.discordbot import get_discord_runtime_status

        return get_discord_runtime_status()
    except Exception:
        return "unknown"


def runtime_info() -> dict[str, Any]:
    """Return version/config details useful for deployment verification."""
    commit = _env("WAKU_COMMIT")
    return {
        "version": _env("WAKU_VERSION"),
        "commit": commit,
        "commit_short": _short_commit(commit),
        "build_time": _env("WAKU_BUILD_TIME"),
        "settings_path": str(Path("settings.toml").resolve()),
        "agent_model": app_config.agent_model,
        "agent_model_multimodal": app_config.agent_model_multimodal,
        "sticker_memory": app_config.agent_sticker_memory,
        "sticker_embed_model": app_config.agent_sticker_embed_model,
        "sticker_embed_dimensions": app_config.agent_sticker_embed_dimensions,
        "health_check_port": app_config.health_check_port,
        "discord_token_configured": bool(app_config.discord_token),
        "discord_status": _discord_status(),
    }


def runtime_info_text() -> str:
    """Return deployment info formatted for logs."""
    info = runtime_info()
    lines = [
        "Waku runtime version",
        f"version: {info['version']}",
        f"commit: {info['commit_short']}",
        f"build_time: {info['build_time']}",
        f"settings_path: {info['settings_path']}",
        f"agent_model: {info['agent_model']}",
        f"agent_model_multimodal: {info['agent_model_multimodal']}",
        f"sticker_memory: {info['sticker_memory']}",
        f"sticker_embed_model: {info['sticker_embed_model']}",
        f"sticker_embed_dimensions: {info['sticker_embed_dimensions']}",
        f"discord_status: {info['discord_status']}",
    ]
    return "\n".join(lines)


def runtime_info_telegram_text() -> str:
    """Return compact HTML-formatted runtime info for Telegram."""
    info = runtime_info()
    sticker_embed_model = info["sticker_embed_model"] or "fallback bằng agent_model"
    return "\n".join(
        [
            "<b>🚀 Waku Runtime</b>",
            "",
            f"<b>Version:</b> <code>{info['version']}</code>",
            f"<b>Commit:</b> <code>{info['commit_short']}</code>",
            f"<b>Build:</b> <code>{info['build_time']}</code>",
            "",
            "<b>🤖 Models</b>",
            f"• Chat: <code>{info['agent_model']}</code>",
            f"• Vision: <code>{info['agent_model_multimodal']}</code>",
            "",
            "<b>🧠 Sticker Memory</b>",
            f"• Enabled: <code>{info['sticker_memory']}</code>",
            f"• Embed: <code>{sticker_embed_model}</code>",
            f"• Dims: <code>{info['sticker_embed_dimensions']}</code>",
            "",
            "<b>🛰️ Discord</b>",
            f"• Token: <code>{info['discord_token_configured']}</code>",
            f"• Status: <code>{info['discord_status']}</code>",
            "",
            f"<b>⚙️ Settings:</b> <code>{info['settings_path']}</code>",
        ]
    )
