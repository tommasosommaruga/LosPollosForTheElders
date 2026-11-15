#!/usr/bin/env python3
"""One-time OAuth helper: print a Google refresh token for Medicine Buddy.

Prereqs
  1. Google Cloud Console → APIs & Services → enable "Google Calendar API"
  2. Create OAuth client (Desktop app) → copy client id + secret
  3. Add authorized redirect URI: http://127.0.0.1:8765/
  4. Run this script, approve in the browser

Then put into .env:

  MEDICINE_BUDDY_GOOGLE_CLIENT_ID=...
  MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET=...
  MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN=...
  MEDICINE_BUDDY_GOOGLE_CALENDAR_ID=...   # or set on Family page

Calendar ID: Google Calendar → Settings → the calendar (e.g. "Tommaso test")
→ Integrate calendar → Calendar ID.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

# events create/cancel + calendar metadata / list (Test Google uses events.list).
SCOPE = " ".join(
    [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://127.0.0.1:8765/"
LISTEN_PORT = 8765


def main() -> int:
    print("Google Calendar OAuth for Medicine Buddy\n")
    client_id = input("OAuth client id: ").strip()
    client_secret = input("OAuth client secret: ").strip()
    if not client_id or not client_secret:
        print("Need both client id and secret.", file=sys.stderr)
        return 1

    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    auth_link = f"{AUTH_URL}?{params}"
    print("\nOpening browser for Google consent…")
    print(f"(If it does not open: {auth_link})\n")
    webbrowser.open(auth_link)

    code_holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("code"):
                code_holder["code"] = qs["code"][0]
                body = b"<html><body><h1>OK - return to the terminal.</h1></body></html>"
                self.send_response(200)
            else:
                err = qs.get("error", ["unknown"])[0]
                code_holder["error"] = err
                body = f"<html><body><h1>Error: {err}</h1></body></html>".encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

    server = HTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    print(f"Waiting for Google redirect on {REDIRECT_URI} …")
    while "code" not in code_holder and "error" not in code_holder:
        server.handle_request()
    server.server_close()
    if "error" in code_holder:
        print(f"OAuth error: {code_holder['error']}", file=sys.stderr)
        return 1
    code = code_holder["code"]

    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        return 1

    refresh = str(payload.get("refresh_token") or "").strip()
    if not refresh:
        print(
            "No refresh_token in response. Revoke prior access for this client and retry.",
            file=sys.stderr,
        )
        print(json.dumps(payload, indent=2))
        return 1

    print("\nAdd these to your .env:\n")
    print(f"MEDICINE_BUDDY_GOOGLE_CLIENT_ID={client_id}")
    print(f"MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN={refresh}")
    print("# MEDICINE_BUDDY_GOOGLE_CALENDAR_ID=paste-from-google-calendar-settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
