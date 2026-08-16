from __future__ import annotations

import re
import unicodedata


def normalize_pinyin(value: str) -> str:
    value = value.casefold().replace("u:", "v").replace("ü", "v")
    value = unicodedata.normalize("NFD", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    # Цифры 1–5 — альтернативная запись тонов. Для поиска они, как и диакритика,
    # не должны менять результат.
    return re.sub(r"[^a-zv]", "", value)


def normalize_translation(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"[^\w\s-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()
