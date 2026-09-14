#!/bin/sh
set -eu

/waku/.venv/bin/python /waku/scripts/repair_settings_toml.py /waku/settings.toml
exec uv run python -m waku
