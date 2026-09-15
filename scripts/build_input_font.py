"""Создаёт вариант KaiTi только с китайскими глифами для смешанного ввода."""

from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "fonts" / "KaiTi.ttf"
OUTPUT = ROOT / "assets" / "fonts" / "HanziLabKaiTiCJK.ttf"
FAMILY = "HanziLab KaiTi CJK"


def main() -> None:
    options = subset.Options()
    options.name_IDs = [0, 1, 2, 3, 4, 5, 6]
    options.name_legacy = True
    options.name_languages = [0x409]
    with subset.load_font(str(SOURCE), options) as font:
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(
            unicodes=set(range(0x3000, 0x3040))
            | set(range(0x3400, 0x4DC0))
            | set(range(0x4E00, 0xA000))
            | set(range(0xF900, 0xFB00))
        )
        subsetter.subset(font)

        for record in font["name"].names:
            if record.nameID in {1, 4}:
                record.string = FAMILY.encode(record.getEncoding())
            elif record.nameID == 2:
                record.string = "Regular".encode(record.getEncoding())
            elif record.nameID in {3, 6}:
                record.string = "HanziLabKaiTiCJK-Regular".encode(record.getEncoding())

        subset.save_font(font, str(OUTPUT), options)
    with TTFont(OUTPUT) as verified:
        codepoints = set().union(*(table.cmap.keys() for table in verified["cmap"].tables))
    assert ord("你") in codepoints
    assert ord("А") not in codepoints
    assert ord("A") not in codepoints
    print(f"Создан {OUTPUT.name}: {len(codepoints)} китайских символов")


if __name__ == "__main__":
    main()
