#!/usr/bin/env python3
"""Serve the Carmella site and store waitlist signups.

Emails are saved to data/waitlist.jsonl. Optional delivery:

  RESEND_API_KEY   Resend key. Sends you a note for each new signup.
  NOTIFY_EMAIL     Inbox that should receive those notes.
  NOTIFY_FROM      From address Resend will accept. Defaults to
                   Carmella <onboarding@resend.dev> (Resend test sender).
  WEBHOOK_URL      POST each new signup as JSON (Zapier, Make, etc.).
  ADMIN_TOKEN      If set, GET /api/waitlist.csv?token=... downloads the list.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA = ROOT / "data"
STORE = DATA / "waitlist.jsonl"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[a-z]{2,}$", re.I)
LOCK = threading.Lock()
HITS = {}

def load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_signups():
    if not STORE.exists():
        return []
    rows = []
    for line in STORE.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def allow(ip):
    window = 3600
    now = time.time()
    with LOCK:
        recent = [t for t in HITS.get(ip, []) if now - t < window]
        if len(recent) >= 8:
            HITS[ip] = recent
            return False
        recent.append(now)
        HITS[ip] = recent
        return True


def notify(row):
    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if webhook:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(row).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as err:
            print("webhook failed:", err)

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("NOTIFY_EMAIL", "").strip()
    if not api_key or not to:
        return
    sender = os.environ.get("NOTIFY_FROM", "Carmella <onboarding@resend.dev>").strip()
    payload = {
        "from": sender,
        "to": [to],
        "subject": "New Carmella waitlist signup",
        "text": (
            f"{row['email']} joined the Carmella waitlist.\n\n"
            f"Time: {row['created_at']}\n"
            f"Source: {row['source']}\n"
            f"Referrer: {row['referrer'] or '—'}\n"
            f"UTM: {row['utm_source'] or '—'} / {row['utm_medium'] or '—'} / {row['utm_campaign'] or '—'}\n"
        ),
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as err:
        print("resend failed:", err)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/waitlist.csv":
            self.export_csv()
            return
        if path.startswith("/api/"):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/waitlist":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8000:
            self.send_json(400, {"status": "error", "error": "invalid_body"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode())
        except Exception:
            self.send_json(400, {"status": "error", "error": "invalid_json"})
            return
        email = str(body.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            self.send_json(400, {"status": "invalid"})
            return
        ip = self.client_address[0]
        if not allow(ip):
            self.send_json(429, {"status": "error", "error": "rate_limited"})
            return

        row = {
            "email": email,
            "created_at": now_iso(),
            "source": str(body.get("source") or "direct")[:80],
            "utm_source": str(body.get("utm_source") or "")[:120],
            "utm_medium": str(body.get("utm_medium") or "")[:120],
            "utm_campaign": str(body.get("utm_campaign") or "")[:120],
            "referrer": str(body.get("referrer") or "")[:300],
            "consent_version": str(body.get("consent_version") or "")[:40],
        }
        duplicate = False
        with LOCK:
            DATA.mkdir(exist_ok=True)
            existing = {item["email"] for item in read_signups()}
            if email in existing:
                duplicate = True
            else:
                with STORE.open("a") as handle:
                    handle.write(json.dumps(row) + "\n")
        if duplicate:
            self.send_json(200, {"status": "duplicate"})
            return
        threading.Thread(target=notify, args=(row,), daemon=True).start()
        self.send_json(200, {"status": "success"})

    def export_csv(self):
        token = os.environ.get("ADMIN_TOKEN", "").strip()
        supplied = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
            for part in query.split("&"):
                if part.startswith("token="):
                    supplied = urllib.parse.unquote(part[6:])
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not token or supplied != token:
            self.send_error(404)
            return
        lines = ["email,created_at,source,utm_source,utm_medium,utm_campaign,referrer"]
        for row in read_signups():
            lines.append(",".join(csv_cell(row.get(key, "")) for key in [
                "email", "created_at", "source", "utm_source", "utm_medium", "utm_campaign", "referrer"
            ]))
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=carmella-waitlist.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if str(args[0]).startswith("GET /assets") or str(args[0]).startswith("GET /js"):
            return
        super().log_message(fmt, *args)


def csv_cell(value):
    text = str(value).replace('"', '""')
    if any(ch in text for ch in ",\"\n"):
        return f'"{text}"'
    return text


def main():
    load_env()
    port = int(os.environ.get("PORT", "4321"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Carmella is running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
