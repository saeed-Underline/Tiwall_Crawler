import os
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from reportlab.lib.pagesizes import A3
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors

import arabic_reshaper
from bidi.algorithm import get_display
import re
import time
from datetime import datetime, timedelta



BOT_TOKEN = "8180945977:AAHIqAUWn4a0gtKC4Liv2lvYNN6D45rUCdE"
CHAT_ID = "-1003358233998"   # Your own Telegram user ID or group ID
CHAT_ID_2 = "-1003253814794"  # Another group ID for testing
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

BASE_URL = "https://www.tiwall.com"
SHOWCASE_URL = "https://www.tiwall.com/showcase?filters=city:2111,s:theater,available:true&order=rating"

URL_RE = re.compile(r"(https?://\S+)")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
})

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TiwallScraper/1.0)",
}

PERSIAN_DIGITS = {
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
}

def persian_to_english(s: str) -> str:
    if not s:
        return ""
    out = []
    for ch in s:
        if ch in PERSIAN_DIGITS:
            out.append(PERSIAN_DIGITS[ch])
        else:
            out.append(ch)
    return "".join(out)

def persian_to_int(s: str) -> Optional[int]:
    s_en = persian_to_english(s)
    s_en = "".join(c for c in s_en if c.isdigit())
    return int(s_en) if s_en else None

def has_persian(text: str) -> bool:
    # Arabic/Persian Unicode range
    return bool(re.search(r"[\u0600-\u06FF]", text))

def fetch_showcase_html() -> str:
    resp = SESSION.get(SHOWCASE_URL, timeout=20)
    resp.raise_for_status()
    return resp.text

def parse_showcase(html: str) -> List[Dict[str, Any]]:
    """
    Parse the showcase page and return a list of shows.

    Each show dict:
        {
          "title": str,
          "rating": float | None,
          "votes": int | None,
          "page_url": str,   # /p/...
          "sale_url": str,   # /s/...
          "slug": str,       # e.g. "ghalbenarengi3"
        }
    """
    soup = BeautifulSoup(html, "html.parser")
    # 🔥 Remove archived section if present
    archived = soup.find("div", class_="archived-pages")
    if archived:
        archived.decompose()   # completely removes it from the DOM
    cards = soup.select("a.item-page")

    shows: List[Dict[str, Any]] = []

    for card in cards:
        href = card.get("href", "")
        page_url = urljoin(BASE_URL, href)

        info = card.select_one("div.info")
        if not info:
            continue

        # Title
        title = ""
        h2 = info.select_one("h2")
        if h2:
            small = h2.select_one("span.normal.small")
            if small:
                small.extract()
            title = h2.get_text(strip=True)

        # Rating + votes
        rating = None
        votes = None
        rating_span = info.select_one("span.avg-rating")
        if rating_span:
            txt = rating_span.get_text(strip=True)
            if "★" in txt:
                before, after = [p.strip() for p in txt.split("★", 1)]
                votes = persian_to_int(before)
                # rating in Persian like "۴٫۷"
                after = after.replace("٫", ".")
                try:
                    rating = float(persian_to_english(after))
                except ValueError:
                    rating = None

        # Sale URL (button)
        sale_btn = card.select_one("span.btn.tmp-label")
        sale_urn = sale_btn.get("data-saleurn") if sale_btn else None
        sale_url = urljoin(BASE_URL, sale_urn) if sale_urn else None

        slug = None
        if sale_urn:
            slug = sale_urn.rstrip("/").split("/")[-1]

        shows.append({
            "title": title,
            "rating": rating,
            "votes": votes,
            "page_url": page_url,
            "sale_url": sale_url,
            "slug": slug,
        })

    return shows

def fetch_show_page(sale_url: str) -> str:
    resp = SESSION.get(sale_url, timeout=20)
    resp.raise_for_status()
    return resp.text

