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
    # Arabic-Indic digits: visually identical, used interchangeably on the web
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
}

# Arabic yeh/kaf look identical to the Persian letters and are typed
# interchangeably; fold them so "شهريور" and "شهریور" compare equal.
# ZWNJ is dropped for the same reason.
LETTER_FOLD_MAP = {"ي": "ی", "ك": "ک", "‌": ""}

TIME_RE = re.compile(r"\d{1,2}[:٫]\d{2}")
# A day number (1–2 digits, never part of a longer number like a year)
# optionally followed by its month name.
DAY_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)\s*([^\s\d›>|،,]+)?")
# Same shape over raw text, so a date keeps the digits the user typed.
_D = r"\d۰-۹٠-٩"
RAW_DAY_MONTH_RE = re.compile(rf"(?<![{_D}])([{_D}]{{1,2}})(?![{_D}])\s*([^\s{_D}›>|،,]+)?")


def persian_to_english(text: str) -> str:
    """Converts Persian digits in a string to English digits."""
    if not text:
        return ""
    return "".join(PERSIAN_DIGITS_MAP.get(ch, ch) for ch in text)


def normalize_date(text: str) -> str:
    """Canonical form for comparing dates: ASCII digits, folded letters, single spaces."""
    folded = "".join(LETTER_FOLD_MAP.get(ch, ch) for ch in persian_to_english(text))
    return re.sub(r"\s+", " ", folded).strip()


def split_date_list(text: str) -> list:
    """Splits user-typed dates into individual date strings, digits as typed.

    Accepts '|', ',' and '،' separators and also bare runs such as
    "۵ شهریور ۶ شهریور ۷ شهریور", which are otherwise stored as one
    unmatchable blob.
    """
    dates = []
    for chunk in re.split(r"[|,،]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        matches = RAW_DAY_MONTH_RE.findall(chunk)
        if matches:
            dates.extend(f"{day} {month}".strip() for day, month in matches)
        else:
            dates.append(chunk)
    return dates


def parse_day_month(text: str) -> list:
    """Extracts (day:int, month or None) pairs from a date string.

    Times are stripped first so "› 19:00" is not read as a day. Day numbers
    are compared as integers, so the site's zero-padded "۰۴ شهریور" matches
    a hand-typed "4 شهریور" while "13 شهریور" still differs from "3 شهریور".
    """
    cleaned = TIME_RE.sub(" ", normalize_date(text))
    return [(int(m.group(1)), m.group(2) or None) for m in DAY_MONTH_RE.finditer(cleaned)]


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

    Matches on (day number, month name), so "4 شهریور" excludes the site's
    "چهارشنبه ۰۴ شهریور › ۱۹:۰۰" but "3 شهریور" never excludes "13 شهریور".
    An exclusion written as a bare day ("4") matches that day in any month.
    """
    session_pairs = parse_day_month(date_text)
    if not session_pairs:
        return False
    for excluded in excluded_dates:
        for ex_day, ex_month in parse_day_month(excluded):
            for day, month in session_pairs:
                if day == ex_day and (ex_month is None or month is None or ex_month == month):
                    return True
    return False
