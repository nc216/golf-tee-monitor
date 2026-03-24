# Golf Vancouver Tee Time Monitor

Monitors [Golf Vancouver's booking page](https://golfvancouver.cps.golf/onlineresweb/search-teetime?TeeOffTimeMin=0&TeeOffTimeMax=23.999722222222225) for newly available weekend tee times (cancellations) across Fraserview, McCleery, and Langara. Sends an email alert when new slots appear.

## How it works

1. A GitHub Actions cron job runs every 15 minutes during waking hours (6 AM - 10 PM PT).
2. Playwright (headless Chromium) loads the booking page and captures tee time data.
3. New tee times are compared against a cached list of previously seen times.
4. If new times appear (someone cancelled), an email notification is sent.

## Setup

### 1. Fork/clone this repo

### 2. Create a Gmail App Password

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable 2-Factor Authentication if not already on
3. Go to App Passwords and generate one for "Mail"

### 3. Add GitHub Secrets

In your repo, go to **Settings > Secrets and variables > Actions** and add:

| Secret | Value |
|---|---|
| `EMAIL_FROM` | Your Gmail address (the sender) |
| `EMAIL_PASSWORD` | The App Password from step 2 |
| `EMAIL_TO` | `noahcastelo@gmail.com` (or any recipient) |

### 4. Enable Actions

The workflow runs automatically on schedule. You can also trigger it manually from the Actions tab.

## Local testing

```bash
pip install -r requirements.txt
playwright install chromium

# Set env vars
export EMAIL_FROM="you@gmail.com"
export EMAIL_PASSWORD="your-app-password"
export EMAIL_TO="noahcastelo@gmail.com"

python scraper.py
```

## Customization

- **Days to monitor**: Edit `is_weekend()` in `scraper.py` to change from weekends to other days.
- **Check frequency**: Edit the cron schedule in `.github/workflows/check-tee-times.yml`.
- **Courses**: The scraper checks all three courses by default.

## Debugging

Each run uploads a screenshot (`debug/page.png`) and HTML dump (`debug/page.html`) as build artifacts, retained for 3 days.
