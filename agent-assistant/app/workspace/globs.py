"""Small, deterministic, separator-aware glob matcher."""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1024)
def glob_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.casefold()
    index = 0
    pieces = ["^"]
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    pieces.append("(?:.*/)?")
                    index += 1
                else:
                    pieces.append(".*")
                continue
            pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        elif char == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                pieces.append(r"\[")
            else:
                content = pattern[index + 1 : closing]
                if content.startswith("!"):
                    content = "^" + content[1:]
                pieces.append("[" + content.replace("\\", r"\\") + "]")
                index = closing
        else:
            pieces.append(re.escape(char))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    folded = path.casefold()
    return any(glob_regex(pattern).fullmatch(folded) is not None for pattern in patterns)

