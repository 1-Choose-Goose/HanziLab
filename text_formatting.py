from __future__ import annotations

import html
import re
import unicodedata
from functools import lru_cache
from itertools import groupby

from scripts.text_normalization import CJK_RE, PINYIN_VARIANT_RE, normalize_pinyin

_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_INVISIBLE_FORMATTING_RE = re.compile(r"[\u00ad\u200b\u2060\ufeff]")
_BRACKETED_ANNOTATION_RE = re.compile(r"\[([^\]\n]{1,120})]")
_SECTION_READING_RE = re.compile(
    r"^(?P<label>\s*•?\s*(?:[IVXLCDM]+|\d+\)))"
    r"[\s,.:;–—-]+(?P<reading>.+)$"
)


def normalize_display_text(
    text: object,
    *,
    preserve_line_breaks: bool = True,
) -> str:
    """Remove layout noise while preserving meaningful, non-empty lines."""
    source = "" if text is None else str(text)
    source = _INVISIBLE_FORMATTING_RE.sub("", source)
    # splitlines() also handles CR-only data and Unicode line separators.
    lines = [
        _HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip()
        for line in source.splitlines()
    ]
    non_empty_lines = [line for line in lines if line]
    separator = "\n" if preserve_line_breaks else " "
    return separator.join(non_empty_lines)


@lru_cache(maxsize=1)
def valid_pinyin_syllables() -> frozenset[str]:
    # Static inventory avoids loading pypinyin's multi-megabyte phrase model
    # on the UI thread when the first study card is shown.
    return frozenset(
        """a ai an ang ao ba bai ban bang bao bei ben beng bi bian biang biao bie
        bin bing bo bong bu ca cai can cang cao ce cei cen ceng cha chai chan chang
        chao che chen cheng chi chong chou chu chua chuai chuan chuang chui chun chuo
        ci cong cou cu cuan cui cun cuo da dai dan dang dao de dei den deng di dia dian
        diao die din ding diu dong dou du duan dui dun duo e ei en eng er fa fan fang
        fei fen feng fiao fo fou fu ga gai gan gang gao ge gei gen geng gong gou gu gua
        guai guan guang gui gun guo ha hai han hang hao he hei hen heng hm hng hong hou
        hu hua huai huan huang hui hun huo ji jia jian jiang jiao jie jin jing jiong jiu
        ju juan jue jun ka kai kan kang kao ke kei ken keng kong kou ku kua kuai kuan
        kuang kui kun kuo la lai lan lang lao le lei len leng li lia lian liang liao lie
        lin ling liu lo long lou lu luan lun luo lv lve m ma mai man mang mao me mei men
        meng mi mian miao mie min ming miu mo mou mu n na nai nan nang nao ne nei nen
        neng ng ni nia nian niang niao nie nin ning niu nong nou nu nuan nun nuo nv nve
        o ou pa pai pan pang pao pei pen peng pi pian piao pie pin ping po pou pu qi qia
        qian qiang qiao qie qin qing qiong qiu qu quan que qun ran rang rao re ren reng
        ri rong rou ru rua ruan rui run ruo sa sai san sang sao se sen seng sha shai shan
        shang shao she shei shen sheng shi shou shu shua shuai shuan shuang shui shun
        shuo si song sou su suan sui sun suo ta tai tan tang tao te tei teng ti tian
        tiao tie ting tong tou tu tuan tui tun tuo wa wai wan wang wei wen weng wo wong
        wu xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun ya yan yang
        yao ye yi yin ying yo yong you yu yuan yue yun za zai zan zang zao ze zei zen
        zeng zha zhai zhan zhang zhao zhe zhei zhen zheng zhi zhong zhou zhu zhua zhuai
        zhuan zhuang zhui zhun zhuo zi zong zou zu zuan zui zun zuo""".split()  # noqa: SIM905
    )


