#!/usr/bin/env python3
"""
Monitors the Advance Screenings Washington, DC city page for new screening
postings and emails an alert via Gmail SMTP when the listings change.

HISTORY OF FIXES:
1. Plain requests.get() missed JS-rendered content -> switched to Playwright.
2. BeautifulSoup HTML parsing missed dynamic "Added" labels (e.g. "yesterday")
   -> switched to Playwright's own rendered innerText.
3. innerText flattened tables into a single squished text stream that was
   hard to read in the email (columns ran together) -> this version queries
   the actual <table> elements in the DOM directly, extracting each row as
   separate cell values (Screening Type, City, Outlet, Added), and sends a
   real HTML table in the email so columns stay aligned and readable.

State is stored as structured JSON (not raw text) so change-detection is
based on actual row content, not on formatting/whitespace differences.

Designed to run from GitHub Actions on a schedule. State is committed back
to the repo by the workflow, so each run compares against the previous run.
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

# JS run inside the browser to pull out each table as structured rows,
# along with the nearest preceding heading/movie-title text for context.
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


def normalize_for_diff(tables) -> str:
    """Canonical JSON representation used purely for change detection,
    independent of any display formatting."""
    return json.dumps(tables, sort_keys=True, indent=2)


def load_previous_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(tables) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(tables, f, indent=2)


def build_html_email(tables) -> str:
    parts = ["<html><body style='font-family: Arial, sans-serif;'>"]
    parts.append(f"<p>The Advance Screenings page for Washington, DC has new content:</p>")
    parts.append(f"<p><a href='{URL}'>{URL}</a></p>")

    for tbl in tables:
        if tbl.get("heading"):
            parts.append(f"<h3 style='margin-top:20px;'>{tbl['heading']}</h3>")
        rows = tbl["rows"]
        if not rows:
            continue
        parts.append(
            "<table style='border-collapse:collapse; width:100%; margin-bottom:16px;'>"
        )
        for i, row in enumerate(rows):
            tag = "th" if i == 0 else "td"
            style = (
                "border:1px solid #ccc; padding:6px 10px; text-align:left; "
                + ("background:#f2f2f2; font-weight:bold;" if i == 0 else "")
            )
            parts.append("<tr>")
            for cell in row:
                parts.append(f"<{tag} style='{style}'>{cell}</{tag}>")
            parts.append("</tr>")
        parts.append("</table>")

    parts.append("</body></html>")
    return "\n".join(parts)


def build_plain_text_email(tables) -> str:
    """Readable fallback: one clearly labeled block per row instead of a
    squished single line, in case the email client doesn't render HTML."""
    lines = [
        "The Advance Screenings page for Washington, DC has new content:",
        URL,
        "",
    ]
    for tbl in tables:
        if tbl.get("heading"):
            lines.append(f"=== {tbl['heading']} ===")
        rows = tbl["rows"]
        if not rows:
            continue
        headers = rows[0]
        for row in rows[1:]:
            lines.append("-" * 40)
            for header, value in zip(headers, row):
                lines.append(f"{header}: {value}")
        lines.append("")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, plain_body: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and GMAIL_TO):
        print("Missing GMAIL_USER, GMAIL_APP_PASSWORD, or GMAIL_TO env vars; skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_TO
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

    print("Email sent.")


def main() -> int:
    tables = fetch_structured_data(URL)

    print("===== EXTRACTED TABLES (summary) =====")
    for t in tables:
        print(f"Heading: {t.get('heading')!r}, rows: {len(t['rows'])}")
    print("===== END SUMMARY =====")

    current_snapshot = normalize_for_diff(tables)
    previous_tables = load_previous_state()

    if previous_tables is None:
        save_state(tables)
        print("Baseline snapshot saved. No email sent on first run.")
        return 0

    previous_snapshot = normalize_for_diff(previous_tables)

    if current_snapshot != previous_snapshot:
        html_body = build_html_email(tables)
        plain_body = build_plain_text_email(tables)
        send_email(
            "New screening posted: Washington, DC (Advance Screenings)",
            html_body,
            plain_body,
        )
        save_state(tables)
        print("Change detected, email sent, state updated.")
    else:
        print("No change detected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
