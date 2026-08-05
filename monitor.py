#!/usr/bin/env python3
"""
Monitors the Advance Screenings Washington, DC city page for new screening
postings and emails an alert via Gmail SMTP when the listings table changes.

IMPORTANT: This page renders its listings via client-side JavaScript, so a
plain requests.get() only sees a static shell (often a stale "No screenings
found" placeholder) even when real screenings exist. This script uses
Playwright (headless Chromium) to fully render the page before reading it.

DEBUGGING: Every run saves debug_screenshot.png and debug_page.html as
workflow artifacts (see check_screenings.yml) so you can visually confirm
whether Playwright is seeing real listings, a placeholder, or a bot-block
page. Check these first if you suspect the script isn't picking up listings.

Designed to run from GitHub Actions on a schedule. State (the last seen
content) is persisted to state.txt and committed back to the repo by the
workflow, so each run compares against the previous run's snapshot.
"""

import os
import sys
import smtplib
import difflib
from email.message import EmailMessage

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

URL = "https://www.advancescreenings.com/city/us/dc/washington"
STATE_FILE = "state.txt"
DEBUG_SCREENSHOT = "debug_screenshot.png"
DEBUG_HTML = "debug_page.html"

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_USER)


def fetch_rendered_html(url: str) -> str:
    """Loads the page in headless Chromium, waits for JS-populated content,
    and saves a screenshot + raw HTML for debugging before returning the
    fully rendered HTML."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )

        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            print(f"WARNING: networkidle wait failed or timed out: {e}")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Extra buffer for slow/late XHR calls that populate the table.
        page.wait_for_timeout(5000)

        page_title = page.title()
        print(f"Page title after render: {page_title!r}")

        page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
        html = page.content()

        with open(DEBUG_HTML, "w", encoding="utf-8") as f:
            f.write(html)

        browser.close()
        return html


def extract_screenings_section(html: str) -> str:
    """
    Isolates the listings table for Washington, DC, stripping nav/footer/
    ads/blog links so unrelated site changes don't trigger false alerts.
    Falls back to full page text if the expected structure isn't found.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    full_text = "\n".join(lines)

    end_marker = "Upcoming Movies"
    end_idx = full_text.find(end_marker)

    start_marker = "Washington, DC"
    start_idx = full_text.find(start_marker)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        section = full_text[start_idx:end_idx].strip()
    else:
        print("WARNING: Could not locate expected section markers; using full page text.")
        section = full_text

    return section


def load_previous_state() -> str:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def save_state(content: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def send_email(subject: str, body: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and GMAIL_TO):
        print("Missing GMAIL_USER, GMAIL_APP_PASSWORD, or GMAIL_TO env vars; skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_TO
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

    print("Email sent.")


def build_diff(old: str, new: str) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = difflib.unified_diff(
        old_lines, new_lines, lineterm="", fromfile="previous", tofile="current"
    )
    diff_text = "\n".join(diff)
    return diff_text if diff_text else "(No previous snapshot; this is the first run.)"


def main() -> int:
    html = fetch_rendered_html(URL)
    current_section = extract_screenings_section(html)

    print("===== CAPTURED CONTENT (first 2000 chars) =====")
    print(current_section[:2000])
    print("===== END CAPTURED CONTENT =====")

    previous_section = load_previous_state()

    if not previous_section:
        save_state(current_section)
        print("Baseline snapshot saved. No email sent on first run.")
        return 0

    if current_section.strip() != previous_section.strip():
        diff_text = build_diff(previous_section, current_section)
        body = (
            f"The Advance Screenings page for Washington, DC appears to have changed.\n\n"
            f"URL: {URL}\n\n"
            f"--- Current listings section ---\n{current_section}\n\n"
            f"--- What changed (diff) ---\n{diff_text}\n"
        )
        send_email("New screening posted: Washington, DC (Advance Screenings)", body)
        save_state(current_section)
        print("Change detected, email sent, state updated.")
    else:
        print("No change detected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