def parse_sessions(html: str) -> List[Dict[str, Any]]:
    """
    Parse the sessions (dates/times/status) from a /s/<slug> page.

    Logic:
      - Find <div id="showtimeMenu">
      - For each <a data-id="..."> inside it:
          * data-id = instance_id
          * text = row_text (we extract date, time, status from this)
    """
    soup = BeautifulSoup(html, "html.parser")

    menu = soup.find("div", id="showtimeMenu") or soup.find("div", class_="showtimeMenu")
    if not menu:
        return []

    sessions: List[Dict[str, Any]] = []

    days_fa = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
    time_pattern = re.compile(r"[0-9۰-۹]{1,2}[:٫][0-9۰-۹]{2}")

    # 🔥 This is the key: every <a> with data-id is a session
    anchor_tags = menu.find_all("a", attrs={"data-id": True})

    for a in anchor_tags:
        row_text = a.get_text(" ", strip=True)
        if not row_text:
            continue

        # ---- instance_id from data-id ----
        data_id = a.get("data-id")
        if not data_id or not data_id.isdigit():
            continue
        instance_id = int(data_id)

        # ---- time ----
        time_match = time_pattern.search(row_text)
        if time_match:
            time_raw = time_match.group(0).replace("٫", ":")
            time_text = persian_to_english(time_raw)
        else:
            time_text = None

        # ---- status ----
        status_text = None
        sold_out = False
        extra_capacity = False

        text = row_text

        # SOLD OUT: "پُر شد" or "پر شد"
        if re.search(r"پُر\s*شد", text) or re.search(r"پر\s*شد", text):
            status_text = "پُر شد"
            sold_out = True

        # EXTRA CAPACITY: "بیرون از ظرفیت"
        elif re.search(r"بیرون\s+از\s+ظرفیت", text):
            status_text = "Extra Capacity"
            extra_capacity = True

        # AVAILABLE: "مانده دارد" or "مانده: ۷۶ بلیت/بلیط"
        else:
            pattern = r"(مانده\s+دارد|مانده[:\s]+[0-9۰-۹]+\s*(?:بلیط|بلیت))"
            m = re.search(pattern, text)
            if m:
                status_text = m.group(0)

        # ---- date (heuristic around weekday name) ----
        date_text = None
        tokens = row_text.split()
        for i, tok in enumerate(tokens):
            if any(day in tok for day in days_fa):
                date_text = " ".join(tokens[i:i+5])
                break

        sessions.append({
            "raw_text": row_text,
            "date_text": date_text,
            "time_text": time_text,
            "status_text": status_text,
            "sold_out": sold_out,
            "extra_capacity": extra_capacity,
            "instance_id": instance_id,
        })

    return sessions

def has_available_front_rows(seats, front_rows=(1, 2)) -> bool:
    """
    Return True if there is at least one free seat in any of the given rows.
    front_rows: tuple of row numbers (e.g. (1, 2)).
    """
    front_rows_set = set(front_rows)
    for seat in seats:
        if seat.get("status") == "free" and seat.get("row") in front_rows_set:
            return True
    return False

