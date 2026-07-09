import os
import re
import time
import requests
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types as genai_types, errors as genai_errors
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================

# Secrets are injected via environment variables (see .github/workflows/tiwall-watcher.yml).
# Never hardcode credentials here; this is a public repository.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID_REPORT = os.environ.get("CHAT_ID_REPORT", "")   # Group for the top-shows summary
CHAT_ID_ALERTS = os.environ.get("CHAT_ID_ALERTS", "")   # Group for favorite show alerts

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is not set. Configure it as a repository secret.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Optional: enables Gemini-researched public-opinion remarks in the channel
# summary. When unset, the job runs normally without remarks.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
INFORMATION_FILE = "information.txt"   # persistent show-feedback bank, committed to the repo
INFO_MAX_AGE_DAYS = 14                 # re-research a show after this many days
TEHRAN_TZ = ZoneInfo("Asia/Tehran")

# Tiwall URLs
BASE_URL = "https://www.tiwall.com"
SHOWCASE_URL = "https://www.tiwall.com/showcase?filters=city:2111,s:theater,available:true&order=rating"
SEATMAP_API_URL = "https://www.tiwall.com/api/v1/internal/general/seatmapState"
VARIATIONS_API_URL = "https://www.tiwall.com/api/v1/projects/{}/variations"

# HTTP Configuration
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# Regex Patterns
URL_RE = re.compile(r"(https?://\S+)")
TIME_PATTERN = re.compile(r"[0-9۰-۹]{1,2}[:٫][0-9۰-۹]{2}")
PERSIAN_DIGITS_MAP = {
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
}


# ==========================================
# UTILITY FUNCTIONS
# ==========================================

def persian_to_english(text: str) -> str:
    """Converts Persian digits in a string to English digits."""
    if not text:
        return ""
    return "".join(PERSIAN_DIGITS_MAP.get(ch, ch) for ch in text)

def persian_to_int(text: str) -> Optional[int]:
    """Extracts integers from a string containing Persian or English digits."""
    en_text = persian_to_english(text)
    digits = "".join(c for c in en_text if c.isdigit())
    return int(digits) if digits else None

