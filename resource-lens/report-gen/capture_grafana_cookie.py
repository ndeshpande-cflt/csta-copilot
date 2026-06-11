#!/usr/bin/env python3
"""
capture_grafana_cookie.py — open the CCloud telemetry Grafana in a browser, let
the user log in, then save the authenticated session cookie into the project
.env as GRAFANA_COOKIE.

    .venv/bin/python report-gen/capture_grafana_cookie.py

It launches a real (headed) Chromium window via Playwright, navigates to
grafana.telemetry.aws.confluent.cloud, waits for you to finish authenticating
(SSO included), then reads the session cookies back out of the browser and
writes them to .env. topic_report.py reads GRAFANA_COOKIE from .env, so no extra
wiring is needed.

This mirrors the project's capture_cookies.py, but targets the Grafana host
(a different domain) and keys off the `grafana_session` cookie instead of the
confluent.cloud internal_auth_token.
"""
import sys
import time
from pathlib import Path

# Write into the project .env (parent of this folder) so it sits alongside
# CONFLUENT_COOKIE and topic_report.py's load_dotenv picks it up.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ENV_VAR = "GRAFANA_COOKIE"

LOGIN_URL = (
    "https://grafana.telemetry.aws.confluent.cloud/"
    "?orgId=1"
)
COOKIE_DOMAIN_SUFFIX = "grafana.telemetry.aws.confluent.cloud"

# The cookie that proves an authenticated Grafana session. Anonymous visitors
# don't have it. `grafana_session_expiry` (epoch seconds) rides alongside it and
# lets us report/validate expiry offline.
AUTH_COOKIE_NAME = "grafana_session"
EXPIRY_COOKIE_NAME = "grafana_session_expiry"


def _read_env_var(name: str) -> str:
    """Read a single VAR=value from .env (last wins), unquoted. Returns ''."""
    val = ""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    return val


def _cookie_header(cookies, suffix: str) -> str:
    """Build a `name=value; …` header from cookies on the Grafana host."""
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


def _is_authenticated(cookies) -> bool:
    """True once the grafana_session cookie is present on the Grafana host."""
    for c in cookies:
        if c.get("name") != AUTH_COOKIE_NAME:
            continue
        domain = (c.get("domain") or "").lstrip(".")
        if domain.endswith(COOKIE_DOMAIN_SUFFIX) and c.get("value"):
            return True
    return False


def _write_env(value: str) -> None:
    """Upsert GRAFANA_COOKIE in .env, preserving every other line."""
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


def capture() -> str:
    """Launch a browser, wait for login, return the captured cookie header ('' on
    failure/timeout). Also writes it to .env on success."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print(
            "Playwright isn't installed. Install it into the venv:\n"
            "    .venv/bin/pip install playwright\n"
            "    .venv/bin/playwright install chromium\n",
            file=sys.stderr,
        )
        return ""

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

        POLL_INTERVAL = 1.5   # seconds between checks
        WAIT_TIMEOUT = 300.0  # 5 min to finish logging in
        deadline = time.monotonic() + WAIT_TIMEOUT
        print("Waiting for you to finish logging in…")
        try:
            while True:
                jar = context.cookies()
                if _is_authenticated(jar):
                    # Brief settle so grafana_session_expiry lands too.
                    time.sleep(0.5)
                    jar = context.cookies()
                    cookie = _cookie_header(jar, COOKIE_DOMAIN_SUFFIX)
                    break
                if time.monotonic() >= deadline:
                    print("\nTimed out waiting for login — nothing written.")
                    browser.close()
                    return ""
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nCancelled — nothing written.")
            browser.close()
            return ""

        browser.close()

    if f"{AUTH_COOKIE_NAME}=" not in cookie:
        print(
            f"\nAuthenticated, but the captured cookie has no {AUTH_COOKIE_NAME} "
            "marker — not writing .env. Please report this.",
            file=sys.stderr,
        )
        return ""

    _write_env(cookie)
    print(f"\n✔ Authenticated — saved {ENV_VAR} to {ENV_PATH} ({len(cookie)} chars).")
    return cookie


def check_cookie() -> int:
    """Validate the GRAFANA_COOKIE currently in .env, offline, via the
    grafana_session_expiry epoch. No network call."""
    import re
    cookie = _read_env_var(ENV_VAR)
    if not cookie:
        print(f"✘ {ENV_VAR} is not set in {ENV_PATH}. Run this script (no flag) to capture one.")
        return 1
    if f"{AUTH_COOKIE_NAME}=" not in cookie:
        print(f"✘ {ENV_VAR} has no {AUTH_COOKIE_NAME} marker — likely malformed.")
        return 1

    m = re.search(rf"{EXPIRY_COOKIE_NAME}=(\d+)", cookie)
    if not m:
        print(f"✔ {ENV_VAR} is set (no {EXPIRY_COOKIE_NAME} to check expiry against).")
        return 0

    exp = int(m.group(1))
    now = int(time.time())
    if exp <= now:
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
        print(f"✘ Grafana cookie expired {when:%b %d, %Y %I:%M %p %Z} — re-capture it.")
        return 1

    mins = (exp - now) // 60
    human = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins} min"
    print(f"✔ Grafana cookie is valid — session expires in {human}.")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        raise SystemExit(check_cookie())
    raise SystemExit(0 if capture() else 1)
