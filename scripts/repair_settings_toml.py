#!/usr/bin/env python3
"""Repair the malformed one-line TOML strings written by older /config builds.

Older versions of the configuration editor wrote actual newline characters into
double-quoted TOML strings.  TOML only permits those inside triple-quoted
strings, so Dynaconf could not even start the bot.  This migration is purposely
conservative: it changes only a clearly broken double-quoted value that can be
decoded safely after its newlines are escaped.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


_ASSIGNMENT = re.compile(r"^(?P<prefix>\s*[A-Za-z0-9_-]+\s*=\s*)(?P<value>.*)$")


def _ends_with_unescaped_quote(value: str) -> bool:
    value = value.rstrip()
    if not value.endswith('"'):
        return False
    slash_count = 0
    for char in reversed(value[:-1]):
        if char != "\\":
            break
        slash_count += 1
    return slash_count % 2 == 0


def repair_text(text: str) -> tuple[str, int]:
    """Return TOML text with recoverable old multiline basic strings repaired."""
    lines = text.splitlines(keepends=True)
    repaired: list[str] = []
    changes = 0
    index = 0

    while index < len(lines):
        line = lines[index]
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        match = _ASSIGNMENT.match(body)
        if match is None or not match["value"].lstrip().startswith('"'):
            repaired.append(line)
            index += 1
            continue

        raw_value = match["value"].lstrip()
        try:
            tomllib.loads(f"value = {raw_value}")
        except tomllib.TOMLDecodeError:
            pass
        else:
            repaired.append(line)
            index += 1
            continue

        end_index: int | None = None
        for candidate in range(index + 1, len(lines)):
            candidate_body = lines[candidate].rstrip("\r\n")
            # A configuration assignment cannot be a continuation of the old
            # string. This prevents swallowing an unrelated later setting.
            if _ASSIGNMENT.match(candidate_body):
                break
            if _ends_with_unescaped_quote(candidate_body):
                end_index = candidate
                break

        if end_index is None:
            repaired.append(line)
            index += 1
            continue

        literal = "\n".join(
            [raw_value, *(item.rstrip("\r\n") for item in lines[index + 1 : end_index + 1])]
        )
        try:
            value = json.loads(literal.replace("\n", "\\n"))
        except json.JSONDecodeError:
            repaired.append(line)
            index += 1
            continue

        repaired.append(f'{match["prefix"]}{json.dumps(value, ensure_ascii=False)}{newline}')
        changes += 1
        index = end_index + 1

    return "".join(repaired), changes


def repair_file(path: Path) -> int:
    if not path.is_file():
        print(f"settings file not found: {path}", file=sys.stderr)
        return 1

    original = path.read_text(encoding="utf-8")
    repaired, changes = repair_text(original)
    if changes:
        backup = path.with_suffix(path.suffix + ".invalid-backup")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        path.write_text(repaired, encoding="utf-8")
        print(f"Repaired {changes} invalid multiline setting(s); backup: {backup}")

    try:
        tomllib.loads(repaired)
    except tomllib.TOMLDecodeError as exc:
        print(f"Invalid TOML in {path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    settings_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("settings.toml")
    raise SystemExit(repair_file(settings_path))
