import asyncio
import io
import random
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import discord
import httpx
from ddgs import DDGS
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.messages import ModelMessage

from waku import common
from waku.config import app_config
from waku.logger import logger
from waku.plugins.agent import provider
from waku.plugins.agent.history import filter_empty_model_responses
from waku.services import manyacg as manyacg_service
from waku.services.manyacg import manyacg_client

_discord_client: discord.Client | None = None
_discord_task: asyncio.Task | None = None
_discord_agent: Agent["DiscordContextDeps", str] | None = None
_warned_empty_content = False


_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<@!?\d+>")
_KEYWORD_SEPARATORS = (" ", "\n", "\t", ",", ":", "，", "：", "!", "！", "?", "？")
_SETU_COMMANDS = ("setu", "/setu", "!setu", ".setu", "涩图", "色图")
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "]"
)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,25}>")
_R18_KEYWORDS = (
    "r18",
    "nsfw",
    "hentai",
    "ero",
    "ecchi",
    "nude",
    "naked",
    "porn",
    "sex",
    "lewd",
    "18+",
    "🔞",
)


@dataclass
class DiscordGuildSettings:
    enabled: bool = False
    muted: bool = False
    allow_r18: bool = False


@dataclass
class DiscordContextDeps:
    message: discord.Message


@dataclass
class DiscordArtistInfo:
    name: str
    type: str | None = None
    username: str | None = None
    uid: str | None = None


@dataclass
class DiscordAnimePhotoInfo:
    title: str
    source_url: str
    r18: bool
    description: str
    artist: DiscordArtistInfo | None
    tags: list[str]


@dataclass
class DiscordAnimePhotoResult:
    success: bool = True
    message: str | None = None
    data: DiscordAnimePhotoInfo | None = None


@dataclass
class DiscordChannelInfo:
    id: int
    name: str
    type: str
    mention: str | None = None
    category: str | None = None


@dataclass
class DiscordServerInfo:
    id: int
    name: str
    member_count: int | None
    owner_id: int | None
    owner_mention: str | None
    current_channel: DiscordChannelInfo
    channels: list[DiscordChannelInfo]


@dataclass
class DiscordServerInfoResult:
    success: bool = True
    message: str | None = None
    data: DiscordServerInfo | None = None


@dataclass
class DiscordUserInfo:
    id: int
    name: str
    display_name: str
    mention: str
    bot: bool
    global_name: str | None = None
    nick: str | None = None
    matched_by: str | None = None
    roles: list[str] | None = None


@dataclass
class DiscordUserSearchResult:
    success: bool = True
    message: str | None = None
    users: list[DiscordUserInfo] | None = None


@dataclass
class DiscordMentionResult:
    success: bool = True
    message: str | None = None
    mention: str | None = None
    user: DiscordUserInfo | None = None


@dataclass
class DiscordMessageSearchEntry:
    message_id: int
    channel_id: int
    channel_name: str
    author_id: int
    author_name: str
    author_mention: str
    created_at: str
    content: str
    jump_url: str


@dataclass
class DiscordMessageSearchResult:
    success: bool = True
    message: str | None = None
    searched: int = 0
    matches: list[DiscordMessageSearchEntry] | None = None


@dataclass
class DiscordWebImageResult:
    success: bool = True
    message: str | None = None
    title: str | None = None
    source_url: str | None = None


def _history_key(channel_id: int, user_id: int) -> str:
    return f"discord_message_history:{channel_id}:{user_id}"


def _waiting_key(user_id: int) -> str:
    return f"discord_agent_waiting:{user_id}"


async def _discord_guild_settings(guild: discord.Guild | None) -> DiscordGuildSettings:
    if guild is None:
        return DiscordGuildSettings(enabled=True)
    try:
        from waku.database.db import AsyncSessionFactory
        from waku.database.models import ChatData

        async with AsyncSessionFactory() as session:
            chat = await session.get(ChatData, guild.id)
            if chat is None:
                chat = ChatData(id=guild.id, title=guild.name, username=None)
                session.add(chat)
                await session.commit()
            config = chat.chat_config
        return DiscordGuildSettings(
            enabled=config.discord_enabled,
            muted=config.discord_muted,
            allow_r18=config.discord_allow_r18,
        )
    except Exception as e:
        logger.error(f"Failed to load Discord guild settings from DB: {e}")
        return DiscordGuildSettings(enabled=False)


async def _set_discord_guild_settings(
    guild: discord.Guild, settings: DiscordGuildSettings
) -> None:
    from waku.database.db import AsyncSessionFactory
    from waku.database.models import ChatData

    async with AsyncSessionFactory() as session:
        chat = await session.get(ChatData, guild.id)
        if chat is None:
            chat = ChatData(id=guild.id, title=guild.name, username=None)
            session.add(chat)
            await session.flush()
        config = chat.chat_config
        config.discord_enabled = settings.enabled
        config.discord_muted = settings.muted
        config.discord_allow_r18 = settings.allow_r18
        chat.chat_config = config
        await session.commit()


async def _discord_r18_allowed(guild: discord.Guild | None) -> bool:
    return (await _discord_guild_settings(guild)).allow_r18


def _contains_r18_keyword(text: str) -> bool:
    lowered = text.casefold()
    return any(keyword in lowered for keyword in _R18_KEYWORDS)


def _emoji_key(message: discord.Message) -> str:
    guild_part = message.guild.id if message.guild else "dm"
    return f"discord_emoji_style:{guild_part}:{message.author.id}"


async def _remember_discord_emojis(message: discord.Message) -> None:
    content = _message_text(message)
    emojis = _CUSTOM_EMOJI_RE.findall(content) + _EMOJI_RE.findall(content)
    if not emojis:
        return
    key = _emoji_key(message)
    existing: list[str] = await common.memttlcache.get(key, [])
    merged = (existing + emojis)[-40:]
    await common.memttlcache.set(key, merged, ttl=7 * 24 * 60 * 60)


