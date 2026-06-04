#!/usr/bin/env python3
"""
capture_cookies.py — open admin.confluent.cloud in a browser, let the user log
in, then save the authenticated session cookie into .env as CONFLUENT_COOKIE.

Standalone helper (mirrors Ticket Lens's capture_cookies.py):

    .venv/bin/python capture_cookies.py

It launches a real (headed) Chromium window via Playwright, navigates to
admin.confluent.cloud, waits for you to finish authenticating (including SSO),
then reads the session cookies back out of the browser and writes them to .env.

The app reads CONFLUENT_COOKIE from .env on startup (load_dotenv), so a restart
picks up whatever this writes — no extra wiring needed.
"""
import json
import sys
import time
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"
ENV_VAR = "CONFLUENT_COOKIE"

# admin.confluent.cloud and confluent.cloud share cookies on the .confluent.cloud
# parent domain. The app talks to https://confluent.cloud/api/internal/… , so we
# collect every cookie that applies to a *.confluent.cloud host.
LOGIN_URL = "https://admin.confluent.cloud/"
COOKIE_DOMAIN_SUFFIX = "confluent.cloud"

# The marker that proves an *authenticated* Confluent Cloud session: a JWT in the
# `internal_auth_token` cookie. Anonymous visitors don't have it. (Same token the
# app's _confluent_cookie_status / _INTERNAL_AUTH_TOKEN_RE keys off.)
AUTH_COOKIE_NAME = "internal_auth_token"


def _read_env_var(name: str) -> str:
    """Read a single VAR=value from .env (last wins), unquoted. Returns ''."""
    val = ""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    return val


def _decode_jwt_payload(token: str):
    """Decode a JWT payload *without* verifying the signature — only to inspect
    the `exp` claim. Never trust it for authorization. Mirrors app.py."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        pad = (-len(payload_b64)) % 4
        if pad:
            payload_b64 += "=" * pad
        import base64
        decoded = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
        return json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _token_expiry(token: str):
    """Return the `exp` (epoch seconds) from a JWT, or None if undecodable."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def _cookie_header(cookies, suffix: str) -> str:
    """Build a `name=value; …` header from the cookies that apply to the
    Confluent Cloud host. Domain cookies are stored with a leading dot, so match
    on suffix."""
    parts = []
    seen = set()
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if not domain.endswith(suffix):
            continue
        name = c.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def _authenticated_token(cookies):
    """Return the internal_auth_token value if a *valid, unexpired* one is
    present in the browser's cookie jar, else None."""
    for c in cookies:
        if c.get("name") != AUTH_COOKIE_NAME:
            continue
        token = c.get("value") or ""
        exp = _token_expiry(token)
        if exp and exp > int(time.time()):
            return token
    return None


def _write_env(value: str) -> None:
    """Upsert CONFLUENT_COOKIE in .env, preserving every other line."""
    line = f"{ENV_VAR}={value}"
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
        for i, existing in enumerate(lines):
            if existing.strip().startswith(ENV_VAR + "="):
                lines[i] = line
                break
        else:
            lines.append(line)
        ENV_PATH.write_text("\n".join(lines) + "\n")
    else:
        ENV_PATH.write_text(line + "\n")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print(
            "Playwright isn't installed. Install it into the venv:\n"
            "    .venv/bin/pip install playwright\n"
            "    .venv/bin/playwright install chromium\n",
            file=sys.stderr,
        )
        return 1

    print(f"\nOpening {LOGIN_URL} …")
    print("A browser window will appear. Just log in (SSO included).")
    print("As soon as you're authenticated I'll grab the cookie and close it.\n")

    cookie = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # navigation can race with SSO redirects
            print(f"(navigation note: {exc})")

        # Wait for *real* authentication. The internal_auth_token JWT is only set
        # once you're signed in, so we poll the browser's cookie jar until one
        # appears with a future `exp` — exactly how the app decides a session is
        # live (see _confluent_cookie_status in app.py).
        POLL_INTERVAL = 1.5   # seconds between checks
        WAIT_TIMEOUT = 300.0  # 5 min to finish logging in
        deadline = time.monotonic() + WAIT_TIMEOUT
        print("Waiting for you to finish logging in…")
        try:
            while True:
                jar = context.cookies()
                if _authenticated_token(jar):
                    cookie = _cookie_header(jar, COOKIE_DOMAIN_SUFFIX)
                    break
                if time.monotonic() >= deadline:
                    print("\nTimed out waiting for login — nothing written.")
                    browser.close()
                    return 1
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nCancelled — nothing written.")
            browser.close()
            return 1

        browser.close()

    # Sanity check: the captured cookie must carry the auth token, matching what
    # the app's _confluent_cookie_status expects.
    if f"{AUTH_COOKIE_NAME}=" not in cookie:
        print(
            f"\nAuthenticated, but the captured cookie has no {AUTH_COOKIE_NAME} "
            "marker — not writing .env. Please report this.",
            file=sys.stderr,
        )
        return 1

    _write_env(cookie)
    print(f"\n✔ Authenticated — saved {ENV_VAR} to {ENV_PATH} ({len(cookie)} chars).")
    print("  Browser closed. Restart the app to pick it up.")
    return 0


def check_cookie() -> int:
    """Validate the CONFLUENT_COOKIE currently in .env, without a browser.

    Decodes the embedded internal_auth_token JWT's `exp` claim — the same offline
    check the app's home page uses. No network call (so it never returns 2; the
    code is kept for run.sh parity with the Ticket Lens flow).
    """
    cookie = _read_env_var(ENV_VAR)
    if not cookie:
        print(f"✘ {ENV_VAR} is not set in {ENV_PATH}. Run this script (no flag) to capture one.")
        return 1

    import re
    m = re.search(r"internal_auth_token=([^;\s]+)", cookie)
    if not m:
        print(f"✘ {ENV_VAR} has no {AUTH_COOKIE_NAME} marker — likely malformed.")
        return 1

    exp = _token_expiry(m.group(1))
    if not exp:
        print(f"✘ {ENV_VAR} contains a token that can't be decoded — re-capture it.")
        return 1

    now = int(time.time())
    if exp <= now:
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
        print(f"✘ Cookie expired {when:%b %d, %Y %I:%M %p %Z} — re-capture it.")
        return 1

    mins = (exp - now) // 60
    human = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins} min"
    print(f"✔ Cookie is valid — Confluent Cloud session expires in {human}.")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check_cookie())
    raise SystemExit(main())
