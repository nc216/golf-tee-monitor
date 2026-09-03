"""
Golf Vancouver Tee Time Monitor

Uses curl_cffi (browser TLS fingerprint impersonation) to call the
CPS Golf TeeTimes API directly. Detects newly appeared times
(cancellations) and sends email notifications.
"""

from __future__ import annotations

import json
import os
import smtplib
import uuid
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from curl_cffi import requests

BASE_URL = (
    "https://golfvancouver.cps.golf/onlineresweb/search-teetime"
    "?TeeOffTimeMin=0&TeeOffTimeMax=23.999722222222225"
)
API_BASE = "https://golfvancouver.cps.golf"

KNOWN_TIMES_FILE = Path(__file__).parent / "known_tee_times.json"

# Only alert for these courses (empty list = all courses)
COURSES_FILTER = []

# Specific dates to monitor (YYYY-MM-DD). Leave empty to use day-of-week logic.
TARGET_DATES = ["2026-09-07"]

# Email settings
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "noahcastelo@gmail.com")

# API constants
WEBSITE_ID = "2957df8d-a5c0-40e2-6586-08dd13a88838"
COURSE_IDS = "2,1,3"  # Fraserview=2, Langara=1, McCleery=3


def load_known_times() -> set[str]:
    if KNOWN_TIMES_FILE.exists():
        return set(json.loads(KNOWN_TIMES_FILE.read_text()))
    return set()


def save_known_times(keys: set[str]) -> None:
    KNOWN_TIMES_FILE.write_text(json.dumps(sorted(keys), indent=2))


def make_key(date: str, time: str, course: str, players: str) -> str:
    return f"{date}|{time}|{course}|{players}"


def get_target_dates() -> list[datetime]:
    if TARGET_DATES:
        return [datetime.strptime(d, "%Y-%m-%d") for d in TARGET_DATES]
    today = datetime.now()
    dates = []
    for i in range(14):
        d = today + timedelta(days=i)
        if d.weekday() in (5, 6):
            dates.append(d)
    return dates


def parse_tee_times(content: list, date: datetime) -> list[dict]:
    results = []
    for item in content:
        if not isinstance(item, dict):
            continue
        start_time = item.get("startTime", "")
        course_name = item.get("courseName", "Unknown")
        if COURSES_FILTER and course_name not in COURSES_FILTER:
            continue
        players_display = item.get("playersDisplay", "?")
        holes_display = item.get("holesDisplay", "?")
        price = ""
        prices = item.get("shItemPrices", [])
        if prices:
            price = "CA${}".format(prices[0].get("price", "?"))

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


def fetch_tee_times(target_dates: list[datetime]) -> list[dict]:
    session = requests.Session(impersonate="chrome")

    # Step 1: Get API token
    token_resp = session.post(
        f"{API_BASE}/identityapi/myconnect/token/short",
        data=(
            "client_id=onlinereswebshortlived"
            "&client_secret=v4secret"
            "&grant_type=client_credentials"
            "&scope=onlinereservation references"
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if token_resp.status_code != 200:
        print(f"ERROR: Token request failed ({token_resp.status_code})")
        return []
    token = token_resp.json()["access_token"]
    print("  Token acquired")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "client-id": "onlineresweb",
        "x-websiteid": WEBSITE_ID,
        "x-componentid": "1",
        "x-siteid": "6",
        "x-productid": "1",
        "x-moduleid": "7",
        "X-TerminalId": "3",
        "x-timezone-offset": "420",
        "x-timezoneid": "America/Vancouver",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://golfvancouver.cps.golf/onlineresweb/search-teetime",
        "Origin": "https://golfvancouver.cps.golf",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    # Step 2: Register transaction ID
    txn_id = str(uuid.uuid4())
    reg_resp = session.post(
        f"{API_BASE}/onlineres/onlineapi/api/v1/onlinereservation/RegisterTransactionId",
        data=json.dumps({"transactionId": txn_id}),
        headers=headers,
    )
    if reg_resp.status_code != 200:
        print(f"ERROR: RegisterTransactionId failed ({reg_resp.status_code})")
        print(f"  Body: {reg_resp.text[:200]}")
        return []
    print("  Transaction registered")

    # Step 3: Fetch tee times for each date
    all_tee_times = []
    for target_date in target_dates:
        date_str = target_date.strftime("%a %b %d %Y")
        params = {
            "searchDate": date_str,
            "holes": "18",
            "numberOfPlayer": "0",
            "courseIds": COURSE_IDS,
            "searchTimeType": "0",
            "transactionId": txn_id,
            "teeOffTimeMin": "0",
            "teeOffTimeMax": "23",
            "isChangeTeeOffTime": "true",
            "teeSheetSearchView": "5",
            "classCode": "R",
            "defaultOnlineRate": "N",
            "isUseCapacityPricing": "false",
            "memberStoreId": "1",
            "searchType": "1",
        }
        tt_resp = session.get(
            f"{API_BASE}/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes",
            params=params,
            headers=headers,
        )
        if tt_resp.status_code != 200:
            print(
                f"  {target_date.strftime('%A %b %d')}: "
                f"API error {tt_resp.status_code} - {tt_resp.text[:200]}"
            )
            continue

        data = tt_resp.json()
        content = data.get("content", [])
        times = parse_tee_times(content, target_date)
        print(f"  {target_date.strftime('%A %b %d')}: {len(times)} tee times")
        all_tee_times.extend(times)

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

    subject = "Golf Tee Time Alert: {} new time(s) available!".format(len(new_times))

    lines = ["New tee times on Golf Vancouver (likely cancellations):\n"]

    by_date = {}
    for tt in new_times:
        key = "{} {}".format(tt["day"], tt["date"])
        by_date.setdefault(key, []).append(tt)

    for date_label, times in sorted(by_date.items()):
        lines.append("\n--- {} ---".format(date_label))
        for tt in sorted(times, key=lambda x: x["time"]):
            lines.append(
                "  {:>8s}  {:<30s}  {}H  {}  {}".format(
                    tt["time"], tt["course"], tt["holes"], tt["players"], tt["price"]
                )
            )

    lines.append("\nBook now: {}".format(BASE_URL))

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print("Email sent to {} with {} new tee time(s).".format(EMAIL_TO, len(new_times)))


def main():
    print("[{}] Starting tee time check...".format(datetime.now().isoformat()))

    target_dates = get_target_dates()
    print("Checking {} dates...\n".format(len(target_dates)))

    tee_times = fetch_tee_times(target_dates)
    print("\nFound {} total tee times.".format(len(tee_times)))

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
        print("\n*** {} NEW tee time(s) detected! ***".format(len(new_times)))
        send_email(new_times)
    else:
        print("No new tee times since last check.")

    all_keys = known_keys | current_keys
    today_str = datetime.now().strftime("%Y-%m-%d")
    pruned = {k for k in all_keys if k.split("|")[0] >= today_str}
    save_known_times(pruned)
    print("Saved {} known tee time keys.".format(len(pruned)))


if __name__ == "__main__":
    main()
