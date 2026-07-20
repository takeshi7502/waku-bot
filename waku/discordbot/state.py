from __future__ import annotations

import asyncio
from typing import Any

import discord

from .constants import DISCORD_AGENT_BUSY_TIMEOUT, DISCORD_AGENT_MAX_CONCURRENT

discord_client: discord.Client | None = None
discord_task: asyncio.Task | None = None
discord_agent: Any = None
discord_recovery_agent: Any = None
warned_empty_content = False
server_list_view_registered = False
discord_image_send_lock: asyncio.Lock | None = None
discord_agent_semaphore: asyncio.Semaphore | None = None
discord_agent_semaphore_limit = 0
discord_auth_locks: dict[int, asyncio.Lock] = {}


def _discord_image_lock() -> asyncio.Lock:
    global discord_image_send_lock
    if discord_image_send_lock is None:
        discord_image_send_lock = asyncio.Lock()
    return discord_image_send_lock


def _discord_agent_limit() -> int:
    return DISCORD_AGENT_MAX_CONCURRENT


def _discord_agent_busy_timeout() -> float:
    return DISCORD_AGENT_BUSY_TIMEOUT


def _discord_auth_lock(guild_id: int) -> asyncio.Lock:
    lock = discord_auth_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        discord_auth_locks[guild_id] = lock
    return lock


def _discord_agent_gate() -> asyncio.Semaphore:
    global discord_agent_semaphore, discord_agent_semaphore_limit
    limit = _discord_agent_limit()
    if discord_agent_semaphore is None or discord_agent_semaphore_limit != limit:
        discord_agent_semaphore = asyncio.Semaphore(limit)
        discord_agent_semaphore_limit = limit
    return discord_agent_semaphore


def runtime_status() -> str:
    if discord_client is not None and discord_client.is_ready():
        return "running"
    if discord_task is not None and not discord_task.done():
        return "starting"
    if discord_task is not None and discord_task.done() and not discord_task.cancelled():
        try:
            if discord_task.exception() is not None:
                return "error"
        except Exception:
            return "error"
    return "stopped"
