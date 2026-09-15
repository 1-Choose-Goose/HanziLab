from __future__ import annotations

import re
import unicodedata

CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002ee5f\U0002f800-\U0002fa1f\U00030000-\U000323af]"
)
PINYIN_VARIANT_RE = re.compile(r"[;,/|、]")


def normalize_pinyin(value: str) -> str:
    value = unicodedata.normalize("NFD", value.casefold().replace("u:", "v"))
    # Тоновые ǖ/ǘ/ǚ/ǜ и раздельно набранное ü сначала раскладываются
    # в u + диерезис + тон. Диерезис отличает гласную, а не обозначает тон.
    value = value.replace("u\u0308", "v")
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    # Цифры 1–5 — альтернативная запись тонов. Для поиска они, как и диакритика,
    # не должны менять результат.
    return re.sub(r"[^a-zv]", "", value)


def normalized_pinyin_variants(value: str) -> str:
    """Индексировать чтения раздельно, без ложных совпадений на их стыке."""
    return " ".join(
        normalized
        for part in PINYIN_VARIANT_RE.split(value)
        if (normalized := normalize_pinyin(part))
    )


def normalize_translation(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"[^\w\s-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()
