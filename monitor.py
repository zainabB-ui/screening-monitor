#!/usr/bin/env python3
"""
Monitors an Advance Screenings city page for new screening postings
and emails an alert via Gmail SMTP when the listings section changes.

Designed to run from GitHub Actions on a schedule. State (the last
seen content) is persisted to state.txt and committed back to the repo
by the workflow, so each run compares against the previous run's snapshot.
"""

import os
import sys
import smtplib
import difflib
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

URL = "https://www.advancescreenings.com/city/us/dc/washington"
STATE_FILE = "state.txt"

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_USER)


def fetch_page(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ScreeningMonitor/1.0)"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def extract_screenings_section(html: str) -> str:
    """
    Isolates the part of the page that lists screenings (or the
    'No screenings found' message), stripping nav/footer/ads/blog
    links so unrelated site changes don't trigger false alerts.

    Heuristic: the screenings content sits between the city heading
    and the 'Upcoming Movies' block. If that marker isn't found
    (e.g. the site redesigns), it falls back to the full page text
    so you still get notified rather than silently missing changes.
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
    if start_idx != -1:
        start_idx = full_text.find(start_marker, start_idx + 1)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        section = full_text[start_idx:end_idx].strip()
    else:
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
    html = fetch_page(URL)
    current_section = extract_screenings_section(html)
    previous_section = load_previous_state()

    if not previous_section:
        # First run ever: just establish baseline, no alert.
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
