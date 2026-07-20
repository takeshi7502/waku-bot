from __future__ import annotations

from datetime import UTC, datetime

import discord

from waku.config import app_config
from waku.logger import logger

from .. import state
from ..constants import (
    DISCORD_AUTH_APPROVE_BUTTON_ID,
    DISCORD_AUTH_REJECT_BUTTON_ID,
    DISCORD_AUTH_REQUEST_BUTTON_ID,
    DISCORD_AUTH_STATUS_PENDING,
    DISCORD_AUTH_STATUS_REJECTED,
)
from ..permissions import _is_discord_server_admin, _is_discord_user_bot_admin
from ..settings import (
    _approve_discord_auth_request,
    _discord_auth_config,
    _reject_discord_auth_request,
    _reset_discord_auth_request,
    _set_discord_auth_review_messages,
    _submit_discord_auth_request,
)


def _guild_id_from_review(interaction: discord.Interaction) -> int | None:
    message = interaction.message
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer.text or ""
    marker = "Guild ID: "
    if marker not in footer:
        return None
    value = footer.split(marker, 1)[1].split()[0]
    return int(value) if value.isdigit() else None


def _request_embed(guild: discord.Guild, config) -> discord.Embed:
    status = config.discord_auth_status
    if status == DISCORD_AUTH_STATUS_PENDING:
        description = (
            "Yêu cầu cấp quyền của server đang chờ quản trị viên Waku duyệt.\n"
            "Waku sẽ thông báo tại đây sau khi có kết quả."
        )
        color = discord.Color.orange()
    elif status == DISCORD_AUTH_STATUS_REJECTED:
        reason = config.discord_auth_rejection_reason or "Không có lý do cụ thể."
        description = (
            "Yêu cầu cấp quyền trước đó đã bị từ chối.\n"
            f"**Lý do:** {reason}\n\nBạn có thể gửi lại một yêu cầu mới."
        )
        color = discord.Color.red()
    else:
        description = "Server của bạn chưa được cấp quyền để sử dụng Waku!"
        color = discord.Color.blurple()
    embed = discord.Embed(title="Waku Server Authorization", description=description, color=color)
    embed.set_footer(text=f"Server: {guild.name} • ID: {guild.id}")
    return embed


