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


def runtime_info() -> dict[str, Any]:
    """Return version/config details useful for deployment verification."""
    return {
        "version": _env("WAKU_VERSION"),
        "commit": _env("WAKU_COMMIT"),
        "build_time": _env("WAKU_BUILD_TIME"),
        "settings_path": str(Path("settings.toml").resolve()),
        "agent_model": app_config.agent_model,
        "agent_model_multimodal": app_config.agent_model_multimodal,
        "sticker_memory": app_config.agent_sticker_memory,
        "sticker_embed_model": app_config.agent_sticker_embed_model,
        "sticker_embed_dimensions": app_config.agent_sticker_embed_dimensions,
        "health_check_port": app_config.health_check_port,
    }


def runtime_info_text() -> str:
    """Return deployment info formatted for logs or Telegram replies."""
    info = runtime_info()
    lines = [
        "Waku runtime version",
        f"version: {info['version']}",
        f"commit: {info['commit']}",
        f"build_time: {info['build_time']}",
        f"settings_path: {info['settings_path']}",
        f"agent_model: {info['agent_model']}",
        f"agent_model_multimodal: {info['agent_model_multimodal']}",
        f"sticker_memory: {info['sticker_memory']}",
        f"sticker_embed_model: {info['sticker_embed_model']}",
        f"sticker_embed_dimensions: {info['sticker_embed_dimensions']}",
    ]
    return "\n".join(lines)
