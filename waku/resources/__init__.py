from pathlib import Path

import orjson

from waku.logger import logger

_word_dict_cache: dict[str, dict[str, list[str]]] = {}


def _load_words(locale: str) -> dict[str, list[str]]:
    """Load the legacy word-reply dictionary for one locale."""
    internal_path = Path(__file__).parent / "word_dicts"
    locale_path = internal_path / f"{locale}.json"
    language_path = internal_path / f"{locale.split('-', 1)[0]}.json"
    if locale_path.is_file():
        files = [locale_path]
    elif language_path.is_file():
        files = [language_path]
    elif locale.lower().startswith("zh"):
        files = [internal_path / "data.json"]
    else:
        files = []

    words: dict[str, list[str]] = {}
    logger.debug(f"loading word dict for locale {locale} from {files}")
    for file in files:
        try:
            with file.open(encoding="utf-8") as f:
                for keyword, replies in orjson.loads(f.read()).items():
                    if keyword in words:
                        words[keyword].extend(replies)
                    else:
                        words[keyword] = replies
        except Exception as e:
            logger.error(
                f"loading word dict failed: {file}: {e.__class__.__name__}: {e}"
            )
    return words


def get_word_dict(locale: str = "zh-CN") -> dict[str, list[str]]:
    """Return a lazily loaded, locale-specific word-reply dictionary."""
    if locale not in _word_dict_cache:
        _word_dict_cache[locale] = _load_words(locale)
    return _word_dict_cache[locale]
