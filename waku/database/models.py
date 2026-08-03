from dataclasses import asdict, dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


@dataclass
class UserConfig:
    lang: str = "vi-VN"
    affection: int = 0
    coins: int = 144 * 16
    dm_ai_enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> "UserConfig":
        if data is None:
            return cls()
        return cls(
            lang=data.get("lang", "vi-VN"),
            coins=data.get("coins", 144 * 16),
            affection=data.get("affection", 0),
            dm_ai_enabled=data.get("dm_ai_enabled", False),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatConfig:
    waifu_enabled: bool = True
    delete_events_enabled: bool = False
    unpin_channel_pin_enabled: bool = False
    message_search_enabled: bool = False
    quote_probability: float = 0.001
    quote_pin_message: bool = True
    title_permissions: dict | None = None
    greeting: str | None = None
    ai_reply: bool = True
    ai_reply_other_bots_enabled: bool = False
    ai_comment: bool = False
    agent_group_manage_enabled: bool = True
    agent_schedule_enabled: bool = True
    agent_ban_users_enabled: bool = False
    agent_mute_users_enabled: bool = False
    agent_automod_enabled: bool = False
    setu_enabled: bool = True
    convert_b23_enabled: bool = True
    parse_artwork_enabled: bool = True
    pick_bottle_enabled: bool = True
    group_memory_enabled: bool = True
    discord_enabled: bool = False
    discord_muted: bool = False
    discord_allow_r18: bool = False
    discord_r18_mode: int = 0
    discord_ai_reply: bool = True
    discord_auth_status: str = "none"
    discord_auth_requester_id: int | None = None
    discord_auth_channel_id: int | None = None
    discord_auth_requested_at: str | None = None
    discord_auth_rejection_reason: str | None = None
    discord_auth_review_messages: list[dict] | None = None
    lang: str = "vi-VN"

    @classmethod
    def from_dict(cls, data: dict | None) -> "ChatConfig":
        if data is None:
            return cls()
        return cls(
            waifu_enabled=data.get("waifu_enabled", True),
            delete_events_enabled=data.get("delete_events_enabled", False),
            unpin_channel_pin_enabled=data.get("unpin_channel_pin_enabled", False),
            message_search_enabled=data.get("message_search_enabled", False),
            quote_probability=data.get("quote_probability", 0.001),
            quote_pin_message=data.get("quote_pin_message", False),
            title_permissions=data.get("title_permissions", {}),
            greeting=data.get("greeting", None),
            ai_reply=data.get("ai_reply", True),
            ai_reply_other_bots_enabled=data.get("ai_reply_other_bots_enabled", False),
            setu_enabled=data.get("setu_enabled", True),
            convert_b23_enabled=data.get("convert_b23_enabled", False),
            parse_artwork_enabled=data.get("parse_artwork_enabled", True),
            pick_bottle_enabled=data.get("pick_bottle_enabled", True),
            ai_comment=data.get("ai_comment", False),
            agent_group_manage_enabled=data.get(
                "agent_group_manage_enabled",
                data.get("agent_ban_users_enabled", True)
                or data.get("agent_mute_users_enabled", False),
            ),
            agent_schedule_enabled=data.get("agent_schedule_enabled", True),
            agent_ban_users_enabled=data.get("agent_ban_users_enabled", False),
            agent_mute_users_enabled=data.get("agent_mute_users_enabled", False),
            agent_automod_enabled=data.get("agent_automod_enabled", False),
            group_memory_enabled=data.get("group_memory_enabled", True),
            discord_enabled=data.get("discord_enabled", False),
            discord_muted=data.get("discord_muted", False),
            discord_allow_r18=data.get("discord_allow_r18", False),
            discord_r18_mode=data.get(
                "discord_r18_mode",
                2 if data.get("discord_allow_r18", False) else 0,
            ),
            discord_ai_reply=data.get("discord_ai_reply", True),
            discord_auth_status=data.get("discord_auth_status", "none"),
            discord_auth_requester_id=data.get("discord_auth_requester_id"),
            discord_auth_channel_id=data.get("discord_auth_channel_id"),
            discord_auth_requested_at=data.get("discord_auth_requested_at"),
            discord_auth_rejection_reason=data.get("discord_auth_rejection_reason"),
            discord_auth_review_messages=data.get("discord_auth_review_messages"),
            lang=data.get("lang", "vi-VN"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class UserChatAssociation(Base):
    __tablename__ = "user_chat_association"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chat_data.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )

    waifu_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_bot_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    member_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    member_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    member_is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    member_privileges: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_member_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    gay_mode_previous_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gay_mode_applied: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserData(Base):
    __tablename__ = "user_data"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        index=True,
    )

    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)

    avatar_big_id: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    config: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: asdict(UserConfig()),
    )
    is_married: Mapped[bool] = mapped_column(Boolean, default=False)
    married_waifu_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    waifu_mention: Mapped[bool] = mapped_column(Boolean, default=False)

    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    is_real_user: Mapped[bool] = mapped_column(Boolean, default=True)
    is_bot_global_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    update_avatar_at: Mapped[datetime | None] = mapped_column(
        DateTime(),
        nullable=True,
        default=None,
    )

    chats: Mapped[list["ChatData"]] = relationship(
        "ChatData",
        secondary="user_chat_association",
        back_populates="members",
        primaryjoin="UserData.id == UserChatAssociation.user_id",
        secondaryjoin="ChatData.id == UserChatAssociation.chat_id",
        lazy="noload",
    )

    quotes: Mapped[list["Quote"]] = relationship(
        "Quote",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    married_waifu: Mapped["UserData | None"] = relationship(
        "UserData",
        remote_side=[id],
        post_update=True,
    )

    @property
    def user_config(self) -> UserConfig:
        return UserConfig.from_dict(self.config)

    @user_config.setter
    def user_config(self, config: UserConfig) -> None:
        self.config = config.to_dict()

    def __repr__(self) -> str:
        return f"<UserData(id={self.id}, username='{self.username}', full_name='{self.full_name}')>"


class ChatData(Base):
    __tablename__ = "chat_data"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(256), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    config: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: asdict(ChatConfig()),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    members: Mapped[list["UserData"]] = relationship(
        "UserData",
        secondary="user_chat_association",
        back_populates="chats",
        primaryjoin="ChatData.id == UserChatAssociation.chat_id",
        secondaryjoin="UserData.id == UserChatAssociation.user_id",
        lazy="noload",
    )

    quotes: Mapped[list["Quote"]] = relationship(
        "Quote",
        back_populates="chat",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    @property
    def chat_config(self) -> ChatConfig:
        return ChatConfig.from_dict(self.config)

    @chat_config.setter
    def chat_config(self, config: ChatConfig) -> None:
        self.config = config.to_dict()

    def __repr__(self) -> str:
        return f"<ChatData(id={self.id}, title='{self.title}', username='{self.username}')>"


class Quote(Base):
    __tablename__ = "quotes"

    link: Mapped[str] = mapped_column(String(256), primary_key=True)

    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chat_data.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    qer_id: Mapped[int] = mapped_column(
        BigInteger,
        index=True,
    )

    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    text: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    img: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["UserData"] = relationship(
        foreign_keys=[user_id],
        back_populates="quotes",
        lazy="noload",
    )
    chat: Mapped["ChatData"] = relationship(
        "ChatData",
        back_populates="quotes",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<Quote(link='{self.link}', chat_id={self.chat_id}, user_id={self.user_id})>"


class Bottle(Base):
    __tablename__ = "bottles"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        index=True,
    )

    sender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(4096), nullable=True)
    picks: Mapped[int] = mapped_column(BigInteger, default=0)
    reports: Mapped[int] = mapped_column(BigInteger, default=0)
    file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """
    meida_type can be one of the following:
    - image
    - video
    - audio
    - document
    - voice
    - None (for text-only bottles)
    """
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_picked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    def __repr__(self) -> str:
        return f"<Bottle(id={self.id}, sender_id={self.sender_id})>"


class BottleReply(Base):
    __tablename__ = "bottle_replies"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        index=True,
    )
    bottle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("bottles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    replier_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(4096), nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false")
    )
    file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<BottleReply(id={self.id}, bottle_id={self.bottle_id}, replier_id={self.replier_id})>"


class Gift(Base):
    __tablename__ = "gifts"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        index=True,
    )
    owner_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user_data.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rarity: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_to_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    gift_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Gift(id={self.id}, owner_id={self.owner_id}, gift_id='{self.gift_id}')>"
        )
