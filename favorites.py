# ==========================================
# FAVORITES FILE LOGIC (shared)
# ==========================================
# Shared between app.py (hourly watcher) and manage_favorites.py (Telegram
# command bot). Stdlib-only on purpose: the bot workflow installs nothing
# but `requests`, and importing app.py would pull in bs4/google-genai and
# exit when BOT_TOKEN is unset.
#
# favorite_shows.txt format, one show per line:
#     slug | excluded date | excluded date ...
# Dates are free-form Persian day+month strings ("8 شهریور"), Persian or
# ASCII digits. Lines starting with '#' are comments: never crawled, and
# preserved verbatim when the bot rewrites the file.

import os
import re

FAVORITES_FILE = "favorite_shows.txt"

PERSIAN_DIGITS_MAP = {
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
}


def persian_to_english(text: str) -> str:
    """Converts Persian digits in a string to English digits."""
    if not text:
        return ""
    return "".join(PERSIAN_DIGITS_MAP.get(ch, ch) for ch in text)


def normalize_date(text: str) -> str:
    """Canonical form for comparing exclusion dates: ASCII digits, single spaces."""
    return re.sub(r"\s+", " ", persian_to_english(text)).strip()


def load_entries(path: str = FAVORITES_FILE) -> list:
    """Parses the favorites file into an ordered list of entries.

    Each entry is ("comment", raw_line) for blank/'#' lines (kept verbatim)
    or ("show", slug, [excluded_date, ...]). Missing file -> [].
    """
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8") as f:
        for raw in f.read().splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                entries.append(("comment", raw))
                continue
            parts = [p.strip() for p in raw.split("|") if p.strip()]
            entries.append(("show", parts[0], parts[1:]))
    return entries


def save_entries(entries: list, path: str = FAVORITES_FILE) -> None:
    """Writes entries back in order: comments verbatim, shows re-serialized."""
    lines = []
    for entry in entries:
        if entry[0] == "comment":
            lines.append(entry[1])
        else:
            _, slug, dates = entry
            lines.append(" | ".join([slug] + list(dates)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")


def load_favorites(path: str = FAVORITES_FILE) -> dict:
    """Returns {slug: [excluded_date, ...]} for active (non-comment) shows."""
    favorites = {}
    for entry in load_entries(path):
        if entry[0] != "show":
            continue
        _, slug, dates = entry
        if slug in favorites:
            print(f"Warning: duplicate favorite '{slug}' in {path} — later line wins.")
        favorites[slug] = dates
    return favorites


def session_excluded(date_text: str, excluded_dates: list) -> bool:
    """True if the session's date matches one of the excluded dates.

    Digit-boundary match so "3 شهریور" does not also exclude "13 شهریور"
    or "23 شهریور"; digits normalized so Persian and ASCII forms compare equal.
    """
    normalized_text = normalize_date(date_text)
    for excluded in excluded_dates:
        needle = normalize_date(excluded)
        if needle and re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", normalized_text):
            return True
    return False
