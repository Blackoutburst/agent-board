"""Insert the board location only into the existing private files HTTPS vhost."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

BEGIN = "    # collaboration-board: begin"
END = "    # collaboration-board: end"


def add_board_location(source: str, location: str) -> str:
    if source.count(BEGIN) or source.count(END):
        if source.count(BEGIN) != 1 or source.count(END) != 1:
            raise ValueError("Ambiguous existing board markers; inspect nginx manually")
        start = source.index(BEGIN)
        finish = source.index(END, start) + len(END)
        if source[finish:finish + 1] == "\n":
            finish += 1
        if source[finish:finish + 1] == "\n":
            finish += 1
        source = source[:start] + source[finish:]
    # Existing files vhost uses plain server blocks. Strip comments for brace scanning.
    matches = []
    for match in re.finditer(r"(?m)^server\s*\{", source):
        depth = 1
        pos = match.end()
        while depth and pos < len(source):
            if source[pos] == "#":
                newline = source.find("\n", pos)
                pos = len(source) if newline < 0 else newline
                continue
            if source[pos] == "{":
                depth += 1
            elif source[pos] == "}":
                depth -= 1
            pos += 1
        if depth:
            raise ValueError("Unbalanced server block")
        block = source[match.start():pos]
        if (re.search(r"\bserver_name\s+files\.blackoutburst\.com\s*;", block)
                and re.search(r"\blisten\s+443\s+ssl\b", block)):
            matches.append((match.start(), pos, block))
    if len(matches) != 1:
        raise ValueError("Expected exactly one existing files.blackoutburst.com HTTPS vhost")
    start, end, block = matches[0]
    if "include /etc/nginx/blackoutburst-platform/admin.conf;" not in block:
        raise ValueError("Existing private platform auth include is missing; inspect nginx manually")
    if re.search(r"\blocation\s+(?:=\s+|\^~\s+)?/board(?:/|\s)", block):
        raise ValueError("An unmanaged /board location already exists; preserve and inspect it")
    anchor = re.search(r"(?m)^[ \t]*location\s+/\s*\{", block)
    if anchor is None:
        raise ValueError("Existing File Browser root location was not found")
    insertion = start + anchor.start()
    return source[:insertion] + location.rstrip() + "\n\n" + source[insertion:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--location", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a fresh candidate file")
    rendered = add_board_location(args.site.read_text(), args.location.read_text())
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
