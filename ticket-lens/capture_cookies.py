#!/usr/bin/env python3
"""
capture_cookies.py — open Zendesk in a browser, let the user log in, then save
the authenticated session cookie into .env as ZENDESK_COOKIE.

First step toward in-app cookie capture. For now it's a standalone helper:

    .venv/bin/python capture_cookies.py

It launches a real (headed) Chromium window via Playwright, navigates to the
Zendesk login, waits for you to finish authenticating (including SSO), then
reads the session cookies back out of the browser and writes them to .env.

Note: the app's startup migration (_migrate_env_cookies_to_db) copies
ZENDESK_COOKIE from .env into its DB on the next launch *if* the DB doesn't
already have one — so this can already feed the app, no extra wiring needed.
"""
import os
import sys
import time
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"
ENV_VAR = "ZENDESK_COOKIE"

# The subdomain the app talks to. Honour ZENDESK_SUBDOMAIN from .env if present,
# else default to confluent.
DEFAULT_SUBDOMAIN = "confluent"

# A Zendesk session cookie is identified by one of these names (see the app's
# _validate_cookie / _ZENDESK_SESSION_RE). Either present == authenticated.
# Note: internal_auth_token is a *Confluent Cloud* marker, NOT Zendesk.
ZENDESK_SESSION_MARKERS = ("_zendesk_session=", "_zendesk_shared_session=")


def _read_env_var(name: str) -> str:
    """Read a single VAR=value from .env (last wins), unquoted. Returns ''."""
    val = ""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    return val


def _read_subdomain() -> str:
    val = _read_env_var("ZENDESK_SUBDOMAIN")
    if val and val != "yourcompany":
        return val
    return DEFAULT_SUBDOMAIN


def _cookie_header(cookies, host: str) -> str:
    """Build a `name=value; …` cookie header from the cookies that apply to the
    Zendesk host. Domain cookies are stored with a leading dot, so match on
    suffix."""
    parts = []
    seen = set()
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if not (host == domain or host.endswith("." + domain) or domain.endswith("zendesk.com")):
            continue
        name = c.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def _authenticated_user(context, base_url: str):
    """Return the logged-in user's display name if /api/v2/users/me.json shows a
    real (non-anonymous) user, else None.

    Uses context.request, which shares the browser context's cookie jar — so as
    the user logs in, this call starts succeeding. Zendesk returns an anonymous
    placeholder (id=None, name="Anonymous user") rather than a 401 when the
    session isn't authenticated, so we key off a non-null user id.
    """
    try:
        resp = context.request.get(
            f"{base_url}/api/v2/users/me.json",
            headers={"Accept": "application/json"},
            timeout=10_000,
        )
    except Exception:
        return None  # transient (mid-SSO redirect, network blip) — keep polling
    if not resp.ok:
        return None
    try:
        user = (resp.json() or {}).get("user") or {}
    except Exception:
        return None
    if not user.get("id"):
        return None
    return (user.get("name") or "").strip() or user.get("email") or "your account"


def _write_env(value: str) -> None:
    """Upsert ZENDESK_COOKIE in .env, preserving every other line."""
    line = f"{ENV_VAR}={value}"
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text().splitlines()
        for i, existing in enumerate(lines):
            if existing.strip().startswith(ENV_VAR + "="):
                lines[i] = line
                break
        else:
            lines.append(line)
        # keep a trailing newline
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

    subdomain = _read_subdomain()
    base_url = f"https://{subdomain}.zendesk.com"
    final_url = base_url + '/agent'
    host = f"{subdomain}.zendesk.com"

    print(f"\nOpening {final_url} …")
    print("A browser window will appear. Just log in (SSO included).")
    print("As soon as you're authenticated I'll grab the cookie and close it.\n")

    cookie = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(final_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # navigation can race with SSO redirects
            print(f"(navigation note: {exc})")

        # Wait for *real* authentication. A bare _zendesk_session cookie is set
        # even for anonymous visitors, so cookie presence isn't enough. Instead
        # we poll /api/v2/users/me.json using the context's cookie jar (same
        # cookies as the browser) and treat it as authenticated only when it
        # returns a user with a non-null id — exactly how the app decides
        # (see _fetch_current_user / "Anonymous user" handling in app.py).
        POLL_INTERVAL = 1.5   # seconds between checks
        WAIT_TIMEOUT = 300.0  # 5 min to finish logging in
        deadline = time.monotonic() + WAIT_TIMEOUT
        who = None
        print("Waiting for you to finish logging in…")
        try:
            while True:
                who = _authenticated_user(context, base_url)
                if who:
                    cookie = _cookie_header(context.cookies(), host)
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

    # Sanity check: the captured cookie should carry a Zendesk session marker,
    # matching what the app's _validate_cookie expects.
    if not any(m in cookie for m in ZENDESK_SESSION_MARKERS):
        print(
            "\nAuthenticated, but the captured cookie has no _zendesk_session "
            "marker — not writing .env. Please report this.",
            file=sys.stderr,
        )
        return 1
    if who:
        print(f"  Logged in as {who}.")

    _write_env(cookie)
    print(f"\n✔ Authenticated — saved {ENV_VAR} to {ENV_PATH} ({len(cookie)} chars).")
    print("  Browser closed. Restart the app to pick it up (it migrates .env → its DB on startup).")
    return 0


def check_cookie() -> int:
    """Validate the ZENDESK_COOKIE currently in .env, without a browser.

    Sends the stored cookie to /api/v2/users/me.json (same check the app uses)
    and reports whether Zendesk recognises it as a real, logged-in user.
    """
    try:
        import requests
    except ModuleNotFoundError:
        print("The 'requests' package is required for --check.", file=sys.stderr)
        return 2

    cookie = _read_env_var(ENV_VAR)
    subdomain = _read_subdomain()
    if not cookie:
        print(f"✘ {ENV_VAR} is not set in {ENV_PATH}. Run this script (no flag) to capture one.")
        return 1
    if not any(m in cookie for m in ZENDESK_SESSION_MARKERS):
        print(f"✘ {ENV_VAR} doesn't contain a _zendesk_session marker — likely malformed.")
        return 1

    url = f"https://{subdomain}.zendesk.com/api/v2/users/me.json"
    try:
        resp = requests.get(
            url,
            headers={"Cookie": cookie, "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"✘ Couldn't reach Zendesk ({subdomain}): {exc}")
        return 2

    if resp.status_code in (401, 403):
        print(f"✘ Cookie rejected (HTTP {resp.status_code}) — expired or invalid. Re-capture it.")
        return 1
    try:
        user = (resp.json() or {}).get("user") or {}
    except ValueError:
        print(f"✘ Unexpected response (HTTP {resp.status_code}) — not valid JSON.")
        return 1

    # Zendesk returns an anonymous placeholder (id=None) rather than 401 when the
    # session is invalid — same signal the app keys off.
    if not user.get("id"):
        print("✘ Cookie is invalid/expired — Zendesk returned an anonymous user.")
        return 1

    name = (user.get("name") or "").strip() or user.get("email") or f"id {user['id']}"
    print(f"✔ Cookie is valid — logged in as {name} on {subdomain}.zendesk.com.")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check_cookie())
    raise SystemExit(main())