def _review_embed(guild: discord.Guild, requester: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(
        title="Yêu cầu cấp quyền Waku",
        description=(
            f"Server **{guild.name}** đang xin quyền sử dụng Waku.\n\n"
            f"**Người yêu cầu:** {requester} (`{requester.id}`)\n"
            f"**Thành viên:** {guild.member_count or 'Không rõ'}"
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(UTC),
    )
    embed.set_footer(text=f"Guild ID: {guild.id}")
    return embed


async def _notify_request_result(guild_id: int, config, approved: bool, reason: str | None = None) -> None:
    requester_id = config.discord_auth_requester_id
    channel_id = config.discord_auth_channel_id
    if requester_id is None:
        return
    guild = state.discord_client.get_guild(guild_id) if state.discord_client else None
    guild_name = guild.name if guild is not None else str(guild_id)
    description = (
        f"<@{requester_id}> yêu cầu cấp quyền cho **{guild_name}** đã được **chấp nhận**!\n\n"
        "💡 Bây giờ bạn có thể dùng `!config` để cấu hình Waku cho server."
        if approved
        else (
            f"<@{requester_id}> yêu cầu cấp quyền cho **{guild_name}** đã bị **từ chối**.\n\n"
            f"**Lý do:** {reason or 'Không có lý do cụ thể.'}"
        )
    )
    embed = discord.Embed(
        title="Waku Server Authorization",
        description=description,
        color=discord.Color.green() if approved else discord.Color.red(),
        timestamp=datetime.now(UTC),
    )
    embed.set_footer(text=f"Server: {guild_name} • ID: {guild_id}")
    if state.discord_client is not None and channel_id is not None:
        channel = state.discord_client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await state.discord_client.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel is not None and hasattr(channel, "send"):
            try:
                await channel.send(
                    content=f"<@{requester_id}>",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return
            except Exception as exc:
                logger.warning(f"Discord auth result channel notification failed: {exc}")
    if state.discord_client is not None:
        try:
            user = state.discord_client.get_user(requester_id) or await state.discord_client.fetch_user(requester_id)
            await user.send(embed=embed)
        except Exception as exc:
            logger.warning(f"Discord auth result DM notification failed: {exc}")


async def _update_review_messages(
    guild_id: int,
    config,
    approved: bool,
    reviewer: discord.abc.User,
    reason: str | None = None,
) -> None:
    if state.discord_client is None:
        return
    guild = state.discord_client.get_guild(guild_id)
    guild_name = guild.name if guild is not None else str(guild_id)
    description = (
        f"**Server:** {guild_name}\n✅ Đã được **chấp nhận** bởi {reviewer}."
        if approved
        else f"**Server:** {guild_name}\n❌ Đã bị **từ chối** bởi {reviewer}.\n**Lý do:** {reason}"
    )
    for ref in config.discord_auth_review_messages or []:
        try:
            channel = state.discord_client.get_channel(int(ref["channel_id"]))
            if channel is None:
                channel = await state.discord_client.fetch_channel(int(ref["channel_id"]))
            message = await channel.fetch_message(int(ref["message_id"]))
            embed = message.embeds[0] if message.embeds else discord.Embed(title="Yêu cầu cấp quyền Waku")
            embed.description = description
            embed.color = discord.Color.green() if approved else discord.Color.red()
            await message.edit(embed=embed, view=None)
        except Exception as exc:
            logger.debug(f"Could not update Discord auth review message: {exc}")


class DiscordAuthorizationRequestView(discord.ui.View):
    def __init__(self, *, pending: bool = False) -> None:
        super().__init__(timeout=None)
        if pending:
            self.request_access.disabled = True
            self.request_access.label = "Đang chờ duyệt"

    @discord.ui.button(label="Xin quyền", style=discord.ButtonStyle.primary, custom_id=DISCORD_AUTH_REQUEST_BUTTON_ID)
    async def request_access(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Chỉ có thể xin quyền từ trong server.", ephemeral=True)
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if guild.owner_id != interaction.user.id and not (permissions and permissions.administrator):
            await interaction.response.send_message("Chỉ quản trị viên server mới có thể xin quyền.", ephemeral=True)
            return
        await interaction.response.defer()
        config, created = await _submit_discord_auth_request(
            guild.id, guild.name, interaction.user.id, interaction.channel_id
        )
        if config.discord_enabled:
            await interaction.edit_original_response(content="Server đã được cấp quyền.", embed=None, view=None)
            return
        if not created:
            await interaction.edit_original_response(embed=_request_embed(guild, config), view=DiscordAuthorizationRequestView(pending=True))
            return
        review_refs: list[dict] = []
        admin_ids = set(app_config.discord_admin_users) | set(app_config.owners)
        for admin_id in admin_ids:
            try:
                if state.discord_client is None:
                    break
                user = state.discord_client.get_user(admin_id) or await state.discord_client.fetch_user(admin_id)
                sent = await user.send(embed=_review_embed(guild, interaction.user), view=DiscordAuthorizationReviewView())
                review_refs.append({"channel_id": sent.channel.id, "message_id": sent.id})
            except Exception as exc:
                logger.warning(f"Failed to deliver Discord auth request to admin {admin_id}: {exc}")
        if not review_refs:
            await _reset_discord_auth_request(guild.id)
            await interaction.edit_original_response(
                content="Không thể gửi yêu cầu đến quản trị viên Waku lúc này. Vui lòng thử lại sau.", embed=None, view=None
            )
            return
        await _set_discord_auth_review_messages(guild.id, review_refs)
        config.discord_auth_review_messages = review_refs
        await interaction.edit_original_response(embed=_request_embed(guild, config), view=DiscordAuthorizationRequestView(pending=True))
        logger.info(f"Discord auth requested: guild={guild.id} requester={interaction.user.id} reviews={len(review_refs)}")


class DiscordAuthorizationRejectModal(discord.ui.Modal, title="Từ chối yêu cầu cấp quyền"):
    reason = discord.ui.TextInput(label="Lý do", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    def __init__(self, guild_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_discord_user_bot_admin(interaction.user):
            await interaction.response.send_message("Bạn không có quyền duyệt yêu cầu này.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        reason = str(self.reason).strip()
        config = await _reject_discord_auth_request(self.guild_id, reason)
        if config is None:
            await interaction.followup.send("Yêu cầu đã được xử lý hoặc không còn chờ duyệt.", ephemeral=True)
            return
        await _notify_request_result(self.guild_id, config, False, reason)
        await _update_review_messages(self.guild_id, config, False, interaction.user, reason)
        guild = state.discord_client.get_guild(self.guild_id) if state.discord_client else None
        guild_name = guild.name if guild is not None else str(self.guild_id)
        await interaction.followup.send(
            f"Đã từ chối yêu cầu của server **{guild_name}** và thông báo kết quả.",
            ephemeral=True,
        )
        logger.info(f"Discord auth rejected: guild={self.guild_id} reviewer={interaction.user.id} reason={reason!r}")


class DiscordAuthorizationReviewView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Chấp nhận", emoji="✅", style=discord.ButtonStyle.success, custom_id=DISCORD_AUTH_APPROVE_BUTTON_ID)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _is_discord_user_bot_admin(interaction.user):
            await interaction.response.send_message("Bạn không có quyền duyệt yêu cầu này.", ephemeral=True)
            return
        guild_id = _guild_id_from_review(interaction)
        if guild_id is None:
            await interaction.response.send_message("Không xác định được server của yêu cầu.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        config = await _approve_discord_auth_request(guild_id)
        if config is None:
            await interaction.followup.send("Yêu cầu đã được xử lý hoặc không còn chờ duyệt.", ephemeral=True)
            return
        await _notify_request_result(guild_id, config, True)
        await _update_review_messages(guild_id, config, True, interaction.user)
        guild = state.discord_client.get_guild(guild_id) if state.discord_client else None
        guild_name = guild.name if guild is not None else str(guild_id)
        await interaction.followup.send(
            f"Đã cấp quyền cho server **{guild_name}** và thông báo kết quả.",
            ephemeral=True,
        )
        logger.info(f"Discord auth approved: guild={guild_id} reviewer={interaction.user.id}")

    @discord.ui.button(label="Từ chối", emoji="❌", style=discord.ButtonStyle.danger, custom_id=DISCORD_AUTH_REJECT_BUTTON_ID)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _is_discord_user_bot_admin(interaction.user):
            await interaction.response.send_message("Bạn không có quyền duyệt yêu cầu này.", ephemeral=True)
            return
        guild_id = _guild_id_from_review(interaction)
        if guild_id is None:
            await interaction.response.send_message("Không xác định được server của yêu cầu.", ephemeral=True)
            return
        await interaction.response.send_modal(DiscordAuthorizationRejectModal(guild_id))


async def _send_discord_authorization_panel(message: discord.Message) -> None:
    guild = message.guild
    if guild is None:
        return
    config = await _discord_auth_config(guild.id, guild.name)
    pending = config.discord_auth_status == DISCORD_AUTH_STATUS_PENDING
    await message.channel.send(
        embed=_request_embed(guild, config),
        view=DiscordAuthorizationRequestView(pending=pending),
        reference=message,
        mention_author=False,
    )