def parse_tiwall_seats_from_html(html: str) -> List[Dict[str, Any]]:
    """
    Parse the seatmap geometry in a /s/<slug> page HTML.

    We *do not* decide sold/free here; we just extract which seats exist.

    Each seat:
      {
        "zone": "A",
        "row": <int>,
        "number": <int>,
        "code": "A-4-15"
      }
    """
    soup = BeautifulSoup(html, "html.parser")

    seats: List[Dict[str, Any]] = []

    # We assume each row looks like: <div id="zbsm-row-7" class="row">...</div>
    row_divs = soup.find_all("div", id=lambda x: x and x.startswith("zbsm-row-"))

    for row_div in row_divs:
        row_label: Optional[Any] = None

        # row id pattern
        m = re.search(r'^zbsm-row-([A-Za-z0-9]+)$', row_div["id"])
        if m:
            raw = m.group(1)
            as_int = persian_to_int(raw)
            row_label = as_int if as_int is not None else raw

        # or from <div class="row-head">۷</div>
        head = row_div.find("div", class_="row-head")
        if head:
            head_txt = head.get_text(strip=True)
            maybe = persian_to_int(head_txt)
            row_label = maybe if maybe is not None else head_txt

        if row_label is None:
            continue

        # Each seat is a <div class="chair ...">
        for chair in row_div.find_all("div", class_="chair"):
            inp = chair.find("input", {"name": "chair"})
            if not inp:
                continue

            base_id = inp.get("data-base-id") or inp.get("value", "")
            zone: Optional[str] = None
            seat_number: Optional[int] = None
            row_from_id: Optional[int] = None

            # data-base-id format e.g. "A-7-16" (ZONE-ROW-SEAT)
            m2 = re.match(r"([A-Z])-([A-Za-z0-9۰-۹]+)-([0-9۰-۹]+)", base_id)
            if m2:
                zone = m2.group(1)

                row_raw = m2.group(2)
                row_as_int = persian_to_int(row_raw)
                row_from_id = row_as_int if row_as_int is not None else row_raw  # keep "A"/"B" etc.

                seat_number = persian_to_int(m2.group(3))

            # fallback if something missing
            if seat_number is None:
                seat_txt = chair.get_text(strip=True)
                seat_number = persian_to_int(seat_txt)

            if row_from_id is not None:
                row_label = row_from_id

            if zone is None or seat_number is None or row_label is None:
                continue

            code = f"{zone}-{row_label}-{seat_number}"

            seats.append({
                "zone": zone,
                "row": row_label,
                "number": seat_number,
                "code": code,
            })

    return seats

def fetch_seatmap_json(slug: str, instance_id: int, sale_url: str) -> Dict[str, Any]:
    """
    Call Tiwall internal seatmap API and return JSON.

    We MUST send:
      - cookies from the session (SESSION)
      - Origin + Referer like a browser XHR
      - X-Requested-With header
    """
    url = f"{BASE_URL}/api/v1/internal/general/seatmapState"
    params = {
        "instance_id": instance_id,
        "init": 2,
        "urn": slug,
    }

    headers = {
        # pretend to be the browser’s XHR
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": sale_url,  # e.g. https://www.tiwall.com/s/ghalbenarengi3
    }

    resp = SESSION.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()

def parse_price_map(price_str: str) -> Dict[tuple, int]:
    """
    Convert price string like:
      'A-1:3-*=350000,A-4:7-*=300000'
    or:
      'A/B-1:4-*-*=400000,A/B-5:7-*-*=350000,A/B-8:9-*-*=300000'

    into a mapping:
      {('A', 1): 400000, ('B', 1): 400000, ...}

    Key is (zone, row) so multiple zones per row range are supported.
    """
    mapping: Dict[tuple, int] = {}
    if not price_str:
        return mapping

    parts = price_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue

        if "=" not in part:
            continue
        left, right = part.split("=", 1)

        try:
            price = int(right)
        except ValueError:
            continue

        # left examples:
        #   "A-1:3-*"
        #   "A-4:7-*"
        #   "A/B-1:4-*-*"
        #   "A/B-5:7-*-*"
        #
        # We care about:
        #   zones  = "A" or "A/B" or "A/B/C"
        #   rows   = 1:3 or 5:7 etc.
        m = re.match(r"([A-Z](?:/[A-Z])*)-([0-9]+):([0-9]+)-", left)
        if not m:
            continue

        zones_str, row_start_str, row_end_str = m.groups()
        row_start = int(row_start_str)
        row_end = int(row_end_str)

        zones = zones_str.split("/")  # e.g. "A/B" -> ["A", "B"]

        for zone in zones:
            for row in range(row_start, row_end + 1):
                mapping[(zone, row)] = price

    return mapping

