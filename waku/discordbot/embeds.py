from __future__ import annotations

import discord


def discord_command_embed(
    description: str,
    *,
    title: str = "Waku",
    color: discord.Color | None = None,
) -> discord.Embed:
    """Create the consistent embed used for Discord command responses."""
    return discord.Embed(
        title=title,
        description=description,
        color=color if color is not None else discord.Color.blurple(),
    )
