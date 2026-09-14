"""Compatibility exports for the legacy non-AI reply implementation."""

from waku.plugins.simple_reply import bot_reply_if_enabled, word_reply

__all__ = ["bot_reply_if_enabled", "word_reply"]
