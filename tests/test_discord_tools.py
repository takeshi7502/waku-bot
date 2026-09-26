import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from waku.discordbot import history, media, scheduling


def _job(user_id: int, summary: str):
    return SimpleNamespace(
        id=f"discord_schedule_msg:1:99:{user_id}:1700000000:abc",
        args=[99, [user_id], summary],
        next_run_time=datetime(2030, 1, 1, tzinfo=UTC),
    )


def _schedule_context(user_id: int = 42):
    message = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=user_id),
    )
    return SimpleNamespace(deps=SimpleNamespace(message=message))


def test_discord_schedule_parses_persisted_job_and_missing_mentions():
    parsed = scheduling._parse_discord_scheduled_job(_job(42, "hello"))
    assert parsed is not None
    assert parsed.user_id == 42
    assert parsed.summary == "hello"
    assert scheduling._discord_reminder_text("hi <@10>", ["10", "11", "invalid"]) == "<@11> hi <@10>"
    assert scheduling._discord_image_message_parts("hi <@10>", [10, 11]) == (
        "<@11> hi <@10>",
        "",
    )


@pytest.mark.asyncio
async def test_regular_member_cannot_list_or_cancel_other_schedules(monkeypatch):
    own_job = _job(42, "mine")
    other_job = _job(43, "another member")
    monkeypatch.setattr(scheduling.common.jobqueue, "get_all_jobs", lambda: [own_job, other_job])
    remove = Mock()
    monkeypatch.setattr(scheduling.common.jobqueue, "remove_job", remove)
    monkeypatch.setattr(scheduling, "_is_discord_bot_admin", lambda message: False)
    ctx = _schedule_context()

    listed = await scheduling.list_discord_scheduled_messages(ctx)
    assert "mine" in listed
    assert "another member" not in listed

    cancelled = await scheduling.cancel_discord_scheduled_message(ctx, cancel_all=True)
    assert cancelled.success
    remove.assert_called_once_with(own_job.id)


@pytest.mark.asyncio
async def test_unknown_size_attachment_uses_bounded_download(monkeypatch):
    monkeypatch.setattr(media, "_discord_media_enabled", lambda: True)
    download = AsyncMock(return_value=(b"image bytes", "image/png"))
    monkeypatch.setattr(media, "_download_discord_media", download)
    attachment = SimpleNamespace(
        filename="photo.png",
        content_type="image/png",
        size=0,
        url="https://example.test/photo.png",
        read=AsyncMock(),
    )
    message = SimpleNamespace(attachments=[attachment])

    summaries, contents = await media._discord_attachment_contents(message)

    assert len(summaries) == 1
    assert len(contents) == 1
    assert contents[0].data == b"image bytes"
    attachment.read.assert_not_awaited()
    download.assert_awaited_once_with(attachment.url)


@pytest.mark.asyncio
async def test_scheduled_anime_waits_for_image_lock_and_uses_shared_spoiler_sender(monkeypatch):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 99
    channel.guild = SimpleNamespace(id=1)
    channel.send = AsyncMock()
    monkeypatch.setattr(
        scheduling.state,
        "discord_client",
        SimpleNamespace(get_channel=lambda channel_id: channel),
    )
    monkeypatch.setattr(scheduling, "manyacg_client", object())
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(scheduling, "_discord_image_lock", lambda: lock)
    artwork = SimpleNamespace(title="test", r18=True)
    picture = SimpleNamespace(regular="https://example.test/image.jpg")
    fetch = AsyncMock(return_value=(artwork, picture))
    send_card = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduling, "_fetch_discord_anime_artwork", fetch)
    monkeypatch.setattr(scheduling, "_send_discord_anime_photo_card", send_card)
    monkeypatch.setattr(scheduling, "_discord_r18_mode", AsyncMock(return_value=2))

    task = asyncio.create_task(
        scheduling._scheduled_discord_image_job(
            99, "anime", caption="hello <@10>", target_user_ids=[10, 11]
        )
    )
    await asyncio.sleep(0)
    fetch.assert_not_awaited()
    lock.release()
    await asyncio.wait_for(task, timeout=1)

    fetch.assert_awaited_once()
    send_card.assert_awaited_once()
    kwargs = send_card.await_args.kwargs
    assert kwargs["content"] == "<@11> hello <@10>"
    assert send_card.await_args.args[1] is artwork


@pytest.mark.asyncio
async def test_shared_anime_sender_marks_r18_image_as_spoiler(monkeypatch):
    send_image = AsyncMock(return_value=True)
    monkeypatch.setattr(media, "_send_discord_image_embed", send_image)
    message = SimpleNamespace(
        guild=SimpleNamespace(name="server"),
        channel=SimpleNamespace(id=99, name="channel"),
    )
    artwork = SimpleNamespace(title="art", source_url="https://example.test/art", r18=True)
    picture = SimpleNamespace(id=1, regular="https://example.test/image.jpg")

    assert await media._send_discord_anime_photo_card(message, artwork, picture)
    assert send_image.await_args.kwargs["spoiler"] is True


@pytest.mark.asyncio
async def test_group_memory_keeps_batch_after_failed_update(monkeypatch):
    monkeypatch.setattr(history, "_DISCORD_GROUP_MEMORY_BATCH_SIZE", 1)
    monkeypatch.setattr(history.app_config, "agent_group_memory", True)
    monkeypatch.setattr(
        history,
        "_discord_guild_settings",
        AsyncMock(return_value=SimpleNamespace(enabled=True, ai_reply=True, group_memory_enabled=True)),
    )
    add = AsyncMock(side_effect=[RuntimeError("temporary"), "stored"])
    monkeypatch.setattr(history, "_get_powermemory", lambda: SimpleNamespace(add=add))
    cache = {}

    async def get(key, default=None):
        return cache.get(key, default)

    async def set_value(key, value, ttl=None):
        cache[key] = value

    monkeypatch.setattr(history.common.memttlcache, "get", get)
    monkeypatch.setattr(history.common.memttlcache, "set", set_value)
    message = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=42, bot=False, display_name="user", name="user"),
        channel=SimpleNamespace(id=99),
        id=123,
        content="hello",
        clean_content="hello",
        created_at=datetime.now(UTC),
    )

    await history._record_discord_group_memory(message)
    messages_key = history._discord_group_messages_key(1)
    assert len(cache[messages_key]) == 1
    cache.pop(f"{history._discord_group_memory_update_key(1)}:retry")
    await history._record_discord_group_memory(message)
    assert cache[messages_key] == []
    assert add.await_count == 2
