"""Register (or find) the PayPal sandbox webhook for this deployment and print its id.

    PAYPAL_MODE=sandbox PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... \
    python -m scripts.register_webhook https://your-app.onrender.com

Then set PAYPAL_WEBHOOK_ID to the printed id on the server (used to verify every webhook signature).
"""
import sys

from app.config import Settings
from app.paypal import PayPalClient


def main(base: str) -> str:
    s = Settings()
    if s.paypal_mode != "sandbox":
        raise SystemExit("set PAYPAL_MODE=sandbox and the sandbox client id/secret")
    pp = PayPalClient(s)
    url = base.rstrip("/") + "/paypal/webhook"
    for w in pp.list_webhooks():
        if w.get("url") == url:
            return w["id"]
    return pp.create_webhook(url)["id"]


if __name__ == "__main__":
    print(main(sys.argv[1]))
