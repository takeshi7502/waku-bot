from __future__ import annotations

from datetime import UTC, datetime

import discord

from waku.logger import logger

from . import state
from .embeds import discord_command_embed
from .permissions import _is_discord_user_bot_admin


async def _authorized_discord_guild_ids() -> set[int]:
    """Return guild IDs that have explicitly enabled Waku's Discord features."""
    from sqlalchemy import select

    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        rows = await session.execute(select(ChatData.id, ChatData.config))
    return {
        guild_id
        for guild_id, config in rows.all()
        if config and config.get("discord_enabled", False)
    }


async def _respond_to_broadcast_interaction(
    interaction: discord.Interaction,
    description: str,
    *,
    title: str = "Phát thông báo",
    color: discord.Color | None = None,
) -> None:
    embed = discord_command_embed(description, title=title, color=color)
    if interaction.response.is_done():
        await interaction.edit_original_response(content=None, embed=embed)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _broadcast_bot_member(guild: discord.Guild) -> discord.Member | None:
    if guild.me is not None:
        return guild.me
    client = state.discord_client
    if client is not None and client.user is not None:
        return guild.get_member(client.user.id)
    return None


def _can_send_broadcast(channel: object, member: discord.Member | None) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if member is None or not callable(permissions_for):
        return False
    permissions = permissions_for(member)
    return bool(permissions.view_channel and permissions.send_messages)


async def _broadcast_channel(
    guild: discord.Guild,
    preferred_channel: object | None = None,
) -> tuple[discord.abc.Messageable | None, bool]:
    """Choose a writable channel, preferring the invoking channel when available."""
    member = _broadcast_bot_member(guild)
    if (
        preferred_channel is not None
        and isinstance(preferred_channel, discord.abc.Messageable)
        and _can_send_broadcast(preferred_channel, member)
    ):
        return preferred_channel, False

    candidates: list[object] = []
    if guild.system_channel is not None:
        candidates.append(guild.system_channel)
    candidates.extend(guild.text_channels)
    if not guild.text_channels:
        try:
            candidates.extend(
                channel
                for channel in await guild.fetch_channels()
                if isinstance(channel, discord.TextChannel)
            )
        except discord.HTTPException as e:
            logger.debug(f"Discord broadcast channel fetch failed for guild={guild.id}: {e}")

    seen: set[int] = set()
    for candidate in candidates:
        channel_id = getattr(candidate, "id", None)
        if channel_id is None or channel_id in seen:
            continue
        seen.add(channel_id)
        if isinstance(candidate, discord.abc.Messageable) and _can_send_broadcast(candidate, member):
            return candidate, True
    return None, True


