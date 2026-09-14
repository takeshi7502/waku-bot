import tomllib

from scripts.repair_settings_toml import repair_text


def test_repair_text_escapes_old_editor_multiline_basic_string():
    original = '''agent_prompt = "Dòng đầu
Dòng thứ hai"
agent = true
'''

    repaired, changes = repair_text(original)

    assert changes == 1
    assert 'agent_prompt = "Dòng đầu\\nDòng thứ hai"' in repaired
    assert tomllib.loads(repaired) == {
        "agent_prompt": "Dòng đầu\nDòng thứ hai",
        "agent": True,
    }


def test_repair_text_preserves_valid_toml_without_changes():
    original = 'agent_prompt = "Dòng đầu\\nDòng thứ hai"\nagent = true\n'

    repaired, changes = repair_text(original)

    assert changes == 0
    assert repaired == original
