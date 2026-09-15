"""Convert Make Me a Hanzi dictionary.txt into the offline IDS lookup.

Source and licensing: assets/cursive/decomposition/README.txt.
"""

import argparse
import json
from pathlib import Path


def convert(source: Path, output: Path) -> None:
    rows = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    data = {row["character"]: row["decomposition"] for row in rows}
    output.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    convert(args.source, args.output)
