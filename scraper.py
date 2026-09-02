"""
Golf Vancouver Tee Time Monitor

Uses Playwright to load the CPS Golf booking page (bypassing Cloudflare
Turnstile), then calls the TeeTimes API directly from the browser context
to fetch available tee times. Detects newly appeared times (cancellations)
and sends an email notification.
"""

import json
import os
import smtplib
import uuid
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

STEALTH_SCRIPTS = [
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});",
    """window.chrome = {
        runtime: { onConnect: undefined, onMessage: undefined },
        loadTimes: function(){}, csi: function(){},
        app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
    };""",
    """Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ];
            plugins.forEach(p => { p.length = 1; p[0] = {type: 'application/pdf'}; });
            Object.defineProperty(plugins, 'length', {value: 3});
            return plugins;
        },
    });""",
    """const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);""",
    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});",
    "Object.defineProperty(navigator, 'platform', {get: () => 'Linux x86_64'});",
    "Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 4});",
    "Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});",
    "Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});",
    """(() => {
        const origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get;
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() { return origGetter.call(this); }
        });
    })();""",
    """(() => {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Google Inc. (Intel)';
            if (param === 37446) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
            return getParameter.call(this, param);
        };
        if (typeof WebGL2RenderingContext !== 'undefined') {
            const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Google Inc. (Intel)';
                if (param === 37446) return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
                return getParameter2.call(this, param);
            };
        }
    })();""",
]

BASE_URL = (
    "https://golfvancouver.cps.golf/onlineresweb/search-teetime"
    "?TeeOffTimeMin=0&TeeOffTimeMax=23.999722222222225"
)

KNOWN_TIMES_FILE = Path(__file__).parent / "known_tee_times.json"
DEBUG_DIR = Path(__file__).parent / "debug"

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


def save_debug(page, tag: str) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}.html").write_text(page.content())
    except Exception as e:
        print(f"  (failed to save debug snapshot '{tag}': {e})")


def wait_for_turnstile(page, timeout_ms: int = 30000) -> bool:
    import time
    start = time.time()
    deadline = start + timeout_ms / 1000
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        title = page.title().strip()
        body = page.inner_text("body")

        if title not in ("Just a moment...", "") and "Verify you are human" not in body:
            if "Suspicious" not in body:
                print(f"  Turnstile solved (title={title!r})")
                return True

        turnstile_iframe = page.query_selector(
            'iframe[src*="challenges.cloudflare.com"]'
        )
        if turnstile_iframe:
            box = turnstile_iframe.bounding_box()
            if box:
                cx = box["x"] + 35
                cy = box["y"] + 35
                print(f"  Clicking Turnstile iframe at ({cx}, {cy})")
                page.mouse.click(cx, cy)
                page.wait_for_timeout(5000)
                continue

        widget = page.query_selector('.challenge-slot div[style*="grid"]')
        if widget:
            box = widget.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                cx = box["x"] + 35
                cy = box["y"] + box["height"] / 2
                print(f"  Clicking Turnstile widget at ({cx}, {cy}), box={box}")
                page.mouse.click(cx, cy)
                page.wait_for_timeout(5000)
                continue

        if attempt <= 3:
            print(f"  No Turnstile widget found yet (attempt {attempt})")

        page.wait_for_timeout(2000)

    return False


def fetch_tee_times_via_api(context, target_dates: list[datetime]) -> list[dict]:
    """Call the CPS Golf API using context.request (shares browser cookies)."""
    base = "https://golfvancouver.cps.golf"
    txn_id = str(uuid.uuid4())

    api_headers = {
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
    }

    token_resp = context.request.post(
        f"{base}/identityapi/myconnect/token/short",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=(
            "client_id=onlinereswebshortlived&client_secret=v4secret"
            "&grant_type=client_credentials"
            "&scope=onlinereservation references"
        ),
    )
    if not token_resp.ok:
        print(f"  Token request failed: {token_resp.status}")
        return []
    token = token_resp.json()["access_token"]
    print("  Token OK")
    api_headers["Authorization"] = f"Bearer {token}"

    reg_resp = context.request.post(
        f"{base}/onlineres/onlineapi/api/v1/onlinereservation/RegisterTransactionId",
        headers=api_headers,
        data=json.dumps({"transactionId": txn_id}),
    )
    print(f"  Register: {reg_resp.status}")
    if not reg_resp.ok:
        body = reg_resp.text()[:300]
        print(f"  Register failed: {body}")

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
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        resp = context.request.get(
            f"{base}/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes?{qs}",
            headers=api_headers,
        )
        if resp.ok:
            data = resp.json()
            times = parse_tee_times(data.get("content", []), target_date)
            print(f"  {target_date.strftime('%A %b %d')}: {len(times)} tee times")
            all_tee_times.extend(times)
        else:
            body = resp.text()[:300]
            print(f"  {target_date.strftime('%A %b %d')}: API error {resp.status} - {body}")

    return all_tee_times


