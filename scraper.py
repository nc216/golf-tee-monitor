"""
Golf Vancouver Tee Time Monitor

Uses Playwright with stealth to load the CPS Golf booking page, navigate
through the calendar to weekend dates, and extract available tee times
via intercepted API responses. Detects newly appeared times (cancellations)
and sends an email notification.
"""

import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import Stealth
except ImportError:
    from playwright_stealth.stealth import Stealth

BASE_URL = (
    "https://golfvancouver.cps.golf/onlineresweb/search-teetime"
    "?TeeOffTimeMin=0&TeeOffTimeMax=23.999722222222225"
)

KNOWN_TIMES_FILE = Path(__file__).parent / "known_tee_times.json"
DAYS_AHEAD = 14

# Email settings
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "noahcastelo@gmail.com")


def load_known_times() -> set[str]:
    if KNOWN_TIMES_FILE.exists():
        return set(json.loads(KNOWN_TIMES_FILE.read_text()))
    return set()


def save_known_times(keys: set[str]) -> None:
    KNOWN_TIMES_FILE.write_text(json.dumps(sorted(keys), indent=2))


def make_key(date: str, time: str, course: str, players: str) -> str:
    return f"{date}|{time}|{course}|{players}"


def get_weekend_dates(days_ahead: int = DAYS_AHEAD) -> list[datetime]:
    today = datetime.now()
    dates = []
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        if d.weekday() in (5, 6):
            dates.append(d)
    return dates


def parse_tee_times_from_api(data: dict, date: datetime) -> list[dict]:
    results = []
    for item in data.get("content", []):
        start_time = item.get("startTime", "")
        course_name = item.get("courseName", "Unknown")
        players_display = item.get("playersDisplay", "?")
        holes_display = item.get("holesDisplay", "?")
        price = ""
        prices = item.get("shItemPrices", [])
        if prices:
            price = f"CA${prices[0].get('price', '?')}"

        try:
            dt = datetime.fromisoformat(start_time)
            time_str = dt.strftime("%-I:%M %p")
            date_str = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            time_str = start_time
            date_str = date.strftime("%Y-%m-%d")

        results.append({
            "date": date_str,
            "day": date.strftime("%A"),
            "time": time_str,
            "course": course_name,
            "holes": holes_display,
            "players": players_display,
            "price": price,
        })
    return results


def dismiss_modals(page) -> None:
    """Close any popup dialogs (course notes, etc.)."""
    for _ in range(5):
        close_btn = page.query_selector('button:has-text("Close")')
        if close_btn and close_btn.is_visible():
            close_btn.click()
            page.wait_for_timeout(500)
        else:
            break


def get_calendar_month(page) -> str:
    """Read the currently displayed month name from the calendar header."""
    header = page.query_selector(".ngx-dates-picker-calendar-container")
    if header:
        text = header.inner_text()
        # First line should be like "March 2026"
        first_line = text.split("\n")[0].strip()
        return first_line
    return ""


def navigate_to_month(page, target_date: datetime) -> bool:
    """Click the forward arrow until the calendar shows the target month."""
    target_label = target_date.strftime("%B %Y")
    for _ in range(6):
        current = get_calendar_month(page)
        if target_label in current:
            return True
        # The forward arrow is the last div in .topbar-container
        # (structure: [back-arrow div] [title span] [forward-arrow div])
        next_btn = page.query_selector(
            ".topbar-container > div:last-child"
        )
        if next_btn and next_btn.is_visible():
            cls = next_btn.get_attribute("class") or ""
            if "disabled" in cls:
                print(f"  Forward arrow is disabled")
                return False
            next_btn.click()
            page.wait_for_timeout(500)
        else:
            print(f"  Could not find next-month button")
            return False
    return False


def click_calendar_day(page, day_num: int) -> bool:
    """Click a specific day number in the visible calendar."""
    day_spans = page.query_selector_all(".day-background-upper.is-visible")
    for span in day_spans:
        if span.inner_text().strip() == str(day_num):
            cls = span.get_attribute("class") or ""
            if "is-disabled" in cls:
                return False
            span.click()
            return True
    return False