def parse_locks(locks_str: str) -> set:
    """
    Convert locks string like:
      'A-1-5:8=r,B-1-9:12=r'
    into a set of locked seat codes:
      {'A-1-5', 'A-1-6', 'A-1-7', 'A-1-8',
       'B-1-9', 'B-1-10', 'B-1-11', 'B-1-12'}
    """
    locked = set()
    if not locks_str:
        return locked

    parts = locks_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # split "A-1-5:8=r" into "A-1-5:8" and "r"
        if "=" not in part:
            continue
        left, _flag = part.split("=", 1)

        # left patterns like "A-1-5:8"
        m = re.match(r"([A-Z])-([0-9]+)-([0-9]+):([0-9]+)", left)
        if not m:
            continue

        zone = m.group(1)
        row = int(m.group(2))
        seat_start = int(m.group(3))
        seat_end = int(m.group(4))

        for seat in range(seat_start, seat_end + 1):
            code = f"{zone}-{row}-{seat}"
            locked.add(code)

    return locked

def parse_sold_set(state_list: List[Dict[str, Any]]) -> set:
    """
    Build a set of seat codes that are sold from the API 'state' list.
    Each item: {"c": "A-1-1", "s": 1}
    """
    sold = set()
    for item in state_list:
        code = item.get("c")
        code = code.replace('همکف', 'A')
        status_flag = item.get("s")
        if code and status_flag == 1:
            sold.add(code)
    return sold

