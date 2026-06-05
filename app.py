import os
import re
import time
import requests
import arabic_reshaper
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bidi.algorithm import get_display

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.lib.pagesizes import A3
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================

BOT_TOKEN = "8180945977:AAHIqAUWn4a0gtKC4Liv2lvYNN6D45rUCdE"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Chat IDs
CHAT_ID_REPORT = "-1003358233998"   # Group for PDF reports
CHAT_ID_ALERTS = "-1003253814794"  # Group for favorite show alerts

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

def has_persian_chars(text: str) -> bool:
    """Checks if the string contains Persian/Arabic characters."""
    return bool(re.search(r"[\u0600-\u06FF]", text))

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
            front_rows = {1, 2, "1", "2", "A", "B"}
            has_front = False

            rows_dict = {} # For text map generation

            for g in geometry:
                if g["code"] in sold_set:
                    status = "sold"
                elif g["code"] in locked_set:
                    status = "locked"   # reserved/held -> not buyable
                else:
                    status = "free"

                # Front-row availability counts ONLY genuinely free seats (not sold, not locked)
                if status == "free" and g["row"] in front_rows:
                    has_front = True
                
                # Group for map
                if g["row"] not in rows_dict: rows_dict[g["row"]] = []
                rows_dict[g["row"]].append({"status": status, "number": g["number"]})
                
                final_seats.append({**g, "status": status})

            sess["has_front_row_free"] = has_front
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
# REPORT GENERATION & NOTIFICATION
# ==========================================

def create_persian_pdf(content: str, filename: str, font_path: str = "Vazirmatn-Regular.ttf"):
    """Generates a PDF report supporting Persian/Arabic text."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    font_full_path = os.path.join(base_dir, font_path)

    if not os.path.exists(font_full_path):
        # Fallback if specific font missing, though user provided it
        print(f"Warning: Font {font_path} not found.")
        return

    pdfmetrics.registerFont(TTFont("Vazir", font_full_path))
    
    doc = SimpleDocTemplate(
        filename, pagesize=A3,
        rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40
    )

    styles = getSampleStyleSheet()
    
    style_fa = ParagraphStyle("Persian", parent=styles["Normal"], fontName="Vazir", fontSize=14, leading=20, alignment=TA_RIGHT)
    style_en = ParagraphStyle("English", parent=styles["Normal"], fontName="Vazir", fontSize=12, alignment=TA_LEFT)
    style_map = ParagraphStyle("Map", parent=styles["Normal"], fontName="Courier", fontSize=10, leading=10, backColor=colors.whitesmoke)

    story = []
    
    for line in content.splitlines():
        if not line.strip():
            story.append(Spacer(1, 10))
            continue

        if has_persian_chars(line):
            reshaped = get_display(arabic_reshaper.reshape(line))
            story.append(Paragraph(reshaped, style_fa))
        elif "Stage" in line or "Row" in line:
            story.append(Paragraph(line.replace(" ", "&nbsp;"), style_map))
        else:
            story.append(Paragraph(line, style_en))

    doc.build(story)

def send_telegram_message(chat_id: str, text: str = None, file_path: str = None):
    """Sends a text message or a document to Telegram."""
    if file_path:
        url = f"{API_URL}/sendDocument"
        with open(file_path, "rb") as f:
            data = {"chat_id": chat_id, "caption": text}
            files = {"document": f}
            requests.post(url, data=data, files=files).raise_for_status()
    elif text:
        url = f"{API_URL}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        requests.post(url, json=data).raise_for_status()


# ==========================================
# MAIN WORKFLOW & SCHEDULER
# ==========================================

def perform_hourly_job():
    """Main job that runs scraping, PDF generation, and alerting."""
    print(f"[{datetime.now()}] Starting hourly job...")
    scraper = TiwallScraper()

    # --- Task 1: Top Shows Report ---
    print("Fetching top shows...")
    top_shows = scraper.fetch_top_shows(limit=10)
    report_lines = ["Top Tiwall Shows Report", "=" * 30, ""]
    summary_lines = ["🎭 **Top Shows with Front Row Availability:**", ""]

    for show in top_shows:
        if not show["sale_url"]: continue
        
        try:
            details = scraper.scrape_show_details(show["sale_url"])
        except Exception as e:
            report_lines.append(f"Error scraping {show['title']}: {e}")
            continue

        # Add to PDF Report
        report_lines.append(f"🎭: {details['title']}")
        report_lines.append(f"⭐: {show['score']:.2f} | Rating: {show['rating']} | Votes: {show['votes']}")
        report_lines.append(f"🌐: {show['sale_url']}")
        
        available_sessions = [s for s in details["sessions"] if s["has_front_row_free"]]
        
        if not available_sessions:
            report_lines.append("No front row seats available.")
        else:
            # Add to Summary for Telegram Caption
            summary_lines.append(f"🎭 {details['title']}")
            summary_lines.append(f"   ⭐: {show['score']:.2f} | 🗓️: {len(available_sessions)}")
            summary_lines.append(f"   🌐: {show['sale_url']}\n")

            for sess in available_sessions:
                report_lines.append(f"  📅 {sess['date_text']} ({sess['status_text']})")
                if sess['seat_text_map']:
                    report_lines.append("\n" + sess['seat_text_map'] + "\n")
        
        report_lines.append("-" * 30)

    # Generate and Send PDF
    pdf_filename = "tiwall_report.pdf"
    create_persian_pdf("\n".join(report_lines), pdf_filename)
    
    summary_text = "\n".join(summary_lines)
    if len(summary_lines) <= 2:
        summary_text = "No top shows have front row seats available right now."

    print("Sending PDF report...")
    send_telegram_message(CHAT_ID_REPORT, text=summary_text, file_path=pdf_filename)


    # --- Task 2: Favorite Shows Alert ---
    # Reads a local file for slugs to monitor specifically
    fav_file = "favorite_shows.txt"
    if os.path.exists(fav_file):
        print("Checking favorite shows...")
        with open(fav_file, "r", encoding="utf-8") as f:
            slugs = [line.strip() for line in f if line.strip()]
        
        for slug in slugs:
            url = f"{BASE_URL}/s/{slug}"
            try:
                data = scraper.scrape_show_details(url)
                good_sessions = sum(1 for s in data["sessions"] if s.get("has_front_row_free"))
                
                if good_sessions > 0:
                    alert_msg = (
                        f"🚨 **Favorite Show Alert**\n"
                        f"Show: {data['title']}\n"
                        f"Sessions with Front Rows: {good_sessions}\n"
                        f"Link: {url}"
                    )
                    send_telegram_message(CHAT_ID_ALERTS, text=alert_msg)
            except Exception as e:
                print(f"Error checking favorite {slug}: {e}")

    print(f"[{datetime.now()}] Job complete.")

if __name__ == "__main__":
    # Start the continuous scheduler
    perform_hourly_job()
