# Discord bot package

`waku.discordbot` is the first-class Discord application layer. It is independent of Telegram's plugin package and exposes only lifecycle/status APIs to the rest of Waku.

## Public API

Import `start_discord_bot`, `stop_discord_bot`, and `get_discord_runtime_status` from `waku.discordbot`. Do not read or mutate runtime globals outside this package.

## Module ownership

- `runtime.py`: composition root, agent/tool registration, startup and shutdown.
- `state.py`: mutable runtime references, locks, and concurrency gates.
- `client.py`: Discord client/intents and event registration.
- `handlers.py`: event and command orchestration.
- `agent.py`: one-turn execution, recovery, and model error handling.
- `models.py`: Discord-only dataclasses and result types.
- `constants.py`: Discord-only constants and regex patterns.
- `utilities.py`: shared Discord object and text helpers.
- `settings.py`: guild/DM settings and settings cache.
- `permissions.py`: Discord permission and authorization checks.
- `history.py`: history, emoji/reaction style, and group memory.
- `messages.py`: wake parsing, prompt construction, and replies.
- `scheduling.py`: persistent Discord message/image schedules.
- `media.py`: attachment, artwork, and image transport helpers.
- `tools/`: focused AI tool implementations.
- `views/`: persistent and interactive Discord UI views.

## Extension rules

1. Add Discord features inside this package, not `waku.plugins`.
2. Put AI-callable operations in the appropriate `tools/` module.
3. Keep event callbacks thin and delegate from `client.py` to `handlers.py`.
4. Store mutable process state only in `state.py`.
5. Keep shared database, scheduler, config, and AI provider infrastructure in their existing Waku packages.
6. Preserve scheduler job IDs and account for persisted callable paths when moving job functions.
7. Avoid importing the constructed agent from tool/domain modules; `runtime.py` wires dependencies together.