async def _discord_emoji_hint(message: discord.Message) -> str | None:
    emojis: list[str] = await common.memttlcache.get(_emoji_key(message), [])
    if not emojis:
        return None
    common_emojis = [emoji for emoji, _ in Counter(emojis).most_common(8)]
    if not common_emojis:
        return None
    return "User/server emoji style: " + " ".join(common_emojis)


def _is_discord_admin(message: discord.Message) -> bool:
    if message.author.id in set(app_config.owners) | set(app_config.discord_admin_users):
        return True
    guild = message.guild
    if guild is not None and guild.owner_id == message.author.id:
        return True
    permissions = getattr(message.author, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


async def _send_admin_notice(message: discord.Message, text: str) -> None:
    embed = discord.Embed(
        description=text,
        color=discord.Color.blurple(),
        timestamp=datetime.now(),
    )
    await message.channel.send(embed=embed, reference=message, delete_after=10)
    try:
        await message.delete()
    except Exception:
        pass


def _normalize_keyword(keyword: str) -> str:
    return keyword.strip().lower()


def _message_text(message: discord.Message) -> str:
    return (message.content or message.clean_content or "").strip()


def _keyword_regex(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    return re.compile(
        rf"(^|[\s,，:：!！?？])({escaped})($|[\s,，:：!！?？])",
        re.IGNORECASE,
    )


def _discord_wake_keywords() -> list[str]:
    keywords = app_config.discord_keywords or app_config.bot_keywords
    return [keyword for keyword in keywords if keyword.strip()]


def _strip_bot_mention(content: str, bot_user_id: int) -> str:
    mention_forms = (f"<@{bot_user_id}>", f"<@!{bot_user_id}>")
    text = content
    for mention in mention_forms:
        text = text.replace(mention, "")
    return text.strip()


def _strip_keyword(content: str) -> str:
    text = content.strip()
    lowered = text.lower()
    for keyword in _discord_wake_keywords():
        normalized = _normalize_keyword(keyword)
        if not normalized:
            continue
        if lowered == normalized:
            return ""
        for sep in _KEYWORD_SEPARATORS:
            prefix = normalized + sep
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip()
        match = _keyword_regex(normalized).search(text)
        if match:
            start, end = match.span(2)
            return (text[:start] + text[end:]).strip(" \n\t,，:：!！?？")
    return text


def _matches_keyword(content: str) -> bool:
    text = content.strip()
    lowered = text.lower()
    if not text:
        return False
    for keyword in _discord_wake_keywords():
        normalized = _normalize_keyword(keyword)
        if not normalized:
            continue
        if lowered == normalized:
            return True
        if any(lowered.startswith(normalized + sep) for sep in _KEYWORD_SEPARATORS):
            return True
        if _keyword_regex(normalized).search(text):
            return True
    return False


def _is_setu_command(content: str) -> bool:
    lowered = content.strip().lower()
    if not lowered:
        return False
    return any(
        lowered == command or lowered.startswith(command + " ")
        for command in _SETU_COMMANDS
    )


def _find_artwork_url(content: str) -> str | None:
    for regex in manyacg_service.ARTWORK_ALL_REGEX:
        match = regex.search(content)
        if match:
            artwork_url = match.group()
            if not artwork_url.startswith("http"):
                artwork_url = "https://" + artwork_url
            return artwork_url
    return None


def _channel_candidate_ids(message: discord.Message) -> set[int]:
    ids = {message.channel.id}
    if message.guild is not None:
        ids.add(message.guild.id)
    parent_id = getattr(message.channel, "parent_id", None)
    if parent_id is not None:
        ids.add(parent_id)
    parent = getattr(message.channel, "parent", None)
    if parent is not None:
        ids.add(parent.id)
    return ids


async def _channel_allowed(message: discord.Message) -> bool:
    allowlist = set(app_config.discord_channel_allowlist)
    if allowlist and not bool(_channel_candidate_ids(message) & allowlist):
        return False
    if message.guild is None:
        return True
    settings = await _discord_guild_settings(message.guild)
    return settings.enabled and not settings.muted


def _is_reply_to_bot(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    ref = message.reference
    if ref is None:
        return False
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot_user.id
    cached = getattr(ref, "cached_message", None)
    if isinstance(cached, discord.Message):
        return cached.author.id == bot_user.id
    return False


def _clean_content(message: discord.Message) -> str:
    text = _message_text(message)
    text = _URL_RE.sub(lambda m: m.group(0), text)
    text = _MENTION_RE.sub("", text)
    return text.strip()


async def _should_wake(message: discord.Message, bot_user: discord.ClientUser) -> tuple[bool, str]:
    if message.author.bot:
        return False, ""
    if not isinstance(message.channel, discord.abc.Messageable):
        return False, ""

    content = _message_text(message)
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = bot_user in message.mentions
    replied_to_bot = _is_reply_to_bot(message, bot_user)
    keyword = _matches_keyword(content)
    setu_command = _is_setu_command(content)
    artwork_url = _find_artwork_url(content)

    if not content and not is_dm and not mentioned and not replied_to_bot:
        global _warned_empty_content
        if not _warned_empty_content:
            logger.warning(
                "Discord message content is empty; keyword/setu wake requires "
                "Message Content Intent to be enabled in Discord Developer Portal"
            )
            _warned_empty_content = True

    if (
        not is_dm
        and not mentioned
        and not replied_to_bot
        and not keyword
        and not setu_command
        and not artwork_url
    ):
        return False, ""
    if (
        not is_dm
        and not mentioned
        and not replied_to_bot
        and (keyword or setu_command or artwork_url)
        and not await _channel_allowed(message)
    ):
        logger.debug(
            "Discord wake ignored by server/channel state: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"candidate_ids={sorted(_channel_candidate_ids(message))}"
        )
        return False, ""

    prompt = _clean_content(message)
    if mentioned:
        prompt = _strip_bot_mention(prompt, bot_user.id)
    if keyword:
        prompt = _strip_keyword(prompt)
    if not prompt:
        prompt = "Hãy tiếp tục cuộc trò chuyện."
    return True, prompt


def _guild_name(message: discord.Message) -> str:
    return message.guild.name if message.guild else "Direct Message"


def _channel_name(message: discord.Message) -> str:
    channel = message.channel
    if isinstance(channel, discord.DMChannel):
        return "DM"
    return getattr(channel, "name", str(channel.id))


def _author_name(message: discord.Message) -> str:
    author = message.author
    return getattr(author, "display_name", author.name)


def _extract_discord_id(value: str) -> int | None:
    match = re.search(r"<@!?(\d+)>|<#(\d+)>|(\d{15,25})", value.strip())
    if not match:
        return None
    raw_id = next(group for group in match.groups() if group)
    return int(raw_id)


def _channel_info(channel: object) -> DiscordChannelInfo:
    category = getattr(getattr(channel, "category", None), "name", None)
    return DiscordChannelInfo(
        id=getattr(channel, "id", 0),
        name=getattr(channel, "name", str(getattr(channel, "id", "unknown"))),
        type=channel.__class__.__name__,
        mention=getattr(channel, "mention", None),
        category=category,
    )


def _bot_member(guild: discord.Guild | None) -> discord.Member | None:
    if guild is None:
        return None
    member = guild.me
    if member is not None:
        return member
    if _discord_client is not None and _discord_client.user is not None:
        return guild.get_member(_discord_client.user.id)
    return None


def _channel_permissions(channel: object, guild: discord.Guild | None) -> discord.Permissions | None:
    member = _bot_member(guild)
    permissions_for = getattr(channel, "permissions_for", None)
    if member is None or not callable(permissions_for):
        return None
    return permissions_for(member)


def _can_view_channel(channel: object, guild: discord.Guild | None) -> bool:
    permissions = _channel_permissions(channel, guild)
    return permissions is None or permissions.view_channel


def _can_read_message_history(channel: object, guild: discord.Guild | None) -> bool:
    permissions = _channel_permissions(channel, guild)
    return permissions is None or (
        permissions.view_channel and permissions.read_message_history
    )


def _user_text_fields(user: discord.User | discord.Member) -> list[str]:
    fields = [str(user.id), user.name, getattr(user, "display_name", "")]
    fields.append(getattr(user, "global_name", "") or "")
    fields.append(getattr(user, "nick", "") or "")
    return [field.casefold() for field in fields if field]


def _query_matches_user(user: discord.User | discord.Member, query: str) -> bool:
    normalized = query.strip().casefold().lstrip("@")
    if not normalized:
        return True
    query_id = _extract_discord_id(query)
    if query_id is not None:
        return user.id == query_id
    return any(normalized in field for field in _user_text_fields(user))


def _discord_user_info(
    user: discord.User | discord.Member, matched_by: str | None = None
) -> DiscordUserInfo:
    roles: list[str] | None = None
    if isinstance(user, discord.Member):
        roles = [role.name for role in user.roles if not role.is_default()]
        roles = roles[-8:] or None
    return DiscordUserInfo(
        id=user.id,
        name=user.name,
        display_name=getattr(user, "display_name", user.name),
        mention=user.mention,
        bot=user.bot,
        global_name=getattr(user, "global_name", None),
        nick=getattr(user, "nick", None),
        matched_by=matched_by,
        roles=roles,
    )


async def _resolve_discord_channel(
    message: discord.Message, channel_id: int | None
) -> object | None:
    if channel_id is None:
        return message.channel
    guild = message.guild
    channel = None
    if guild is not None:
        get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
        if callable(get_channel_or_thread):
            channel = get_channel_or_thread(channel_id)
        if channel is None:
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None and _discord_client is not None:
        channel = _discord_client.get_channel(channel_id)
    if channel is None and _discord_client is not None:
        try:
            channel = await _discord_client.fetch_channel(channel_id)
        except Exception as e:
            logger.debug(f"Discord fetch_channel failed for {channel_id}: {e}")
    return channel


async def _find_discord_users(
    message: discord.Message, query: str, limit: int = 5
) -> list[discord.User | discord.Member]:
    limit = max(1, min(limit, 10))
    guild = message.guild
    query_id = _extract_discord_id(query)
    users: list[discord.User | discord.Member] = []
    seen: set[int] = set()

    def add_user(user: discord.User | discord.Member | None) -> None:
        if user is None or user.id in seen:
            return
        if query and not _query_matches_user(user, query):
            return
        seen.add(user.id)
        users.append(user)

    for mentioned_user in message.mentions:
        add_user(mentioned_user)
    add_user(message.author)

    if guild is not None and query_id is not None:
        add_user(guild.get_member(query_id))
        if query_id not in seen:
            try:
                add_user(await guild.fetch_member(query_id))
            except Exception as e:
                logger.debug(f"Discord fetch_member failed for {query_id}: {e}")
    if _discord_client is not None and query_id is not None and query_id not in seen:
        add_user(_discord_client.get_user(query_id))
        if query_id not in seen:
            try:
                add_user(await _discord_client.fetch_user(query_id))
            except Exception as e:
                logger.debug(f"Discord fetch_user failed for {query_id}: {e}")

    if guild is not None:
        for member in guild.members:
            add_user(member)
            if len(users) >= limit:
                return users[:limit]
        if query.strip() and len(users) < limit:
            query_members = getattr(guild, "query_members", None)
            if callable(query_members):
                try:
                    members = await query_members(
                        query=query.strip().lstrip("@"),
                        limit=limit,
                        cache=True,
                    )
                    for member in members:
                        add_user(member)
                except Exception as e:
                    logger.debug(f"Discord query_members failed: {e}")

    if len(users) < limit and _can_read_message_history(message.channel, guild):
        history = getattr(message.channel, "history", None)
        if callable(history):
            try:
                async for previous_message in history(limit=100):
                    add_user(previous_message.author)
                    if len(users) >= limit:
                        break
            except Exception as e:
                logger.debug(f"Discord recent-author scan failed: {e}")

    return users[:limit]


async def _reply_context(message: discord.Message) -> str | None:
    ref = message.reference
    if ref is None:
        return None
    replied = ref.resolved
    if isinstance(replied, discord.Message):
        text = _clean_content(replied)
        if text:
            return f"Replying to {_author_name(replied)}: {text[:1000]}"
    return None


async def _build_prompt(message: discord.Message, user_prompt: str) -> str:
    parts = [
        "ContextInfo[Discord chat]",
        f"Server: {_guild_name(message)}",
        f"Channel: {_channel_name(message)}",
        f"User: {_author_name(message)} (id={message.author.id})",
        f"Current time: {datetime.now().isoformat(timespec='seconds')}",
    ]
    reply_ctx = await _reply_context(message)
    if reply_ctx:
        parts.append(reply_ctx)
    emoji_hint = await _discord_emoji_hint(message)
    if emoji_hint:
        parts.append(emoji_hint)
    parts.append(f"Discord R18 images allowed: {await _discord_r18_allowed(message.guild)}")
    parts.append("User message:")
    parts.append(user_prompt)
    return "\n".join(parts)


def _split_reply(text: str) -> list[str]:
    chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
    if not chunks:
        return []

    max_messages = max(1, app_config.discord_reply_max_messages)
    if len(chunks) <= max_messages:
        return chunks

    grouped: list[str] = []
    total = len(chunks)
    base = total // max_messages
    remainder = total % max_messages
    index = 0
    for i in range(max_messages):
        size = base + (1 if i < remainder else 0)
        grouped.append("\n\n".join(chunks[index : index + size]))
        index += size
    return grouped


async def _send_reply(message: discord.Message, text: str) -> None:
    chunks = _split_reply(text)
    if not chunks:
        return
    delay_min = max(0.0, app_config.discord_reply_delay_min)
    delay_max = max(delay_min, app_config.discord_reply_delay_max)
    for index, chunk in enumerate(chunks):
        if len(chunk) > 1900:
            chunk = chunk[:1900] + "…"
        await message.channel.send(chunk, reference=message if index == 0 else None)
        if index < len(chunks) - 1:
            await asyncio.sleep(random.uniform(delay_min, delay_max) + len(chunk) / 900)


def _sanitize_discord_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Remove previous tool messages before reusing Discord history.

    DeepSeek's OpenAI-compatible endpoint is strict about historical `tool`
    messages: each one must immediately follow its matching assistant
    `tool_calls` message. Discord does not need old tool internals for memory, so
    we keep user/text assistant history and drop old tool call/return messages.
    Current-turn tool calls still work normally because this only sanitizes
    cached history before the next request.
    """
    cleaned: list[ModelMessage] = []
    removed = 0
    for msg in filter_empty_model_responses(messages):
        part_kinds = {getattr(part, "part_kind", "") for part in msg.parts}
        if part_kinds & {"tool-call", "tool-return", "retry-prompt"}:
            removed += 1
            continue
        cleaned.append(msg)
    if removed:
        logger.debug(f"Discord history sanitized: removed {removed} tool messages")
    return cleaned


def _discord_artwork_view(source_url: str, original_url: str | None = None) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Chi tiết", url=source_url))
    if original_url:
        view.add_item(discord.ui.Button(label="Ảnh gốc", url=original_url))
    return view


def _discord_artwork_embed(
    *,
    title: str,
    source_url: str,
    image_url: str,
    r18: bool,
    description: str | None = None,
    index: int | None = None,
) -> discord.Embed:
    display_title = f"🔞 {title}" if r18 else title
    if index is not None:
        display_title = f"{display_title} ({index})"
    embed = discord.Embed(
        title=display_title[:256],
        url=source_url,
        description=(description or "")[:3500] or None,
        color=0xFF8AC5 if not r18 else 0xFF5C8A,
    )
    embed.set_image(url=image_url)
    embed.set_footer(text="ManyACG / Pixiv")
    return embed


async def _fetch_discord_anime_artwork(
    keyword: str = "",
) -> tuple[manyacg_service.Artwork, manyacg_service.Picture] | None:
    if manyacg_client is None:
        return None
    try:
        if keyword:
            resp = await manyacg_client.client.get(
                "/artwork/list",
                params={
                    "r18": 2,
                    "hybrid": app_config.manyacg_hybrid_search,
                    "keyword": keyword,
                },
            )
            if resp.status_code != 200:
                logger.error(f"Discord anime photo API returned {resp.status_code}")
                return None
            resp_model = manyacg_service.RandomArtworkResponse.model_validate(
                resp.json()
            )
        else:
            resp_model = await manyacg_client.random_artwork(limit=1, r18=2)
        if resp_model.status != 200 or not resp_model.data:
            logger.error(
                "Discord anime photo API failed: "
                f"status={resp_model.status} message={resp_model.message!r}"
            )
            return None
        artwork = random.choice(resp_model.data)
        if not artwork.pictures:
            return None
        return artwork, random.choice(artwork.pictures)
    except Exception as e:
        logger.error(f"Discord anime photo fetch error: {e.__class__.__name__}: {e}")
        return None


async def _send_discord_anime_photo_card(
    message: discord.Message,
    artwork: manyacg_service.Artwork,
    picture: manyacg_service.Picture,
) -> None:
    original_url = f"https://t.me/{app_config.manyacg_bot}/?start=file_{picture.id}"
    embed = _discord_artwork_embed(
        title=artwork.title,
        source_url=artwork.source_url,
        image_url=picture.regular,
        r18=artwork.r18,
    )
    await message.channel.send(
        embed=embed,
        view=_discord_artwork_view(artwork.source_url, original_url),
        reference=message,
    )


async def get_discord_server_info(
    ctx: RunContext[DiscordContextDeps], channel_limit: int = 25
) -> DiscordServerInfoResult:
    """Get information about the current Discord server and visible channels.

    Use this when the user asks about the Discord server, current channel,
    available channels, or general server context.

    Args:
        channel_limit: Maximum number of visible channels to return.
    """
    message = ctx.deps.message
    guild = message.guild
    if guild is None:
        return DiscordServerInfoResult(
            success=False,
            message="This is a DM, so there is no Discord server to inspect.",
        )

    limit = max(1, min(channel_limit, 50))
    channels: list[DiscordChannelInfo] = []
    guild_channels = sorted(
        guild.channels,
        key=lambda channel: (
            getattr(channel, "position", 0),
            getattr(channel, "name", ""),
        ),
    )
    for channel in guild_channels:
        if not _can_view_channel(channel, guild):
            continue
        channels.append(_channel_info(channel))
        if len(channels) >= limit:
            break

    owner_id = guild.owner_id
    return DiscordServerInfoResult(
        success=True,
        data=DiscordServerInfo(
            id=guild.id,
            name=guild.name,
            member_count=guild.member_count,
            owner_id=owner_id,
            owner_mention=f"<@{owner_id}>" if owner_id else None,
            current_channel=_channel_info(message.channel),
            channels=channels,
        ),
    )


async def find_discord_user(
    ctx: RunContext[DiscordContextDeps], query: str, limit: int = 5
) -> DiscordUserSearchResult:
    """Find Discord users in the current server/chat.

    Query may be a username, display name, server nickname, global name, mention,
    or numeric Discord user ID. Returns mention strings the AI can use to tag
    matching users.

    Args:
        query: User search text, mention, or user ID.
        limit: Maximum number of users to return.
    """
    users = await _find_discord_users(ctx.deps.message, query, limit)
    if not users:
        return DiscordUserSearchResult(
            success=False,
            message=f"No Discord users found for query: {query!r}.",
            users=[],
        )
    return DiscordUserSearchResult(
        success=True,
        users=[_discord_user_info(user, matched_by=query) for user in users],
    )


async def mention_discord_user(
    ctx: RunContext[DiscordContextDeps], query: str
) -> DiscordMentionResult:
    """Resolve a Discord user and return a mention string like <@123>.

    Use this when the user asks Waku to tag, ping, call, mention, or notify a
    specific Discord user. Put the returned `mention` directly in the final reply.

    Args:
        query: Username, display name, nickname, mention, or Discord user ID.
    """
    users = await _find_discord_users(ctx.deps.message, query, limit=1)
    if not users:
        return DiscordMentionResult(
            success=False,
            message=f"Could not resolve a Discord user for query: {query!r}.",
        )
    user_info = _discord_user_info(users[0], matched_by=query)
    return DiscordMentionResult(
        success=True,
        mention=user_info.mention,
        user=user_info,
    )


async def search_discord_messages(
    ctx: RunContext[DiscordContextDeps],
    query: str,
    limit: int = 100,
    max_results: int = 8,
    channel_id: int | None = None,
) -> DiscordMessageSearchResult:
    """Search recent readable Discord messages.

    Searches the current channel by default. Only searches channels the bot can
    read and where it has Read Message History permission.

    Args:
        query: Text to search for. Case-insensitive substring match.
        limit: Number of recent messages to scan, capped for safety.
        max_results: Maximum matching snippets to return.
        channel_id: Optional Discord channel ID. Defaults to current channel.
    """
    message = ctx.deps.message
    guild = message.guild
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return DiscordMessageSearchResult(
            success=False,
            message="Search query is empty.",
        )

    channel = await _resolve_discord_channel(message, channel_id)
    if channel is None:
        return DiscordMessageSearchResult(
            success=False,
            message="Could not find that Discord channel.",
        )
    if not _can_read_message_history(channel, guild):
        return DiscordMessageSearchResult(
            success=False,
            message="I do not have permission to read message history in that channel.",
        )
    history = getattr(channel, "history", None)
    if not callable(history):
        return DiscordMessageSearchResult(
            success=False,
            message="That Discord channel does not support message history search.",
        )

    scan_limit = max(1, min(limit, 300))
    result_limit = max(1, min(max_results, 20))
    matches: list[DiscordMessageSearchEntry] = []
    searched = 0
    try:
        async for previous_message in history(limit=scan_limit):
            searched += 1
            content = _message_text(previous_message)
            if not content or normalized_query not in content.casefold():
                continue
            matches.append(
                DiscordMessageSearchEntry(
                    message_id=previous_message.id,
                    channel_id=previous_message.channel.id,
                    channel_name=_channel_name(previous_message),
                    author_id=previous_message.author.id,
                    author_name=_author_name(previous_message),
                    author_mention=previous_message.author.mention,
                    created_at=previous_message.created_at.isoformat(),
                    content=content[:800],
                    jump_url=previous_message.jump_url,
                )
            )
            if len(matches) >= result_limit:
                break
    except discord.Forbidden:
        return DiscordMessageSearchResult(
            success=False,
            message="Discord denied access to that channel history.",
            searched=searched,
        )
    except Exception as e:
        logger.error(f"Discord message search error: {e.__class__.__name__}: {e}")
        return DiscordMessageSearchResult(
            success=False,
            message="Discord message search failed.",
            searched=searched,
        )

    return DiscordMessageSearchResult(
        success=True,
        message=None if matches else "No matching recent messages found.",
        searched=searched,
        matches=matches,
    )


async def _search_web_images(query: str, max_results: int = 8) -> list[dict]:
    def _search() -> list[dict]:
        with DDGS() as ddgs:
            return list(ddgs.images(query, max_results=max_results, safesearch="moderate"))

    return await asyncio.to_thread(_search)


async def _download_image_bytes(url: str) -> tuple[bytes, str] | None:
    max_bytes = 8_000_000
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "WakuDiscordBot/1.0"}) as response:
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                return None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None
            return bytes(data), content_type


def _image_filename(content_type: str) -> str:
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(content_type, "jpg")
    return f"waku_image.{extension}"


async def send_discord_web_image(
    ctx: RunContext[DiscordContextDeps], query: str
) -> DiscordWebImageResult:
    """Search the web for an image and upload it to the current Discord channel.

    Use this when the user asks for a general internet/web image that is not
    specifically an anime/Pixiv/setu image. This uploads the image to Discord.

    Args:
        query: Image search query.
    """
    message = ctx.deps.message
    search_query = query.strip()
    if not search_query:
        return DiscordWebImageResult(success=False, message="Image search query is empty.")
    if (
        not await _discord_r18_allowed(message.guild)
        and _contains_r18_keyword(search_query)
    ):
        return DiscordWebImageResult(
            success=False,
            message="R18 web image search is disabled in this Discord server.",
        )

    try:
        results = await _search_web_images(search_query)
    except Exception as e:
        logger.error(f"Discord web image search error: {e.__class__.__name__}: {e}")
        return DiscordWebImageResult(success=False, message="Web image search failed.")

    for result in results:
        image_url = result.get("image") or result.get("thumbnail")
        if not image_url:
            continue
        title = str(result.get("title") or search_query)
        source_url = str(result.get("url") or image_url)
        if (
            not await _discord_r18_allowed(message.guild)
            and _contains_r18_keyword(" ".join([title, source_url, image_url]))
        ):
            continue
        try:
            downloaded = await _download_image_bytes(str(image_url))
        except Exception as e:
            logger.debug(f"Discord web image download skipped: {e.__class__.__name__}: {e}")
            continue
        if downloaded is None:
            continue
        data, content_type = downloaded
        file = discord.File(io.BytesIO(data), filename=_image_filename(content_type))
        embed = discord.Embed(
            title=title[:256],
            url=source_url,
            description=f"Web image search: `{search_query[:120]}`",
            color=0x8AC5FF,
        )
        embed.set_image(url=f"attachment://{file.filename}")
        await message.channel.send(embed=embed, file=file, reference=message)
        return DiscordWebImageResult(
            success=True,
            title=title,
            source_url=source_url,
        )

    return DiscordWebImageResult(
        success=False,
        message="Could not find a downloadable image result.",
    )


async def send_discord_anime_photo(
    ctx: RunContext[DiscordContextDeps], keyword: str = ""
) -> DiscordAnimePhotoResult:
    """Get and send an anime/Pixiv image to the current Discord chat.

    Use this tool when the user naturally asks Waku to send an anime image,
    Pixiv image, picture, photo, setu, or similar. Only call it when sending an
    actual image is appropriate in the conversation.

    Args:
        keyword: Optional search keyword for a more specific anime/Pixiv image.
    """
    message = ctx.deps.message
    if not await _discord_r18_allowed(message.guild) and _contains_r18_keyword(keyword):
        return DiscordAnimePhotoResult(
            success=False,
            message="R18 image sending is disabled in this Discord server.",
        )
    if manyacg_client is None:
        return DiscordAnimePhotoResult(
            success=False,
            message="ManyACG is not configured, so Discord cannot send anime photos.",
        )

    ratekey = f"discord_anime_photo_rate_limit:{message.channel.id}:{message.author.id}"
    current_count = await common.memttlcache.get(ratekey, 0)
    if current_count > 3:
        return DiscordAnimePhotoResult(
            success=False,
            message="You are sending image requests too frequently. Please try again later.",
        )
    await common.memttlcache.set(ratekey, current_count + 1, ttl=10)

    logger.info(
        "Discord tool call: send_discord_anime_photo "
        f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
        f"user={message.author.id} keyword={keyword!r}"
    )
    fetched = await _fetch_discord_anime_artwork(keyword.strip())
    if fetched is None:
        return DiscordAnimePhotoResult(
            success=False,
            message="Failed to fetch anime artwork from ManyACG.",
        )
    artwork, picture = fetched
    if artwork.r18 and not await _discord_r18_allowed(message.guild):
        return DiscordAnimePhotoResult(
            success=False,
            message="R18 image sending is disabled in this Discord server.",
        )
    await _send_discord_anime_photo_card(message, artwork, picture)
    artist = None
    if artwork.artist is not None:
        artist = DiscordArtistInfo(
            name=artwork.artist.name,
            type=artwork.artist.type,
            username=artwork.artist.username,
            uid=artwork.artist.uid,
        )
    return DiscordAnimePhotoResult(
        success=True,
        data=DiscordAnimePhotoInfo(
            title=artwork.title,
            source_url=artwork.source_url,
            r18=artwork.r18,
            description=artwork.description[:512],
            artist=artist,
            tags=artwork.tags[:10],
        ),
    )


async def _send_discord_setu(message: discord.Message) -> bool:
    if manyacg_client is None:
        await message.channel.send("ManyACG chưa được cấu hình nên chưa gửi ảnh được.", reference=message)
        return True

    ratekey = f"discord_setu_cd:{message.channel.id}:{message.author.id}"
    if await common.memttlcache.get(ratekey, False):
        await message.channel.send("Từ từ đã, đợi cooldown xíu nha.", reference=message)
        return True
    await common.memttlcache.set(ratekey, True, ttl=app_config.manyacg_setu_cd)

    try:
        async with message.channel.typing():
            fetched = await _fetch_discord_anime_artwork()
        if fetched is None:
            await message.channel.send("Không lấy được ảnh từ ManyACG rồi.", reference=message)
            return True
        artwork, picture = fetched
        if artwork.r18 and not await _discord_r18_allowed(message.guild):
            await message.channel.send("Server này đang tắt R18 nên Waku không gửi ảnh này nha.", reference=message)
            return True
        await _send_discord_anime_photo_card(message, artwork, picture)
        return True
    except Exception as e:
        logger.error(f"Discord setu error: {e.__class__.__name__}: {e}")
        await message.channel.send("Gửi ảnh lỗi rồi, thử lại sau nha.", reference=message)
        return True


async def _send_discord_artwork(message: discord.Message, artwork_url: str) -> bool:
    if manyacg_client is None:
        await message.channel.send("ManyACG chưa được cấu hình nên chưa parse Pixiv được.", reference=message)
        return True
    if not app_config.manyacg_api_key:
        await message.channel.send("ManyACG API key chưa có nên chưa parse link Pixiv được.", reference=message)
        return True

    try:
        async with message.channel.typing():
            resp = await manyacg_client.fetch_artwork(artwork_url)
        if resp.status != 200 or resp.data is None:
            await message.channel.send("Không lấy được artwork từ link này.", reference=message)
            return True
        artwork = resp.data
        if artwork.r18 and not await _discord_r18_allowed(message.guild):
            await message.channel.send("Server này đang tắt R18 nên Waku không gửi artwork này nha.", reference=message)
            return True
        pictures = sorted(artwork.pictures or [], key=lambda item: item.index)
        if not pictures:
            await message.channel.send("Artwork này không có ảnh để gửi.", reference=message)
            return True
        embeds = [
            _discord_artwork_embed(
                title=artwork.title,
                source_url=artwork.source_url,
                image_url=picture.original,
                r18=artwork.r18,
                description=artwork.description if index == 1 else None,
                index=index if len(pictures) > 1 else None,
            )
            for index, picture in enumerate(pictures[:4], start=1)
        ]
        if len(pictures) > 4:
            embeds[0].add_field(
                name="Còn nữa",
                value=f"Artwork có {len(pictures)} ảnh, đang gửi 4 ảnh đầu.",
                inline=False,
            )
        await message.channel.send(
            embeds=embeds,
            view=_discord_artwork_view(artwork.source_url),
            reference=message,
        )
        return True
    except Exception as e:
        logger.error(f"Discord artwork parse error: {e.__class__.__name__}: {e}")
        await message.channel.send("Parse link Pixiv/artwork lỗi rồi.", reference=message)
        return True


class DiscordConfigView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=300)
        self.guild = guild
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.delete()
        except Exception:
            pass

    async def _sync_button(self) -> None:
        settings = await _discord_guild_settings(self.guild)
        button = self.children[0]
        if isinstance(button, discord.ui.Button):
            button.label = f"R18: {'ON' if settings.allow_r18 else 'OFF'}"
            button.style = discord.ButtonStyle.danger if settings.allow_r18 else discord.ButtonStyle.secondary

    @discord.ui.button(label="R18", style=discord.ButtonStyle.secondary)
    async def toggle_r18(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        settings = await _discord_guild_settings(self.guild)
        settings.allow_r18 = not settings.allow_r18
        await _set_discord_guild_settings(self.guild, settings)
        await self._sync_button()
        await interaction.response.edit_message(
            content=await _discord_config_text(self.guild),
            view=self,
        )

    @discord.ui.button(label="Lưu", style=discord.ButtonStyle.success)
    async def save_config(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.stop()


async def _discord_config_text(guild: discord.Guild) -> str:
    settings = await _discord_guild_settings(guild)
    return (
        "**Waku Discord config**\n"
        f"- Enabled: `{settings.enabled}`\n"
        f"- Muted: `{settings.muted}`\n"
        f"- R18 images: `{'ON' if settings.allow_r18 else 'OFF'}`\n"
        "\nBật/tắt tuỳ chọn rồi bấm **Lưu** để đóng menu."
    )


async def _handle_discord_admin_command(message: discord.Message) -> bool:
    prefix = app_config.discord_command_prefix or "!"
    content = _message_text(message)
    if not content.startswith(prefix):
        return False
    command = content[len(prefix) :].strip().split(maxsplit=1)[0].lower()
    if command not in {"waku", "unwaku", "mute", "unmute", "config"}:
        return False
    if message.guild is None:
        return True
    if not _is_discord_admin(message):
        return True

    settings = await _discord_guild_settings(message.guild)
    match command:
        case "waku":
            settings.enabled = True
            settings.muted = False
            await _set_discord_guild_settings(message.guild, settings)
            await _send_admin_notice(message, f"Đã bật Waku cho server **{message.guild.name}**.")
        case "unwaku":
            settings.enabled = False
            await _set_discord_guild_settings(message.guild, settings)
            await _send_admin_notice(message, f"Đã tắt Waku cho server **{message.guild.name}**.")
        case "mute":
            settings.muted = True
            await _set_discord_guild_settings(message.guild, settings)
            await _send_admin_notice(message, "Đã mute AI chat trong server này.")
        case "unmute":
            settings.muted = False
            await _set_discord_guild_settings(message.guild, settings)
            await _send_admin_notice(message, "Đã mở mute AI chat trong server này.")
        case "config":
            await _set_discord_guild_settings(message.guild, settings)
            view = DiscordConfigView(message.guild)
            await view._sync_button()
            config_message = await message.channel.send(
                await _discord_config_text(message.guild),
                view=view,
                reference=message,
            )
            view.message = config_message
            try:
                await message.delete()
            except Exception:
                pass
    logger.info(
        f"Discord admin command: guild={message.guild.id} user={message.author.id} command={command}"
    )
    return True


async def _maybe_handle_discord_media_request(message: discord.Message) -> bool:
    content = _clean_content(message) or _message_text(message)
    artwork_url = _find_artwork_url(content)
    if artwork_url:
        logger.info(
            f"Discord artwork request: guild={_guild_name(message)!r} "
            f"channel={_channel_name(message)!r} user={message.author.id} url={artwork_url}"
        )
        return await _send_discord_artwork(message, artwork_url)
    if _is_setu_command(content):
        logger.info(
            f"Discord command setu request: guild={_guild_name(message)!r} "
            f"channel={_channel_name(message)!r} user={message.author.id}"
        )
        return await _send_discord_setu(message)
    return False


async def _handle_message(message: discord.Message, user_prompt: str) -> None:
    if _discord_agent is None:
        return

    waiting_key = _waiting_key(message.author.id)
    if await common.memstore.get(waiting_key):
        await message.channel.send("Thinking...", reference=message)
        return

    history_key = _history_key(message.channel.id, message.author.id)
    history: list[ModelMessage] = await common.memttlcache.get(history_key, [])
    history = _sanitize_discord_history(history)
    prompt = await _build_prompt(message, user_prompt)

    await common.memstore.set(waiting_key, True)
    try:
        async with message.channel.typing():
            result = await _discord_agent.run(
                user_prompt=prompt,
                message_history=history[-app_config.discord_message_history_limit :],
                deps=DiscordContextDeps(message=message),
            )
        await common.memttlcache.set(
            history_key,
            _sanitize_discord_history(result.all_messages()),
            ttl=app_config.cachettl_agent_history,
        )
        if result.output:
            await _send_reply(message, str(result.output))
    except Exception as e:
        logger.error(f"Discord agent error: {e.__class__.__name__}: {e}")
        await message.channel.send("AI đang bị lỗi nhẹ, thử lại sau nha.", reference=message)
    finally:
        await common.memstore.delete(waiting_key)


def _create_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    intents.members = app_config.discord_members_intent
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        user = client.user
        if user is None:
            logger.warning("Discord client ready without user")
            return
        logger.success(f"Discord AI chat ready as {user} ({user.id})")

    @client.event
    async def on_message(message: discord.Message) -> None:
        bot_user = client.user
        if bot_user is None or message.author.bot:
            return
        content = _message_text(message)
        if await _handle_discord_admin_command(message):
            return
        await _remember_discord_emojis(message)
        should_wake, prompt = await _should_wake(message, bot_user)
        if not should_wake:
            return
        logger.debug(
            "Discord wake: "
            f"guild={_guild_name(message)!r} channel={_channel_name(message)!r} "
            f"user={message.author.id} len={len(content)} "
            f"keyword={_matches_keyword(content)} "
            f"setu_command={_is_setu_command(content)} "
            f"allowed={await _channel_allowed(message)}"
        )
        if await _maybe_handle_discord_media_request(message):
            return
        await _handle_message(message, prompt)

    return client


async def start_discord_bot() -> None:
    global _discord_agent, _discord_client, _discord_task

    if not app_config.discord_enabled:
        logger.debug("Discord AI chat disabled")
        return
    if not app_config.discord_token:
        logger.warning("Discord AI chat enabled but discord_token is empty")
        return
    if not app_config.agent or not app_config.agent_model:
        logger.warning("Discord AI chat requires agent=true and agent_model")
        return

    _discord_agent = Agent(
        model=provider.make_chat_model(app_config.agent_model),
        instructions=(
            f"{app_config.agent_group_prompt or app_config.agent_prompt}\n\n"
            "Discord style: keep Waku's cute, playful chat style. "
            "Use natural emojis/emoticons in most casual replies, usually 1-3, "
            "but do not spam them or add them to serious/admin/error messages. "
            "Discord image behavior: if the user clearly asks Waku to send an "
            "anime/Pixiv image, photo, picture, setu, ảnh, or hình, choose "
            "whether it fits the conversation and call send_discord_anime_photo "
            "once when it does. For general internet/web image requests, use "
            "send_discord_web_image with a concise search query. Do not mention "
            "tool internals to the user. "
            "Discord context tools: when useful, Waku may inspect the current "
            "server/channel, resolve Discord users, mention users with returned "
            "<@user_id> mention strings, and search recent readable channel "
            "messages. Only search chat when the user asks or it clearly helps. "
            "If a Discord permission is missing, explain that briefly."
        ),
        output_type=str,
        tools=[
            Tool(get_discord_server_info, sequential=True),
            Tool(find_discord_user, sequential=True),
            Tool(mention_discord_user, sequential=True),
            Tool(search_discord_messages, sequential=True),
            Tool(send_discord_web_image, sequential=True),
            Tool(send_discord_anime_photo, sequential=True),
        ],
        retries=3,
    )
    _discord_client = _create_client()
    _discord_task = asyncio.create_task(_discord_client.start(app_config.discord_token))
    logger.info("Discord AI chat startup scheduled")


async def stop_discord_bot() -> None:
    global _discord_agent, _discord_client, _discord_task

    if _discord_client is not None:
        await _discord_client.close()
    if _discord_task is not None:
        try:
            await _discord_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Discord client stopped with error: {e.__class__.__name__}: {e}")
    _discord_task = None
    _discord_client = None
    _discord_agent = None
