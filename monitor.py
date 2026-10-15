import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://cal.mofa.gov.iq/12220"
STATE_FILE = Path("monitor_state.json")


def send_email(subject, body):
    address = os.environ["GMAIL_ADDRESS"].strip()
    password = "".join(
        os.environ["GMAIL_APP_PASSWORD"].split()
    )

    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = subject
    message.set_content(body + "\n\n" + URL)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        context=ssl.create_default_context(),
        timeout=30,
    ) as server:
        server.login(address, password)
        server.send_message(message)


def inspect_page(page):
    text = page.locator("body").inner_text().lower()

    if "frankfurt" not in text:
        raise RuntimeError("Booking page did not load")

    no_slots = (
        "aucune disponibilité à venir",
        "aucun créneau horaire disponible",
        "no upcoming availability",
        "no available slots",
    )

    if any(phrase in text for phrase in no_slots):
        return "NO_SLOTS"

    if page.get_by_role("grid").count() == 0:
        raise RuntimeError("Calendar missing")

    return "CHECK_PAGE"


def check_calendar():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="fr-FR")
            response = page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            if response and response.status >= 400:
                raise RuntimeError("Website returned an HTTP error")

            page.wait_for_timeout(15000)
            result = inspect_page(page)

            if result == "CHECK_PAGE":
                page.wait_for_timeout(8000)
                result = inspect_page(page)

            return result
        finally:
            browser.close()


def main():
    if STATE_FILE.exists():
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    else:
        state = {
            "initialized": False,
            "alert_sent": False,
            "failures": 0,
            "error_sent": False,
        }

    try:
        result = check_calendar()
    except Exception as error:
        state["failures"] = state.get("failures", 0) + 1
        print("CHECK FAILED:", type(error).__name__)

        if (
            state["failures"] >= 3
            and not state.get("error_sent", False)
        ):
            send_email(
                "Frankfurt Bot - CHECKING PROBLEM",
                "Three consecutive cloud checks failed. "
                "Appointment availability is unknown. "
                "Please check the GitHub Actions run.",
            )
            state["error_sent"] = True

        STATE_FILE.write_text(
            json.dumps(state, indent=2),
            encoding="utf-8",
        )
        raise SystemExit(1)

    now = datetime.now(timezone.utc).isoformat()
    print(now, result)

    if not state.get("initialized", False):
        send_email(
            "Frankfurt Bot - CLOUD CHECK SUCCESSFUL",
            "The first cloud check succeeded.\n"
            "Result: " + result + "\n\n"
            "This message alone does not confirm an appointment "
            "or verify that scheduled checks are running.",
        )
        state["initialized"] = True

    if result == "NO_SLOTS":
        state["alert_sent"] = False
    elif not state.get("alert_sent", False):
        send_email(
            "Frankfurt Bot - CHECK APPOINTMENTS",
            "The calendar loaded, but its usual no-availability "
            "message was not found on two checks.\n\n"
            "An appointment may have opened, or the page may "
            "have changed. Open the website and verify manually. "
            "No appointment has been booked.",
        )
        state["alert_sent"] = True

    state["failures"] = 0
    state["error_sent"] = False

    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