async def send_discord_broadcast(
    interaction: discord.Interaction,
    message: str,
    target: str | None = None,
) -> None:
    """Send an owner-authored announcement to one or more Discord servers.

    `target` accepts `here` (or omitted) for the current server, `all` for every
    joined server, `auth`/`unauth` to filter by authorization state, or a guild ID.
    """
    if not _is_discord_user_bot_admin(interaction.user):
        await _respond_to_broadcast_interaction(
            interaction,
            "Chỉ chủ bot mới có thể dùng `/bc`.",
            title="Không đủ quyền",
            color=discord.Color.orange(),
        )
        return

    text = message.replace("\\n", "\n").strip()
    if not text:
        await _respond_to_broadcast_interaction(
            interaction,
            "Nội dung thông báo không được để trống.",
            title="Nội dung không hợp lệ",
            color=discord.Color.orange(),
        )
        return
    if len(text) > 4000:
        await _respond_to_broadcast_interaction(
            interaction,
            "Nội dung tối đa là 4.000 ký tự để hiển thị trọn vẹn trong embed.",
            title="Nội dung quá dài",
            color=discord.Color.orange(),
        )
        return

    client = state.discord_client
    if client is None:
        await _respond_to_broadcast_interaction(
            interaction,
            "Waku chưa sẵn sàng để phát thông báo. Vui lòng thử lại sau.",
            title="Không thể phát thông báo",
            color=discord.Color.orange(),
        )
        return

    normalized_target = (target or "here").strip().casefold()
    if normalized_target in {"", ".", "here"}:
        if interaction.guild is None:
            await _respond_to_broadcast_interaction(
                interaction,
                "Khi dùng trong DM, hãy đặt `target` là `all` hoặc ID server.",
                title="Thiếu server đích",
                color=discord.Color.orange(),
            )
            return
        target_mode = "here"
        requested_guilds = [interaction.guild]
    elif normalized_target in {"all", "auth", "unauth"}:
        target_mode = "all"
        requested_guilds = list(client.guilds)
    elif normalized_target.isdigit():
        guild = client.get_guild(int(normalized_target))
        if guild is None:
            await _respond_to_broadcast_interaction(
                interaction,
                f"Không tìm thấy server có ID `{target}` mà Waku đang tham gia.",
                title="Không tìm thấy server",
                color=discord.Color.orange(),
            )
            return
        target_mode = "guild"
        requested_guilds = [guild]
    else:
        await _respond_to_broadcast_interaction(
            interaction,
            "`target` chỉ nhận `here`, `all`, `auth`, `unauth`, hoặc ID server.",
            title="Server đích không hợp lệ",
            color=discord.Color.orange(),
        )
        return

    if normalized_target in {"auth", "unauth"}:
        try:
            authorized_ids = await _authorized_discord_guild_ids()
        except Exception as e:
            logger.error(f"Failed to load Discord broadcast targets: {e.__class__.__name__}: {e}")
            await _respond_to_broadcast_interaction(
                interaction,
                "Không thể tải danh sách server đã được cấp quyền. Vui lòng thử lại sau.",
                title="Không thể phát thông báo",
                color=discord.Color.orange(),
            )
            return
        if normalized_target == "auth":
            guilds = [guild for guild in requested_guilds if guild.id in authorized_ids]
        else:
            guilds = [guild for guild in requested_guilds if guild.id not in authorized_ids]
    else:
        # `here`, a guild ID, and `all` are explicit destinations.  They must
        # keep working even if the selected server has not requested Waku auth.
        guilds = requested_guilds
    if not guilds:
        await _respond_to_broadcast_interaction(
            interaction,
            "Không có server nào khớp với target đã chọn.",
            title="Không có server đích",
            color=discord.Color.orange(),
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    announcement = discord.Embed(
        title="📢 Thông báo từ Waku",
        description=text,
        color=discord.Color.blurple(),
        timestamp=datetime.now(UTC),
    )
    direct_count = 0
    fallback_count = 0
    failed_count = 0
    preferred_channel = interaction.channel if target_mode == "here" else None

    for guild in guilds:
        channel, used_fallback = await _broadcast_channel(guild, preferred_channel)
        if channel is None:
            failed_count += 1
            logger.warning(f"Discord broadcast skipped guild={guild.id}: no writable channel")
            continue
        try:
            await channel.send(embed=announcement, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as e:
            failed_count += 1
            logger.warning(
                f"Discord broadcast send failed guild={guild.id} "
                f"channel={getattr(channel, 'id', None)} error={e}"
            )
            continue
        if used_fallback:
            fallback_count += 1
        else:
            direct_count += 1

    total_sent = direct_count + fallback_count
    result = discord_command_embed(
        "Phát sóng hoàn tất." if total_sent else "Không gửi được thông báo đến server nào.",
        title="Kết quả phát thông báo",
        color=discord.Color.green() if total_sent else discord.Color.red(),
    )
    result.add_field(name="Đã gửi", value=f"`{total_sent}` server", inline=True)
    result.add_field(name="Kênh fallback", value=f"`{fallback_count}` server", inline=True)
    result.add_field(name="Thất bại", value=f"`{failed_count}` server", inline=True)
    result.set_footer(
        text=(
            "Đích: toàn bộ server"
            if normalized_target == "all"
            else "Đích: server đã cấp quyền"
            if normalized_target == "auth"
            else "Đích: server chưa cấp quyền"
            if normalized_target == "unauth"
            else "Đích: server đã chọn"
        )
    )
    await interaction.edit_original_response(content=None, embed=result)
    logger.info(
        "Discord broadcast completed: "
        f"user={interaction.user.id} target={normalized_target!r} sent={total_sent} "
        f"fallback={fallback_count} failed={failed_count}"
    )
