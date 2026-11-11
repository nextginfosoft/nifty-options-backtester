"""
Generate the daily Zerodha Kite Connect access token and store it in .env.local.

Run once each trading morning (Kite tokens expire ~06:00 IST the next day):

    python scripts/generate_kite_token.py

Needs KITE_API_KEY and KITE_API_SECRET in .env.local (you add them yourself; this
script never prints them). Steps: open the printed login URL, sign in to Zerodha,
then paste the full redirect URL (or just its request_token) back here.
Only KITE_ACCESS_TOKEN is written.
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.env_loader import load_env  # noqa: E402

ENV_LOCAL = ROOT / ".env.local"


def _extract_request_token(text: str) -> str:
    text = text.strip()
    if "request_token=" in text:
        return parse_qs(urlparse(text).query).get("request_token", [""])[0]
    return text


def _write_env_value(key: str, value: str) -> None:
    lines = ENV_LOCAL.read_text(encoding="utf-8").splitlines() if ENV_LOCAL.exists() else []
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_LOCAL.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    load_env()
    api_key = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    if not api_key or not api_secret:
        print("Set KITE_API_KEY and KITE_API_SECRET in .env.local first "
              "(see .env.local.template).")
        return 1

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    print("1. Open this URL, log in to Zerodha and authorise the app:\n")
    print(f"   {kite.login_url()}\n")
    print("2. You will be redirected to your app's redirect URL. Copy the FULL URL "
          "from the address bar (it contains request_token=...).")
    token = _extract_request_token(input("3. Paste it here: "))
    if not token:
        print("No request_token found.")
        return 1
    try:
        session = kite.generate_session(token, api_secret=api_secret)
    except Exception as exc:  # noqa: BLE001
        print(f"Token exchange failed: {exc.__class__.__name__}: {exc}")
        return 1
    _write_env_value("KITE_ACCESS_TOKEN", session["access_token"])
    print("KITE_ACCESS_TOKEN saved to .env.local (valid for today's session).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
