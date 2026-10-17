"""Demo helper: approve a PayPal **sandbox** checkout in headless Chrome as a guest,
paying with PayPal's public sandbox test card, so the real redirect → capture flow runs end to end.

    python -m agent.approve_sandbox "<approve_url>" [--screens DIR]

Refuses anything that is not www.sandbox.paypal.com. Fake money only; no real account is used.
"""
from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

CARD = {"cardNumber": "4032039317984658", "cardExpiry": "12/30", "cardCvv": "123"}
PERSON = {"firstName": "Sandbox", "lastName": "Tester", "billingLine1": "1 Market St",
          "billingCity": "San Francisco", "billingPostalCode": "94105", "phone": "4155550100",
          "email": "sandbox.buyer@example.com"}


def approve(url: str, return_prefix: str, screens: str | None = None, chrome="/usr/bin/google-chrome") -> str:
    if urlparse(url).hostname != "www.sandbox.paypal.com":
        raise SystemExit("refusing: not a PayPal sandbox URL")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome, headless=True, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1280, "height": 1000}, locale="en-US")
        pg.goto(url, wait_until="networkidle", timeout=60000)
        if screens:
            pg.screenshot(path=f"{screens}/paypal_1_login.png")
        pg.click("#createAccount")  # "Pay with Debit or Credit Card" (guest)
        pg.wait_for_selector("#cardNumber", timeout=30000)
        try:
            pg.select_option("#country", "US")
            pg.wait_for_timeout(1500)
        except Exception:
            pass
        for k, v in (CARD | PERSON).items():
            pg.fill(f"#{k}", v)
        pg.select_option("#billingState", "CA")
        if screens:
            pg.screenshot(path=f"{screens}/paypal_2_guest_card.png", full_page=True)
        # Pay as guest: never use "Create Account & Pay Now"
        pg.get_by_text("Pay now as guest", exact=True).click()
        try:
            pg.wait_for_url(lambda u: u.startswith(return_prefix), timeout=60000)
        except Exception:
            pg.screenshot(path=f"{screens or '/tmp'}/paypal_error.png", full_page=True)
            raise SystemExit("PayPal did not redirect back; page text: " + pg.inner_text("body")[:600].replace("\n", " | "))
        final = pg.url
        pg.wait_for_load_state("networkidle")
        if screens:
            pg.screenshot(path=f"{screens}/paypal_3_returned.png")
        b.close()
        return final


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--return-prefix", default="http://localhost")
    ap.add_argument("--screens")
    a = ap.parse_args()
    print(approve(a.url, a.return_prefix, a.screens))
    sys.exit(0)
