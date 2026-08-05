#!/usr/bin/env python3
"""
Monitors the Advance Screenings Washington, DC city page for new screening
postings and emails a plain-text alert via Gmail SMTP when a NEW screening
row actually appears.

Combines fixes from all prior versions:
1. Uses Playwright to render JS-driven content (plain requests.get() misses it).
2. Extracts actual <table> rows as structured data (Screening Type, City,
   Outlet, Added) instead of flattening to raw text, so nothing gets lost
   or squished together.
3. Emails plain text only, formatted as one labeled block per row -- no
   HTML table, no single-column squish.
4. Change detection ignores the "Added" column and row order when deciding
   whether to send an email, since "3 days ago" ticking forward to "4 days
   ago" is not a new posting and was causing false-positive emails (and,
   as a side effect, unnecessary git pushes that were colliding with each
   other). The "Added" value is still shown in the email for reference --
   it's just not used to decide whether something is "new."

A row counts as new if its (Screening Type, City, Outlet, Movie) combo
was not present in the previous run at all.
"""

import os
import sys
import json
import smtplib
from email.message import EmailMessage

from playwright.sync_api import sync_playwright

URL = "https://www.advancescreenings.com/city/us/dc/washington"
STATE_FILE = "state.json"
DEBUG_SCREENSHOT = "debug_screenshot.png"
DEBUG_HTML = "debug_page.html"
DEBUG_TABLES_JSON = "debug_tables.json"

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
GMAIL_TO = os.environ.get("GMAIL_TO", GMAIL_USER)

EXTRACT_TABLES_JS = """
() => {
  function precedingHeading(el) {
    let node = el;
    while (node && node !== document.body) {
      let sib = node.previousElementSibling;
      while (sib) {
        if (/^H[1-6]$/.test(sib.tagName)) {
          return sib.innerText.trim();
        }
        const h = sib.querySelector && sib.querySelector('h1,h2,h3,h4,h5,h6');
        if (h) return h.innerText.trim();
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return null;
  }

  const tables = Array.from(document.querySelectorAll('table'));
  return tables.map(t => {
    const rows = Array.from(t.querySelectorAll('tr')).map(tr =>
      Array.from(tr.querySelectorAll('th,td')).map(cell => cell.innerText.trim())
    ).filter(row => row.length > 0);
    return { heading: precedingHeading(t), rows: rows };
  }).filter(tbl => tbl.rows.length > 0);
}
"""


def fetch_structured_data(url: str):
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

        page.wait_for_timeout(5000)
        print(f"Page title after render: {page.title()!r}")

        page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
        with open(DEBUG_HTML, "w", encoding="utf-8") as f:
            f.write(page.content())

        tables = page.evaluate(EXTRACT_TABLES_JS)
        with open(DEBUG_TABLES_JSON, "w", encoding="utf-8") as f:
            json.dump(tables, f, indent=2)

        browser.close()
        return tables


def flatten_rows(tables):
    """Turns the nested table structure into a flat list of row dicts,
    tagging each with its table heading (usually the movie title) and
    mapping cells to their header names when possible."""
    flat = []
    for tbl in tables:
        heading = tbl.get("heading")
        rows = tbl["rows"]
        if not rows:
            continue
        headers = rows[0]
        for row in rows[1:]:
            row_dict = dict(zip(headers, row))
            row_dict["_movie"] = heading
            flat.append(row_dict)
    return flat


def identity_key(row: dict) -> str:
    """Stable identity for a row, deliberately EXCLUDING the 'Added'
    field so date drift alone never makes a row look 'new'."""
    parts = [
        row.get("_movie", ""),
        row.get("Screening Type", ""),
        row.get("City", ""),
        row.get("Outlet", ""),
    ]
    return " | ".join(p.strip() for p in parts)


def load_previous_rows():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_rows(rows) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, sort_keys=True)


def build_plain_text_email(new_rows) -> str:
    lines = [
        "New screening(s) found for Washington, DC:",
        URL,
        "",
    ]
    for row in new_rows:
        lines.append("-" * 40)
        if row.get("_movie"):
            lines.append(f"Movie: {row['_movie']}")
        for key, value in row.items():
            if key == "_movie":
                continue
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def send_email(subject: str, plain_body: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and GMAIL_TO):
        print("Missing GMAIL_USER, GMAIL_APP_PASSWORD, or GMAIL_TO env vars; skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_TO
    msg.set_content(plain_body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

    print("Email sent.")


def main() -> int:
    tables = fetch_structured_data(URL)
    current_rows = flatten_rows(tables)

    print(f"Extracted {len(current_rows)} row(s) from {len(tables)} table(s).")

    previous_rows = load_previous_rows()

    if previous_rows is None:
        save_rows(current_rows)
        print("Baseline snapshot saved. No email sent on first run.")
        return 0

    previous_keys = {identity_key(r) for r in previous_rows}
    new_rows = [r for r in current_rows if identity_key(r) not in previous_keys]

    if new_rows:
        body = build_plain_text_email(new_rows)
        send_email(
            f"{len(new_rows)} new screening(s): Washington, DC (Advance Screenings)",
            body,
        )
        print(f"{len(new_rows)} new row(s) detected, email sent.")
    else:
        print("No new rows detected (only date labels or ordering may have changed).")

    # Always resave current rows so "Added" drift and reordering don't
    # accumulate into an ever-growing diff, and so removed rows drop out.
    save_rows(current_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