def _is_pinyin_word(value: str) -> bool:
    valid = valid_pinyin_syllables()
    if value in valid:
        return True
    reachable = {0}
    for start in range(len(value)):
        if start not in reachable:
            continue
        for end in range(start + 1, min(len(value), start + 7) + 1):
            syllable = value[start:end]
            # Standalone syllabic consonants must not make English words such
            # as "noun" look like the concatenation "nou" + "n".
            if syllable in {"m", "n", "ng", "hm", "hng"}:
                continue
            if syllable in valid or (syllable.endswith("r") and syllable[:-1] in valid):
                reachable.add(end)
    return len(value) in reachable


def _looks_like_pinyin_annotation(text: str, known_readings: set[str] | None) -> bool:
    """Validate readings; retain Latin labels and ambiguous unrelated words."""
    text = unicodedata.normalize("NFC", text.strip())
    has_lowercase_latin = False
    for character in text:
        if character.isalpha():
            if "LATIN" not in unicodedata.name(character, ""):
                return False
            has_lowercase_latin = has_lowercase_latin or character.islower()
        elif (
            character in "12345"
            or character.isspace()
            or character in "'\u2019·,;:/|.-–—()"
            or unicodedata.combining(character)
        ):
            continue
        else:
            return False
    # Uppercase A/B and Roman section numbers are grammatical labels, not
    # readings, and therefore stay visible.
    if not has_lowercase_latin:
        return False
    tokens = re.split(r"[\s'’·,;:/|.()–—-]+", text.lower().replace("u:", "v"))
    if not all(_is_pinyin_word(normalize_pinyin(token)) for token in tokens if token):
        return False
    has_tone = any(
        character in "12345\u0300\u0301\u0304\u030c"
        for character in unicodedata.normalize("NFD", text)
    )
    if known_readings is not None and not has_tone:
        return all(
            normalize_pinyin(part) in known_readings
            for part in PINYIN_VARIANT_RE.split(text)
        )
    return True


def card_translation_without_pinyin(text: object, pinyin: str | None = None) -> str:
    """Hide dictionary pronunciation annotations only on a card's Russian side."""
    cleaned_lines: list[str] = []
    known_readings = (
        {normalize_pinyin(part) for part in PINYIN_VARIANT_RE.split(pinyin)}
        if pinyin is not None else None
    )
    for raw_line in normalize_display_text(text).splitlines():
        line = _BRACKETED_ANNOTATION_RE.sub(
            lambda match: (
                "" if _looks_like_pinyin_annotation(match.group(1), known_readings) else match.group(0)
            ),
            raw_line,
        )
        line = normalize_display_text(line, preserve_line_breaks=False)
        section = _SECTION_READING_RE.fullmatch(line)
        if section and _looks_like_pinyin_annotation(section.group("reading"), known_readings):
            line = section.group("label").replace(" ", "")
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def is_cjk(character: str) -> bool:
    return CJK_RE.fullmatch(character) is not None


def mixed_script_html(
    text: str,
    chinese_family: str,
    chinese_size: int,
    text_family: str,
    text_size: int,
) -> str:
    """Форматирует CJK и остальной текст разными шрифтами без смены метрик."""
    text = normalize_display_text(text)
    parts: list[str] = []
    for chinese, characters in groupby(text, key=is_cjk):
        value = html.escape("".join(characters)).replace("\n", "<br>")
        family = chinese_family if chinese else text_family
        size = chinese_size if chinese else text_size
        parts.append(
            f'<span style="font-family:\'{html.escape(family)}\'; font-size:{size}pt;">'
            f"{value}</span>"
        )
    return "".join(parts)


def format_example_blocks(
    examples: list[dict],
    chinese_family: str,
    chinese_size: int,
    text_family: str,
    text_size: int = 12,
) -> str:
    """Render dictionary and study examples with consistent escaping and fonts."""
    blocks: list[str] = []
    for example in examples:
        lines: list[str] = []
        for field in ("chinese", "pinyin", "translation"):
            value = normalize_display_text(example.get(field))
            if value:
                lines.append(
                    mixed_script_html(
                        value, chinese_family, chinese_size, text_family, text_size
                    )
                )
        if lines:
            blocks.append("<br>".join(lines))
    return "<br>".join(blocks)