def scrape_tee_times() -> list[dict]:
    headed = os.environ.get("HEADED", "0") == "1"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-component-update",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Vancouver",
        )
        page = context.new_page()
        for script in STEALTH_SCRIPTS:
            context.add_init_script(script)

        # Navigate to the API path first to trigger Cloudflare challenge
        # and get cf_clearance cookies for the /onlineres/ path.
        # Cloudflare returns the challenge as a 403, which Playwright
        # won't render. Use route() to force the status to 200.
        api_probe_url = (
            "https://golfvancouver.cps.golf/onlineres/onlineapi/api/v1/"
            "onlinereservation/OnlineCourses"
        )
        print("Probing API path for Cloudflare challenge...")

        def log_response(response):
            url = response.url
            if "cdn-cgi" in url or "challenges.cloudflare" in url:
                print(f"  Sub-resource: {response.status} {url[:120]}")

        def log_error(error):
            print(f"  JS error: {error.message[:200]}")

        page.on("response", log_response)
        page.on("pageerror", log_error)

        def force_200(route):
            resp = route.fetch()
            print(f"  route.fetch() status={resp.status}, body_len={len(resp.body())}")
            route.fulfill(response=resp, status=200)

        page.route("**/onlineapi/**", force_200)
        page.goto(api_probe_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        body_text = page.inner_text("body")
        page_title = page.title()
        print(f"  API probe page title: {page_title!r}")
        is_challenge = (
            "Just a moment" in page_title
            or "Verify" in body_text
            or "Verifying" in page_title
            or "Club Prophet" in page_title
        )

        save_debug(page, "api_probe")

        if is_challenge:
            print("Cloudflare challenge on API path, solving...")
            if not wait_for_turnstile(page, timeout_ms=40000):
                print("ERROR: Could not solve Cloudflare challenge on API path.")
                save_debug(page, "api_challenge_failed")
                browser.close()
                return []
            print("API path challenge passed!")
            save_debug(page, "api_challenge_solved")
            page.wait_for_timeout(5000)
        else:
            print("  No challenge detected on API path.")

        cookies = context.cookies()
        cf_cookies = [c for c in cookies if "cf_" in c["name"] or "clearance" in c["name"]]
        print(f"  Cookies after challenge: {[c['name'] + '=' + c['value'][:20] + '...' for c in cf_cookies]}")

        page.unroute("**/onlineapi/**")
        page.remove_listener("response", log_response)
        page.remove_listener("pageerror", log_error)

        # Now load the main page (should pass without challenge since we have cookies)
        print(f"Loading {BASE_URL}")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        body_text = page.inner_text("body")
        page_title = page.title()
        is_challenge = (
            "Just a moment" in page_title
            or "Verify you are human" in body_text
            or "Verifying" in page_title
        )

        if is_challenge:
            print("Cloudflare Turnstile on main page, solving...")
            save_debug(page, "turnstile_before")
            if not wait_for_turnstile(page, timeout_ms=40000):
                print("ERROR: Could not get past Cloudflare Turnstile challenge.")
                save_debug(page, "turnstile_failed")
                browser.close()
                return []
            print("Main page challenge passed!")
            page.wait_for_timeout(3000)

        body_text = page.inner_text("body")
        if "Suspicious" in body_text:
            print("ERROR: CPS Golf bot detection triggered.")
            save_debug(page, "bot_detection")
            browser.close()
            return []

        # Wait for the page to finish loading
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        print("Page loaded, calling API directly...")
        save_debug(page, "initial")

        target_dates = get_target_dates()
        print(f"Checking {len(target_dates)} dates...\n")

        all_tee_times = fetch_tee_times_via_api(context, target_dates)

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

    tee_times = scrape_tee_times()
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