def scrape_tee_times() -> list[dict]:
    stealth = Stealth()
    all_tee_times = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        captured = []

        def on_response(response):
            if "TeeTimes" in response.url and response.status == 200:
                try:
                    captured.append(response.json())
                except Exception:
                    pass

        page.on("response", on_response)

        print(f"Loading {BASE_URL}")
        page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)

        # Check for bot detection
        body_text = page.inner_text("body")
        if "Suspicious" in body_text:
            print("ERROR: Bot detection triggered.")
            browser.close()
            return []

        # Dismiss course notes modals
        dismiss_modals(page)
        print("Page loaded successfully.")

        weekend_dates = get_weekend_dates()
        print(f"Checking {len(weekend_dates)} weekend dates...\n")

        current_calendar_month = get_calendar_month(page)
        print(f"Calendar showing: {current_calendar_month}")

        for target_date in weekend_dates:
            target_month_label = target_date.strftime("%B %Y")
            day_num = target_date.day

            # Navigate to the right month if needed
            current = get_calendar_month(page)
            if target_month_label not in current:
                print(f"  Navigating calendar to {target_month_label}...")
                if not navigate_to_month(page, target_date):
                    print(f"  Failed to navigate to {target_month_label}")
                    continue

            # Click the day
            captured.clear()
            if not click_calendar_day(page, day_num):
                print(f"  Could not click {target_date.strftime('%b %d')} (disabled or not found)")
                continue

            # Wait for API response
            page.wait_for_timeout(4000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Dismiss any modals that pop up
            dismiss_modals(page)

            # Parse the API response
            if captured:
                data = captured[-1]
                times = parse_tee_times_from_api(data, target_date)
                print(f"  {target_date.strftime('%A %b %d')}: {len(times)} tee times")
                all_tee_times.extend(times)
            else:
                print(f"  {target_date.strftime('%A %b %d')}: no API response captured")

        browser.close()

    return all_tee_times


def send_email(new_times: list[dict]) -> None:
    if not EMAIL_FROM or not EMAIL_PASSWORD:
        print("\nEmail credentials not configured. Printing to stdout instead.")
        print("\n=== NEW TEE TIMES AVAILABLE ===")
        for tt in new_times:
            print(
                f"  {tt['day']} {tt['date']} | {tt['time']:>8s} | "
                f"{tt['course']:<30s} | {tt['holes']}H | {tt['players']} | {tt['price']}"
            )
        return

    subject = f"Golf Tee Time Alert: {len(new_times)} new time(s) available!"

    lines = ["New tee times on Golf Vancouver (likely cancellations):\n"]

    by_date = {}
    for tt in new_times:
        key = f"{tt['day']} {tt['date']}"
        by_date.setdefault(key, []).append(tt)

    for date_label, times in sorted(by_date.items()):
        lines.append(f"\n--- {date_label} ---")
        for tt in sorted(times, key=lambda x: x["time"]):
            lines.append(
                f"  {tt['time']:>8s}  {tt['course']:<30s}  "
                f"{tt['holes']}H  {tt['players']}  {tt['price']}"
            )

    lines.append(f"\nBook now: {BASE_URL}")

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"Email sent to {EMAIL_TO} with {len(new_times)} new tee time(s).")


def main():
    print(f"[{datetime.now().isoformat()}] Starting tee time check...")

    tee_times = scrape_tee_times()
    print(f"\nFound {len(tee_times)} total weekend tee times.")

    if not tee_times:
        print("No tee times found.")
        return

    current_keys = set()
    key_to_time = {}
    for tt in tee_times:
        key = make_key(tt["date"], tt["time"], tt["course"], tt["players"])
        current_keys.add(key)
        key_to_time[key] = tt

    known_keys = load_known_times()
    new_keys = current_keys - known_keys

    if new_keys:
        new_times = [key_to_time[k] for k in sorted(new_keys)]
        print(f"\n*** {len(new_times)} NEW tee time(s) detected! ***")
        send_email(new_times)
    else:
        print("No new tee times since last check.")

    # Save and prune old keys
    all_keys = known_keys | current_keys
    today_str = datetime.now().strftime("%Y-%m-%d")
    pruned = {k for k in all_keys if k.split("|")[0] >= today_str}
    save_known_times(pruned)
    print(f"Saved {len(pruned)} known tee time keys.")


if __name__ == "__main__":
    main()
