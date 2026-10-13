"""Pure presentation helpers (also usable in headless tests)."""
from datetime import datetime


def format_size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def format_time(value: int) -> str:
    if not value:
        return "—"
    # Store's current contract uses epoch milliseconds. Accept higher-resolution
    # diagnostic timestamps as well; never pass milliseconds to fromtimestamp.
    if value > 100_000_000_000_000_000:
        seconds = value / 1_000_000_000
    elif value > 100_000_000_000_000:
        seconds = value / 1_000_000
    elif value > 100_000_000_000:
        seconds = value / 1_000
    else:
        seconds = value
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def highlight_ranges(text: str, query: str) -> list[tuple[int, int]]:
    """Scintilla offsets are UTF-8 bytes; regex gives original character spans."""
    import re
    if not query:
        return []
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    return [(offsets[m.start()], offsets[m.end()] - offsets[m.start()])
            for m in re.finditer(re.escape(query), text, re.IGNORECASE)]


def parse_rules(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.replace("，", ",")
                              .replace(";", ",").split(",") if part.strip()))