def is_element_hidden(tag) -> bool:
    """Checks if a BeautifulSoup tag is visually hidden via CSS classes or inline styles."""
    if not tag:
        return False
    
    style = tag.get("style", "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
        
    classes = tag.get("class", [])
    if any(c in classes for c in ["hidden", "d-none", "deleted", "invisible"]):
        return True
        
    return False


# ==========================================
# SCRAPING LOGIC
# ==========================================

class TiwallScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def safe_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Wrapper for HTTP requests with error handling."""
        try:
            resp = self.session.request(method, url, timeout=20, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            print(f"Request failed for {url}: {e}")
            return None

    def fetch_top_shows(self, limit: int = 10, min_votes: int = 100) -> List[Dict[str, Any]]:
        """Fetches the top rated shows from the showcase page using Bayesian ranking."""
        resp = self.safe_request("GET", SHOWCASE_URL)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Remove archived content to avoid parsing old shows
        if archived := soup.find("div", class_="archived-pages"):
            archived.decompose()

        shows = []
        for card in soup.select("a.item-page"):
            shows.append(self._parse_showcase_card(card))

        # Filter valid shows (must have rating and votes)
        valid_shows = [s for s in shows if s["rating"] is not None and s["votes"]]
        
        if not valid_shows:
            return []

        # Bayesian Average Calculation
        avg_rating_all = sum(s["rating"] for s in valid_shows) / len(valid_shows)
        
        for s in shows:
            r = s["rating"]
            v = s["votes"] or 0
            if r is None or v < min_votes:
                s["score"] = 0
            else:
                s["score"] = (v / (v + min_votes)) * r + (min_votes / (v + min_votes)) * avg_rating_all

        # Sort by score descending
        shows.sort(key=lambda x: x["score"], reverse=True)
        return shows[:limit]

    def _parse_showcase_card(self, card) -> Dict[str, Any]:
        """Extracts metadata from a single show card in the showcase list."""
        href = card.get("href", "")
        page_url = urljoin(BASE_URL, href)
        
        title = ""
        info_div = card.select_one("div.info")
        if info_div:
            h2 = info_div.select_one("h2")
            if h2:
                # Remove small secondary text like 'Writer: ...'
                if small := h2.select_one("span.normal.small"):
                    small.extract()
                title = h2.get_text(strip=True)

        rating, votes = None, None
        if rating_span := info_div.select_one("span.avg-rating"):
            txt = rating_span.get_text(strip=True)
            if "★" in txt:
                votes_part, rating_part = [p.strip() for p in txt.split("★", 1)]
                votes = persian_to_int(votes_part)
                try:
                    rating = float(persian_to_english(rating_part.replace("٫", ".")))
                except ValueError:
                    rating = None

        sale_btn = card.select_one("span.btn.tmp-label")
        sale_urn = sale_btn.get("data-saleurn") if sale_btn else None
        sale_url = urljoin(BASE_URL, sale_urn) if sale_urn else None
        slug = sale_urn.rstrip("/").split("/")[-1] if sale_urn else None

        return {
            "title": title,
            "rating": rating,
            "votes": votes,
            "page_url": page_url,
            "sale_url": sale_url,
            "slug": slug,
            "score": 0.0
        }

    def scrape_show_details(self, sale_url: str) -> Dict[str, Any]:
        """Deep scrapes a specific show page for sessions and seat availability."""
        resp = self.safe_request("GET", sale_url)
        if not resp:
            raise ValueError(f"Could not fetch show page: {sale_url}")

        html = resp.text
        slug = urlparse(sale_url).path.rstrip("/").split("/")[-1]
        
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"
        # The h1 is a breadcrumb like "نمایش›TITLE›خرید" — keep only the show name.
        if "›" in title:
            parts = [p.strip() for p in title.split("›") if p.strip()]
            if len(parts) >= 3:
                title = "›".join(parts[1:-1])

        # 1. Get Sessions (Try HTML first, fallback to API)
        sessions = self._parse_sessions_html(soup)
        if not sessions:
            sessions = self._fetch_sessions_api(slug)

        # 2. Parse Geometry (Static HTML representation of seats)
        geometry = self._parse_geometry(soup)

        # 3. Analyze availability for each session
        for sess in sessions:
            self._process_session_availability(sess, slug, sale_url, geometry)

        # 4. Generate a default text map for the report
        text_map = next((s["seat_text_map"] for s in sessions if s.get("seat_text_map")), 
                        "No seatmap available.")

        return {
            "title": title,
            "sale_url": sale_url,
            "slug": slug,
            "sessions": sessions,
            "geometry": geometry,
            "text_map": text_map,
        }

    def _parse_sessions_html(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Parses the 'showtimeMenu' div for session dates and IDs."""
        menu = soup.find("div", id="showtimeMenu") or soup.find("div", class_="showtimeMenu")
        if not menu:
            return []

        sessions = []
        days_fa = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]

        for a_tag in menu.find_all("a", attrs={"data-id": True}):
            raw_text = a_tag.get_text(" ", strip=True)
            if not raw_text:
                continue

            instance_id = int(a_tag.get("data-id"))
            
            # Extract Time
            time_match = TIME_PATTERN.search(raw_text)
            time_text = persian_to_english(time_match.group(0).replace("٫", ":")) if time_match else None

            # Determine Status
            status_text = "Available"
            is_sold_out = False
            
            if re.search(r"(پُر|پر)\s*شد", raw_text):
                status_text = "SOLD OUT"
                is_sold_out = True
            elif re.search(r"بیرون\s+از\s+ظرفیت", raw_text):
                status_text = "Extra Capacity"
            elif match := re.search(r"(مانده\s+دارد|مانده[:\s]+[0-9۰-۹]+\s*(?:بلیط|بلیت))", raw_text):
                status_text = match.group(0)

            # Extract Date (heuristic)
            date_text = raw_text # default
            tokens = raw_text.split()
            for i, tok in enumerate(tokens):
                if any(day in tok for day in days_fa):
                    date_text = " ".join(tokens[i:i+5])
                    break

            sessions.append({
                "date_text": date_text,
                "time_text": time_text,
                "status_text": status_text,
                "sold_out": is_sold_out,
                "instance_id": instance_id,
                "has_front_row_free": False, # Placeholder
                "seat_text_map": None
            })
        return sessions

    def _fetch_sessions_api(self, slug: str) -> List[Dict[str, Any]]:
        """Fallback: Fetches session data via API if HTML parsing fails."""
        url = VARIATIONS_API_URL.format(slug)
        resp = self.safe_request("GET", url, headers={"Referer": f"{BASE_URL}/s/{slug}"})
        
        if not resp:
            return []
            
        try:
            data = resp.json()
            items = data.get('data', {}).get('items', []) or data.get('items', [])
            sessions = []
            
            for item in items:
                name = item.get('name') or item.get('title') or "Unknown Date"
                is_sold_out = item.get('sales_finished', False) or item.get('sold_out', False)
                
                sessions.append({
                    "date_text": name,
                    "time_text": None,
                    "status_text": "SOLD OUT" if is_sold_out else "Available",
                    "sold_out": is_sold_out,
                    "instance_id": item.get('id'),
                    "has_front_row_free": False,
                    "seat_text_map": None
                })
            return sessions
        except Exception:
            return []

    def _parse_geometry(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Parses the static HTML representation of the seating plan."""
        seats = []
        for chair in soup.select("div.chair"):
            if is_element_hidden(chair):
                continue
            
            # Check if parent rows are hidden
            parent = chair.parent
            parent_hidden = False
            for _ in range(3):
                if not parent: break
                if is_element_hidden(parent):
                    parent_hidden = True
                    break
                parent = parent.parent
            if parent_hidden: continue

            inp = chair.find("input", {"name": "chair"})
            if not inp: continue

            base_id = inp.get("data-base-id") or inp.get("value", "")
            
            # Parse ID format: Zone-Row-Seat (e.g., A-1-5)
            match = re.match(r"^(.+)-([^-]+)-([0-9۰-۹]+)$", base_id)
            if match:
                zone, row_raw, seat_raw = match.groups()
                row = persian_to_int(row_raw) or row_raw
                number = persian_to_int(seat_raw)
                
                seats.append({
                    "zone": zone,
                    "row": row,
                    "number": number,
                    "code": f"{zone}-{row}-{number}"
                })
        return seats

    def _process_session_availability(self, sess: Dict, slug: str, sale_url: str, geometry: List[Dict]):
        """Fetches dynamic seat status (JSON) and merges with geometry."""
        if sess["sold_out"] or not sess["instance_id"]:
            return

        # Case: General Admission (No seat map in HTML)
        if not geometry:
            sess["seat_text_map"] = "General Admission (No Seat Map)"
            sess["has_front_row_free"] = False  # General admission has no rows; do not claim front-row availability
            return

        try:
            # Fetch dynamic status
            params = {"instance_id": sess["instance_id"], "init": 2, "urn": slug}
            headers = {"Referer": sale_url, "Origin": BASE_URL}
            
            resp = self.safe_request("GET", SEATMAP_API_URL, params=params, headers=headers)
            if not resp:
                raise Exception("API Error")
                
            json_data = resp.json().get("data", {})
            
            # Parse Statuses
            sold_set = set()
            for item in json_data.get("state", []):
                if item.get("s") == 1: # 1 = Sold
                    code = item.get("c", "").replace('همکف', 'A')
                    sold_set.add(code)

            # Parse locked/reserved seats. Format: comma-separated "ZONE-ROW-START:END=TYPE"
            # entries, e.g. "A-1-7:12=r" locks seats A-1-7 .. A-1-12. A single seat may omit ":END".
            # These are held/reserved and are NOT actually buyable, so they must not count as free.
            locked_set = set()
            for token in (json_data.get("locks", "") or "").split(","):
                token = token.strip()
                if not token:
                    continue
                base = token.split("=", 1)[0].replace('همکف', 'A')  # drop "=r" type suffix
                left, _, end_raw = base.partition(":")              # split optional range end
                m = re.match(r"^(.+)-([^-]+)-([0-9۰-۹]+)$", left)
                if not m:
                    continue
                zone, row_raw, start_raw = m.groups()
                row = persian_to_int(row_raw) or row_raw            # match geometry's row format
                start = persian_to_int(start_raw)
                end = persian_to_int(end_raw) if end_raw else start
                if start is None:
                    continue
                if end is None or end < start:
                    end = start
                for n in range(start, end + 1):
                    locked_set.add(f"{zone}-{row}-{n}")

            # Merge
            final_seats = []
            front_rows = {1, 2, 3, "1", "2", "3", "A", "B", "C"}
            # Free seat numbers in front rows, grouped by (zone, row) so adjacency
            # is only checked within the same physical row.
            front_free: Dict = {}

            rows_dict = {} # For text map generation

            for g in geometry:
                if g["code"] in sold_set:
                    status = "sold"
                elif g["code"] in locked_set:
                    status = "locked"   # reserved/held -> not buyable
                else:
                    status = "free"

                # Front-row availability counts ONLY genuinely free seats (not sold, not locked)
                if status == "free" and g["row"] in front_rows and g["number"] is not None:
                    front_free.setdefault((g["zone"], g["row"]), set()).add(g["number"])

                # Group for map
                if g["row"] not in rows_dict: rows_dict[g["row"]] = []
                rows_dict[g["row"]].append({"status": status, "number": g["number"]})

                final_seats.append({**g, "status": status})

            # Flag only if a pair of adjacent free seats exists in a front row,
            # so two people can sit next to each other.
            sess["has_front_row_free"] = any(
                n + 1 in numbers for numbers in front_free.values() for n in numbers
            )
            sess["seats"] = final_seats
            sess["seat_text_map"] = self._render_text_map(rows_dict)

        except Exception as e:
            sess["seat_text_map"] = f"Error fetching seatmap: {e}"

    def _render_text_map(self, rows_dict: Dict) -> str:
        """Generates a simple ASCII representation of the seat map."""
        lines = ["Stage", "====="]
        sorted_rows = sorted(rows_dict.keys(), key=lambda x: int(x) if isinstance(x, int) else 999)
        
        for r in sorted_rows:
            seats = sorted(rows_dict[r], key=lambda x: x["number"])
            chars = "".join({"sold": "X", "locked": "L"}.get(s["status"], "A") for s in seats)
            lines.append(f"Row {r:>2}: {chars}")
            
        return "\n".join(lines)


# ==========================================
# SHOW OPINION RESEARCH (Gemini API)
# ==========================================

def load_show_info() -> Dict[str, Dict]:
    """Loads the persistent show-feedback bank.

    Line format: "slug | YYYY-MM-DD | remark". Older 4-field lines
    ("slug | date | brief | detail") load the detail (falling back to the
    brief). Hand-added lines may omit the date ("slug | remark") and are
    treated as researched today.
    """
    info: Dict[str, Dict] = {}
    if not os.path.exists(INFORMATION_FILE):
        return info
    with open(INFORMATION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|", 3)]
            if len(parts) < 2:
                continue
            slug = parts[0]
            date = None
            if len(parts) >= 3:
                try:
                    date = datetime.strptime(parts[1], "%Y-%m-%d").date()
                except ValueError:
                    pass
            if date is not None:
                # 4-field legacy lines: prefer the detailed remark
                remark = (parts[3] if len(parts) == 4 and parts[3] else parts[2])
            else:
                date = datetime.now(TEHRAN_TZ).date()
                remark = " | ".join(p for p in parts[1:] if p)
            # Legacy "no feedback found" lines are misses, not answers — drop
            # them so the show gets re-researched.
            if slug and remark and NO_FEEDBACK_MARKER not in remark:
                info[slug] = {"date": date, "remark": remark}
    return info

def save_show_info(info: Dict[str, Dict]):
    lines = ["# Show feedback bank — format: slug | researched date | remark", ""]
    for slug in sorted(info):
        entry = info[slug]
        lines.append(f"{slug} | {entry['date'].strftime('%Y-%m-%d')} | {entry['remark']}")
    with open(INFORMATION_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

NO_FEEDBACK_MARKER = "یافت نشد"  # Gemini's "no reliable feedback" fallback

def get_shows_info_batch(client: "genai.Client", shows: List[Dict]) -> Dict[str, str]:
    """Asks Gemini (one request, with Google Search grounding) for a detailed
    critical Persian remark on the public reception of each show. Each show
    dict has slug/title/rating/votes. Returns {slug: remark}."""
    listing_lines = []
    for s in shows:
        line = f"{s['slug']} | {s['title']}"
        if s.get("rating"):
            line += f" | امتیاز تیوال: {s['rating']} از 5 ({s.get('votes') or '?'} رای)"
        listing_lines.append(line)
    listing = "\n".join(listing_lines)
    prompt = (
        "نمایش‌های زیر هم‌اکنون در تهران روی صحنه هستند. "
        "امتیاز واقعی هر نمایش در سایت تیوال هم داده شده است. "
        "برای هر نمایش با جستجو در وب، بازخورد واقعی تماشاگران و منتقدان ایرانی را پیدا کن "
        "(نقدها و نظرات در تیوال، شبکه‌های اجتماعی و رسانه‌ها).\n"
        "مانند یک منتقد تئاتر بی‌طرف و سخت‌گیر عمل کن:\n"
        "- امتیاز تیوال داده‌شده را مبنا قرار بده و در جمله‌ات بیاور؛ با جستجو نقاط قوت و ضعف مشخص را پیدا کن.\n"
        "- نقاط ضعف و نقدهای منفی را به همان اندازه نقاط قوت ذکر کن. "
        "اگر بازخوردها متفاوت یا متوسط است، صادقانه بنویس نظرها دوپهلوست و ضعف اصلی را نام ببر.\n"
        "- از صفت‌های تبلیغاتی و کلی مثل «عالی» و «بی‌نظیر» بدون استناد به نظر واقعی خودداری کن.\n"
        "- فقط اگر هیچ نظر کیفی پیدا نکردی و امتیازی هم داده نشده، بنویس: بازخورد قابل اعتمادی یافت نشد.\n"
        "پاسخ را دقیقاً در همین قالب بده: برای هر نمایش فقط یک خط، به شکل\n"
        "slug | نقد کامل در یک پاراگراف مفصل (حدود ۵ تا ۷ جمله) به فارسی، "
        "شامل نقاط قوت با ذکر جزئیات (بازی‌ها، کارگردانی، متن، طراحی صحنه و موسیقی)، نقاط ضعف مشخص و جمع‌بندی نظر تماشاگران و منتقدان\n"
        "داخل نقد از علامت | استفاده نکن.\n"
        "از همان slug انگلیسی که داده شده استفاده کن و هیچ متن دیگری ننویس.\n\n"
        f"فهرست نمایش‌ها:\n{listing}"
    )
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
            ),
        )
        text = response.text or ""
    except genai_errors.APIError as e:
        print(f"Batch opinion research failed: {e}")
        return {}
    except Exception as e:
        print(f"Batch opinion research unexpected error: {e}")
        return {}

    valid_slugs = {s["slug"] for s in shows}
    results: Dict[str, str] = {}
    for line in text.splitlines():
        if "|" not in line:
            continue
        slug, _, remark = line.partition("|")
        slug = slug.strip().lstrip("-*•").strip()
        remark = " ".join(remark.split())  # collapse whitespace/newlines
        # "no feedback found" is a miss, not an answer — don't bank it for 14
        # days; leave the show absent so the next run retries.
        if slug in valid_slugs and remark and NO_FEEDBACK_MARKER not in remark:
            results[slug] = remark
    missed = valid_slugs - set(results)
    if missed:
        print(f"Batch opinion research got no answer for: {', '.join(sorted(missed))}")
    return results


# ==========================================
# NOTIFICATION
# ==========================================

TELEGRAM_TEXT_LIMIT = 4096

def _split_message(text: str, limit: int) -> List[str]:
    """Splits text into chunks under the limit, breaking at line boundaries."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # A single line longer than the limit gets hard-cut
            current = line[:limit]
    if current:
        chunks.append(current)
    return chunks

def _pack_blocks(blocks: List[str], limit: int) -> List[str]:
    """Packs blocks into chunks under the limit without ever splitting a
    block: a block that doesn't fit starts the next chunk. Only a single
    block longer than the limit itself falls back to line splitting."""
    chunks, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(block) <= limit:
            current = block
        else:
            parts = _split_message(block, limit)
            chunks.extend(parts[:-1])
            current = parts[-1]
    if current:
        chunks.append(current)
    return chunks

def send_telegram_message(chat_id: str, text: str):
    """Sends a text message to Telegram, splitting it if over the limit."""
    url = f"{API_URL}/sendMessage"
    for chunk in _split_message(text, TELEGRAM_TEXT_LIMIT):
        requests.post(url, json={"chat_id": chat_id, "text": chunk}).raise_for_status()


# ==========================================
# MAIN WORKFLOW & SCHEDULER
# ==========================================

def perform_hourly_job():
    """Main job that runs scraping, summary notification, and alerting."""
    print(f"[{datetime.now()}] Starting hourly job...")
    scraper = TiwallScraper()

    # --- Task 1: Top Shows Summary ---
    print("Fetching top shows...")
    top_shows = scraper.fetch_top_shows(limit=30)
    # One block per show; blocks are never split across Telegram messages.
    summary_blocks = ["🎭 **Top Shows with Front Row Availability:**"]

    summary_shows = []  # shows that will appear in the Telegram summary

    for show in top_shows:
        if not show["sale_url"]: continue

        try:
            details = scraper.scrape_show_details(show["sale_url"])
        except Exception as e:
            print(f"Error scraping {show['title']}: {e}")
            continue

        available_sessions = [s for s in details["sessions"] if s["has_front_row_free"]]

        if available_sessions:
            summary_shows.append({
                "slug": details["slug"],
                "title": details["title"],
                "score": show["score"],
                "rating": show["rating"],
                "votes": show["votes"],
                "session_count": len(available_sessions),
                "url": show["sale_url"],
            })

    # --- Opinion research: one batched Gemini call for shows without fresh info ---
    show_info = load_show_info()
    today = datetime.now(TEHRAN_TZ).date()
    missing = [
        s for s in summary_shows
        if s["slug"] not in show_info
        or (today - show_info[s["slug"]]["date"]).days > INFO_MAX_AGE_DAYS
    ]
    if missing:
        if GEMINI_API_KEY:
            # Bounded timeout (ms): a hung API must not stall the hourly job.
            gemini_client = genai.Client(
                api_key=GEMINI_API_KEY,
                http_options=genai_types.HttpOptions(timeout=240_000),
            )
            print(f"Researching {len(missing)} shows in one batch: {', '.join(s['slug'] for s in missing)}")
            new_remarks = get_shows_info_batch(gemini_client, missing)
            for slug, remark in new_remarks.items():
                show_info[slug] = {"date": today, "remark": remark}
            if new_remarks:
                save_show_info(show_info)
        else:
            print("GEMINI_API_KEY not set; skipping show opinion research.")

    # Build the Telegram summary (stale entries keep showing until re-researched)
    for s in summary_shows:
        block_lines = [f"🎭 {s['title']}",
                       f"   ⭐: {s['score']:.2f} | 🗓️: {s['session_count']}"]
        if entry := show_info.get(s["slug"]):
            block_lines.append(f"   💬 {entry['remark']}")
        block_lines.append(f"   🌐: {s['url']}")
        summary_blocks.append("\n".join(block_lines))

    print("Sending summary...")
    if len(summary_blocks) <= 1:
        send_telegram_message(CHAT_ID_REPORT, "No top shows have front row seats available right now.")
    else:
        # Whole show blocks per message; a block that doesn't fit moves to the next one
        for chunk in _pack_blocks(summary_blocks, TELEGRAM_TEXT_LIMIT):
            send_telegram_message(CHAT_ID_REPORT, chunk)


    # --- Task 2: Favorite Shows Alert ---
    # Reads a local file for slugs to monitor specifically
    fav_file = "favorite_shows.txt"
    if os.path.exists(fav_file):
        print("Checking favorite shows...")
        # Each line: "slug" or "slug | excluded date | excluded date ...".
        # Sessions whose date contains an excluded date are not alerted on.
        favorites = []
        with open(fav_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if parts:
                    favorites.append((parts[0], parts[1:]))

        for slug, excluded_dates in favorites:
            url = f"{BASE_URL}/s/{slug}"
            try:
                try:
                    data = scraper.scrape_show_details(url)
                except ValueError:
                    # Page is gone (404) or unreachable — skip and move on.
                    print(f"Favorite '{slug}' page not available, skipping.")
                    continue
                good_sessions = [
                    s for s in data["sessions"]
                    if s.get("has_front_row_free")
                    # Compare with digits normalized so exclusions work whether
                    # typed as "19 تیر" or "۱۹ تیر".
                    and not any(
                        persian_to_english(x) in persian_to_english(s["date_text"])
                        for x in excluded_dates
                    )
                ]

                if good_sessions:
                    session_lines = []
                    for s in good_sessions[:5]:
                        # date_text often already ends with "› <time>"; strip it to avoid
                        # repeating the time we append explicitly.
                        date_part = re.sub(r"[›>]\s*[0-9۰-۹]{1,2}[:٫][0-9۰-۹]{2}\s*$", "", s["date_text"]).strip()
                        line = f"  📅 {date_part}"
                        if s.get("time_text"):
                            line += f" 🕒 {s['time_text']}"
                        session_lines.append(line)
                    if len(good_sessions) > 5:
                        session_lines.append(f"  … and {len(good_sessions) - 5} more")
                    alert_msg = (
                        f"🚨 **Favorite Show Alert**\n"
                        f"{data['title']}\n"
                        f"Sessions with Front Rows: {len(good_sessions)}\n"
                        + "\n".join(session_lines) + "\n"
                        f"Link: {url}"
                    )
                    send_telegram_message(CHAT_ID_ALERTS, text=alert_msg)
            except Exception as e:
                print(f"Error checking favorite {slug}: {e}")

    print(f"[{datetime.now()}] Job complete.")

if __name__ == "__main__":
    # Start the continuous scheduler
    perform_hourly_job()
