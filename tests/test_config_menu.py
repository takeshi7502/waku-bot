from pathlib import Path

from waku.plugins import version


def _parse(tmp_path: Path, monkeypatch, content: str):
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(version, "_SETTINGS_PATH", settings_path)
    return version._parse_settings()


def test_parse_settings_groups_current_unnumbered_format(tmp_path, monkeypatch):
    groups, entries, providers = _parse(
        tmp_path,
        monkeypatch,
        '''
token = "secret"
timezone = "Asia/Ho_Chi_Minh"
webapp = true
webapp_url = "https://example.com"
discord_enabled = false
agent = true
agent_model = "default/model"
manyacg_api_url = "https://api.example.com"
agent_sticker_memory = true
agent_periodic_reaction_interval = 5

[agent_providers.default]
url = "https://llm.example.com/v1"
key = "provider-secret"
type = "chat_completions"
''',
    )

    assert [group.group_id for group in groups] == [
        "general",
        "webapp",
        "discord",
        "agent",
        "manyacg",
        "sticker",
        "providers",
    ]
    assert {key for group in groups for key in group.keys} == {
        key for key, entry in entries.items() if entry.table is None
    }
    assert providers == ["default"]


def test_parse_settings_preserves_legacy_numbered_groups(tmp_path, monkeypatch):
    groups, _, _ = _parse(
        tmp_path,
        monkeypatch,
        '''
# 1. General
lang = "vi-VN"
# 2. Agent
agent = true
# 3. Hidden legacy section
token = "secret"
''',
    )

    assert [(group.group_id, group.keys) for group in groups] == [
        ("g1", ["lang"]),
        ("g2", ["agent"]),
    ]


def test_parse_settings_ignores_assignments_inside_multiline_prompt(tmp_path, monkeypatch):
    groups, entries, _ = _parse(
        tmp_path,
        monkeypatch,
        '''
agent_prompt = """
fake_key = "not a setting"
"""
agent_model = "default/model"
''',
    )

    assert set(entries) == {"agent_prompt", "agent_model"}
    assert groups[0].group_id == "agent"
    assert groups[0].keys == ["agent_prompt", "agent_model"]


def test_group_markup_paginates_long_groups(tmp_path, monkeypatch):
    settings = "\n".join(f'agent_option_{index} = "value-{index}"' for index in range(12))
    _parse(tmp_path, monkeypatch, settings)
    version._SESSIONS.clear()

    text, markup = version._group_markup(123, "agent", 0)

    assert "Page: <b>1/2</b>" in text
    assert "agent_option_0" in text
    assert "agent_option_10" not in text
    assert sum(len(row) for row in markup.inline_keyboard) <= version._VAR_PAGE_SIZE + 3


def test_add_and_delete_provider_are_separate_operations(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text('agent = true\n', encoding="utf-8")
    monkeypatch.setattr(version, "_SETTINGS_PATH", settings_path)

    version._add_provider("new-provider")
    assert "[agent_providers.new-provider]" in settings_path.read_text(encoding="utf-8")

    version._delete_provider("new-provider")
    assert "[agent_providers.new-provider]" not in settings_path.read_text(encoding="utf-8")