def merge_seats_with_state_and_price(
    geometry: List[Dict[str, Any]],
    json_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    data = json_data.get("data", {})
    price_str = data.get("price", "")
    state_list = data.get("state", [])
    locks_str = data.get("locks", "")

    price_map = parse_price_map(price_str)
    sold_set = parse_sold_set(state_list)
    locked_set = parse_locks(locks_str)

    seats: List[Dict[str, Any]] = []
    for g in geometry:
        zone = g["zone"]
        row = g["row"]
        number = g["number"]
        code = g["code"]

        # 🔽 priority: locked > sold > free
        if code in locked_set:
            status = "locked"
        elif code in sold_set:
            status = "sold"
        else:
            status = "free"

        price = price_map.get((zone, row))

        seats.append({
            "zone": zone,
            "row": row,
            "number": number,
            "code": code,
            "status": status,
            "price": price,
        })

    return seats

def build_seat_map(seats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group seats per row, sorted by row and seat number.

    Output:
      [
        {
          "row_label": 1,
          "seats": [
             {"number": 5, "status": "free"},
             ...
          ]
        },
        ...
      ]
    """
    rows_dict: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)

    for seat in seats:
        rows_dict[seat["row"]].append({
            "number": seat["number"],
            "status": seat["status"],
            "price": seat.get("price"),
        })

    seat_map: List[Dict[str, Any]] = []
    for row_label in sorted(rows_dict, key=lambda x: x):
        seat_list = sorted(rows_dict[row_label], key=lambda s: s["number"])
        seat_map.append({
            "row_label": row_label,
            "seats": seat_list,
        })

    return seat_map

def render_text_map(
    seat_map: List[Dict[str, Any]],
    free_status=("free", "available"),
    sold_status=("sold", "reserved"),
    locked_status=("locked",),
) -> str:
    lines: List[str] = []
    lines.append("Stage")
    lines.append("=" * 20)
    lines.append("")

    for row in seat_map:
        label = row["row_label"]
        chars = []
        for seat in row["seats"]:
            st = str(seat["status"]).lower()
            if st in free_status:
                ch = "A"
            elif st in sold_status:
                ch = "X"
            elif st in locked_status:
                ch = "L"
            else:
                ch = "?"
            chars.append(ch)
        lines.append(f"Row {label:>2}: {''.join(chars)}")

    lines.append("")
    lines.append("Legend: A = available, X = sold, L = locked, ? = other")
    return "\n".join(lines)

def scrape_show(sale_url: str) -> Dict[str, Any]:
    """
    For a single show (/s/<slug>):

    - Fetch page
    - Parse sessions list (with instance_id)
    - Parse seat geometry (HTML) once
    - For each session that has an instance_id and is not sold out:
        * Call seatmapState API
        * Merge JSON state + price with geometry
        * Build a text seat map
    """
    html = fetch_show_page(sale_url)
    sessions = parse_sessions(html)
    geometry = parse_tiwall_seats_from_html(html)

    # derive slug from URL
    path = urlparse(sale_url).path
    slug = path.rstrip("/").split("/")[-1]

    # Build seatmaps per session
    for sess in sessions:
        inst_id = sess.get("instance_id")
        if not inst_id or sess.get("sold_out"):
            sess["seat_text_map"] = None
            sess["seats"] = []
            sess["has_front_row_free"] = False
            continue

        try:
            json_data = json_data = fetch_seatmap_json(slug, inst_id, sale_url)
            seats = merge_seats_with_state_and_price(geometry, json_data)
            seat_map = build_seat_map(seats)
            sess["seat_text_map"] = render_text_map(seat_map)
            sess["seats"] = seats
            # 🔥 mark if this session has available seats in row 1 or 2 or 3
            sess["has_front_row_free"] = has_available_front_rows(seats, front_rows=(1, 2, 'A', 'B'))
        except Exception as e:
            sess["seat_text_map"] = f"Error fetching seatmap: {e}"
            sess["seats"] = []
            sess["has_front_row_free"] = False

    # For backward compatibility: pick a default text_map (first with seat_text_map)
    default_text_map = None
    for sess in sessions:
        if sess.get("seat_text_map"):
            default_text_map = sess["seat_text_map"]
            break
    if default_text_map is None:
        default_text_map = "No seatmap available for any session."

    return {
        "sale_url": sale_url,
        "slug": slug,
        "sessions": sessions,
        "geometry": geometry,
        "text_map": default_text_map,
    }

def compute_bayesian_scores(shows, m: int = 100):
    """
    Adds a 'score' field to each show dict using a Bayesian weighted rating.
    m = minimum votes to be fully trusted. Adjust to your taste (e.g. 20, 30).
    """
    # 1) Compute C: average rating over all shows that have both rating and votes
    rated_shows = [s for s in shows if s.get("rating") is not None and s.get("votes")]
    if not rated_shows:
        return shows  # nothing to do

    C = sum(s["rating"] for s in rated_shows) / len(rated_shows)

    for s in shows:
        R = s.get("rating")
        v = s.get("votes") or 0

        if R is None or v < m:
            s["score"] = 0  # or C, but 0 makes them sink
            continue

        score = (v / (v + m)) * R + (m / (v + m)) * C
        s["score"] = score

    return shows

def has_persian(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))

def create_persian_report_pdf(report_text: str,
                              filename: str = "tiwall_report.pdf",
                              font_path: str = "Vazirmatn-Regular.ttf") -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    font_full_path = os.path.join(base_dir, font_path)

    if not os.path.exists(font_full_path):
        raise FileNotFoundError(f"Font file not found: {font_full_path}")

    pdfmetrics.registerFont(TTFont("Vazir", font_full_path))

    doc = SimpleDocTemplate(
        filename,
        pagesize=A3,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40,
    )

    styles = getSampleStyleSheet()

    # Title / section header (Persian)
    style_title = ParagraphStyle(
        "TitleFA",
        parent=styles["Heading1"],
        fontName="Vazir",
        fontSize=24,
        leading=22,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#1f4e79"),
        backColor=colors.HexColor("#e6f2ff"),
        spaceBefore=6,
        spaceAfter=12,
        borderPadding=4,
    )

    # Normal Persian text
    style_fa = ParagraphStyle(
        "Persian",
        parent=styles["Normal"],
        fontName="Vazir",
        fontSize=18,
        leading=16,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#222222"),
    )

    # Latin / ASCII (for seat maps, debug info)
    style_en = ParagraphStyle(
        "Latin",
        parent=styles["Normal"],
        fontName="Vazir",      # or "Helvetica"
        fontSize=10,
        leading=14,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#333333"),
    )

    # Links (URLs)
    style_link = ParagraphStyle(
        "Link",
        parent=style_en,
        textColor=colors.HexColor("#1a73e8"),
        underline=True,
    )

    # Seatmap style: monospaced feel + light background
    style_seatmap = ParagraphStyle(
        "Seatmap",
        parent=style_en,
        fontName="Courier",   # built-in monospace font
        backColor=colors.HexColor("#f5f5f5"),
        leading=12,
    )

    flow = []

    for raw_line in report_text.splitlines():
        line = raw_line.rstrip()

        # empty line => vertical space
        if not line:
            flow.append(Spacer(1, 6))
            continue

        # Detect "title" lines by a simple convention (you can tweak this):
        # e.g. lines starting with "Show:" or "نمایش:"
        is_title = line.startswith("Show:") or line.startswith("نمایش:")

        # Seatmap lines: e.g. starting with "Row" or "صف"
        is_seatmap = line.strip().startswith("Row") or line.strip().startswith("Stage")

        # URL line?
        url_match = URL_RE.search(line)

        if has_persian(line) and not is_seatmap:
            # Persian text (title or body)
            reshaped = arabic_reshaper.reshape(line)
            bidi_line = get_display(reshaped)

            style = style_title if is_title else style_fa
            flow.append(Paragraph(bidi_line, style))

        else:
            # Non-Persian: seat maps or URLs or misc
            if is_seatmap:
                safe = line.replace("<", "&lt;").replace(">", "&gt;")
                flow.append(Paragraph(safe, style_seatmap))
            elif url_match:
                # clickable link: wrap URL in <link>
                url = url_match.group(1)
                safe_line = line.replace("<", "&lt;").replace(">", "&gt;")
                linked = safe_line.replace(
                    url, f'<link href="{url}">{url}</link>'
                )
                flow.append(Paragraph(linked, style_link))
            else:
                safe = line.replace("<", "&lt;").replace(">", "&gt;")
                flow.append(Paragraph(safe, style_en))

    doc.build(flow)

def main():
    # 1) Get shows from showcase
    showcase_html = fetch_showcase_html()
    shows = parse_showcase(showcase_html)
    shows = compute_bayesian_scores(shows, m=100)
    shows.sort(key=lambda s: s["score"], reverse=True)
    shows = shows[:10]

    summary_text = build_front_row_summary(shows)
    # instead of printing directly, collect lines
    lines: List[str] = []
    lines.append("Top Tiwall Shows (by Bayesian score)")
    lines.append("=" * 60)
    lines.append("")

    for show in shows:
        if not show["sale_url"]:
            continue

        lines.append("=" * 60)
        lines.append(f"Show: {show['title']}")
        lines.append(f"Rating: {show['rating']}  Votes: {show['votes']}  Score: {show.get('score'):.3f}")
        lines.append(f"Sale URL: {show['sale_url']}")
        lines.append("-" * 60)

        try:
            data = scrape_show(show["sale_url"])
        except Exception as e:
            lines.append(f"Error scraping show: {e}")
            lines.append("")
            continue


        if not any(s.get("has_front_row_free") for s in data["sessions"]):
            lines.append("  ❌ No sessions with free seats in row 1 or 2.")
        # Sessions summary + seat maps
        lines.append("Sessions:")
        for sess in data["sessions"]:
            if not sess.get("has_front_row_free"):
                continue  # skip sessions without front-row free seats
            status = "SOLD OUT" if sess["sold_out"] else (sess["status_text"] or "")
            lines.append(
                f"  {sess['date_text']} "
            )

            if sess.get("seat_text_map"):
                lines.append("")
                lines.append("  Seat map for this session:")
                # indent seat map a bit
                for seat_line in sess["seat_text_map"].splitlines():
                    lines.append("    " + seat_line)
                lines.append("")

        # Default seat map
        # lines.append("")
        # lines.append("Default seat map (first non-sold-out session, if any):")
        # lines.append("")
        # for seat_line in data["text_map"].splitlines():
        #     lines.append("  " + seat_line)
        # lines.append("")
        # lines.append("")

    # join everything and write PDF
    report_text = "\n".join(lines)
    # save_report_pdf(report_text, "tiwall_report.pdf")
    # optional: also print where it was saved
    create_persian_report_pdf(report_text, "tiwall_report.pdf", font_path="Vazirmatn-Regular.ttf")
    print("PDF report written to tiwall_report.pdf")
    send_pdf_to_telegram("tiwall_report.pdf", caption=summary_text)

def run_every_hour_at(minute=2):
    while True:
        now = datetime.now()
        next_run = now.replace(minute=minute, second=0, microsecond=0)

        # If the time has already passed for this hour, schedule next hour
        if next_run <= now:
            next_run += timedelta(hours=1)

        wait_seconds = (next_run - now).total_seconds()
        print(f"Next run at {next_run}. Sleeping {int(wait_seconds)}s...")

        time.sleep(wait_seconds)

        # Run your job
        main()

def build_front_row_summary(shows) -> str:
    """
    For each show, count how many sessions have free seats in row 1 or 2,
    and build a short summary text for Telegram.
    """
    lines = []
    lines.append("🎭 Shows with front-row availability:")
    lines.append("")
    
    for show in shows:
        sale_url = show.get("sale_url")
        if not sale_url:
            continue

        try:
            data = scrape_show(sale_url)
        except Exception as e:
            # optional: log error, but skip this show in summary
            continue

        sessions = data.get("sessions", [])
        count_front = sum(
            1 for s in sessions
            if s.get("has_front_row_free")
        )

        # if count_front == 0:
            # continue  # skip shows with no good sessions

        lines.append(f"show: {show['title']}")
        lines.append(f"link: {sale_url}")
        lines.append(f"Session: {count_front}")
        lines.append(f"Score: {show['score']:.3f}")
        lines.append("")  # blank line between shows

    return "\n".join(lines) if len(lines) > 2 else "No shows with free seats in row 1 or 2."

def send_pdf_to_telegram(pdf_path: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(pdf_path, "rb") as pdf_file:
        files = {"document": pdf_file}
        data = {"chat_id": CHAT_ID, "caption": caption}
        response = requests.post(url, data=data, files=files)
        response.raise_for_status()

def load_favorite_slugs() -> list[str]:
    if not os.path.exists("favorite_shows.txt"):
        return []
    with open("favorite_shows.txt", "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


if __name__ == "__main__":
    main()

    favorite_slugs = load_favorite_slugs()
    lines = []
    for slug in favorite_slugs:
        sale_url = f"https://www.tiwall.com/s/{slug}"
        try:
            data = scrape_show(sale_url)
            sessions = data.get("sessions", [])
            count_front = sum(
                1 for s in sessions
                if s.get("has_front_row_free")
            )
            if count_front == 0:
                continue  # skip shows with no good sessions
            # lines.append(f"show: {data.get())"title",[])}")
            lines.append(f"link: {sale_url}")
            lines.append(f"Session: {count_front}")
            # lines.append(f"Score: {data.get("score",[]):.3f}")
            url = f"{API_URL}/sendMessage"
            text_data = {
                "chat_id": CHAT_ID_2,
                "text": lines,
            }
            r = requests.post(url, json=text_data)
            r.raise_for_status()
        except Exception as e:
            continue
    # run_every_hour_at(2)

