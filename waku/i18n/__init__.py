from pathlib import Path
from random import choice
from typing import Any

import yaml


class I18n:
    def __init__(
        self,
        locales_dir: Path = Path(__file__).parent / "locales",
        default_locale: str = "vi-VN",
    ):
        self.locales_dir = locales_dir
        self.default_locale = default_locale
        self.translations: dict[str, dict[str, Any]] = {}
        self.available_locales = set()

        self.load_translations()

    def load_translations(self):
        if not self.locales_dir.exists():
            raise FileNotFoundError(f"Translation directory '{self.locales_dir}' does not exist")

        for locale_dir in self.locales_dir.iterdir():
            if locale_dir.is_dir():
                locale_name = locale_dir.name
                self.available_locales.add(locale_name)
                self.translations[locale_name] = {}
                self._load_locale_files(locale_dir, locale_name)

    def _load_locale_files(self, locale_dir: Path, locale_name: str):
        yaml_files = list(locale_dir.glob("**/*.yaml")) + list(
            locale_dir.glob("**/*.yml")
        )

        for yaml_file in yaml_files:
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                    if content:
                        self._merge_translations(
                            self.translations[locale_name], content
                        )
            except Exception as e:
                print(f"Error loading file {yaml_file}: {e}")

    def _merge_translations(self, target: dict[str, Any], source: dict[str, Any]):
        for key, value in source.items():
            if (
                isinstance(value, dict)
                and key in target
                and isinstance(target[key], dict)
            ):
                self._merge_translations(target[key], value)
            else:
                target[key] = value

    def _get_nested_value(
        self, data: dict[str, Any], key: str
    ) -> str | list[str] | None:
        keys = key.split(".")
        current = data

        try:
            for k in keys:
                current = current[k]
            return current if current is not None else None  # type:ignore
        except (KeyError, TypeError):
            return None

    def _fallback_text(self, key: str) -> str:
        if key.startswith("bot.msg."):
            return "Đang xử lý..."
        if key.startswith("bot.button.") or key.startswith("bot.cmd."):
            return key.rsplit(".", 1)[-1].replace("_", " ").title()
        return key

    def _lookup_translation(self, key: str, locale: str) -> str | list[str] | None:
        translation = self._get_nested_value(self.translations[locale], key)
        if translation is not None:
            return translation
        for fallback_locale in (self.default_locale, "en", "zh-CN"):
            if fallback_locale == locale or fallback_locale not in self.translations:
                continue
            translation = self._get_nested_value(
                self.translations[fallback_locale], key
            )
            if translation is not None:
                return translation
        return None

    def t(self, key: str, locale: str = "") -> str:
        """
        Translate a key.

        Args:
            key: Translation key using dot notation for nested values.
            locale: Target locale.

        Returns:
            Translated string, or a fallback value if the key is missing.
        """
        if locale is None:
            locale = self.default_locale

        if locale not in self.translations:
            if self.default_locale in self.translations:
                locale = self.default_locale
            else:
                return key

        translation = self._lookup_translation(key, locale)

        return translation if translation is not None else self._fallback_text(key)  # type:ignore

    def trl(self, key: str, locale: str = "") -> str:
        """
        translate a list and return a random value from the list
        """
        if locale is None:
            locale = self.default_locale

        if locale not in self.translations:
            if self.default_locale in self.translations:
                locale = self.default_locale
            else:
                return key

        translation = self._lookup_translation(key, locale)

        if isinstance(translation, list):
            return choice(translation)
        return translation if translation is not None else self._fallback_text(key)

    def get_available_locales(self) -> list[str]:
        return list(self.available_locales)

    def set_default_locale(self, locale: str):
        if locale in self.available_locales:
            self.default_locale = locale
        else:
            print(f"Locale '{locale}' is not available")

    def reload(self):
        self.translations.clear()
        self.available_locales.clear()
        self.load_translations()


i18n = I18n()
t = i18n.t
trl = i18n.trl
