"""
Zendesk Call Prep Tool
----------------------
Fetches tickets + comments using your browser session cookies (no API key)
and generates a call-prep brief by shelling out to the Claude Code CLI.

Setup:
  1. Install Claude Code and confirm `claude --version` works
  2. Fill in .env with your cookie string and subdomain
  3. pip install -r requirements.txt
  4. python app.py
  5. Open http://localhost:5001
"""

import os
import random
import re
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import subprocess
import shutil

import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, stream_with_context
from dotenv import load_dotenv

load_dotenv()

SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "").strip()
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude").strip()
HAS_CLAUDE = shutil.which(CLAUDE_CMD) is not None
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # 10 min default

CUSTOMERS_JSON = Path(__file__).parent / "customers.json"


def _load_customers():
    """Load customer config from customers.json.

    Each entry: {name, slug, id, zendesk_org_id}. `id` mirrors `zendesk_org_id`
    for back-compat with existing Ticket Lens lookups. (Any `confluent_org_id`
    field is ignored here — that's used by the separate Resource Lens app.)

    JSON shape:
        {"customers": [{"name": "Acme", "slug": "acme",
                         "zendesk_org_id": 111}, ...]}
    """
    if not CUSTOMERS_JSON.exists():
        return []
    try:
        data = json.loads(CUSTOMERS_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data.get("customers"), list):
        return []
    out = []
    for entry in data["customers"]:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        slug = (entry.get("slug") or "").strip().lower()
        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        try:
            zd_int = int(entry["zendesk_org_id"]) if entry.get("zendesk_org_id") is not None else None
        except (TypeError, ValueError):
            zd_int = None
        if not slug:
            slug = str(zd_int or "")
        out.append({
            "name": name,
            "slug": slug,
            "id": zd_int,                  # back-compat alias
            "zendesk_org_id": zd_int,
        })
    return out


CUSTOMER_ORGS = _load_customers()

PRIORITY_LABELS = {
    "urgent": "P1",
    "high":   "P2",
    "normal": "P3",
    "low":    "P4",
}

DB_PATH = Path(__file__).parent / "cache.db"

app = Flask(__name__)


@app.template_filter("prio_label")
def _prio_label(value):
    if not value or value == "—":
        return "—"
    return PRIORITY_LABELS.get(str(value).lower(), value)


def _fetch_current_user():
    """Return the Zendesk user behind the configured cookie, or None on failure.

    Cached for an hour via the standard zd_get path cache so this doesn't fire
    on every page render.
    """
    if not (SUBDOMAIN and zendesk_cookie()):
        return None
    try:
        data = zd_get("/api/v2/users/me.json", ttl=3600)
    except (AuthError, requests.RequestException):
        return None
    user = (data or {}).get("user") or {}
    # When the session cookie is expired or invalid, Zendesk doesn't return a
    # 401 here — it returns a placeholder anonymous payload
    # (id=None, name="Anonymous user", email="invalid@example.com"). Detect
    # that and treat it as unauthenticated so we don't render "Welcome,
    # Anonymous". Drop the stale cache entry too, so the next page load
    # re-hits Zendesk and picks up a refreshed cookie without waiting for the
    # 1-hour TTL.
    if not user.get("id"):
        try:
            with db() as c:
                c.execute(
                    "DELETE FROM cache WHERE key = ?",
                    ("/api/v2/users/me.json",),
                )
        except sqlite3.DatabaseError:
            pass
        return None
    full = (user.get("name") or "").strip()
    first = full.split()[0] if full else None
    return {"name": full, "first_name": first, "email": user.get("email")}


@app.context_processor
def _inject_current_user():
    return {"current_user": _fetch_current_user()}


# -------------------- Cache --------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""
          CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
          )
        """)
        c.execute("""
          CREATE TABLE IF NOT EXISTS summaries (
            ticket_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            generated_at INTEGER NOT NULL,
            PRIMARY KEY (ticket_id, kind)
          )
        """)
        c.execute("""
          CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
          )
        """)

def cache_get(key, ttl=CACHE_TTL_SECONDS):
    with db() as c:
        row = c.execute(
            "SELECT value, fetched_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    if time.time() - row["fetched_at"] > ttl:
        return None
    return json.loads(row["value"])

def cache_set(key, value):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), int(time.time())),
        )


# -------------------- Secrets (cookies via UI) --------------------

# Keys used in the `secrets` table. Mirrors the previous env-var names so the
# settings UI and the resolvers below stay in lockstep.
SECRET_ZENDESK_COOKIE = "zendesk_cookie"


def secret_get(key):
    """Return the secret value stored in the DB, or None if not set / DB
    not yet initialized."""
    try:
        with db() as c:
            row = c.execute(
                "SELECT value FROM secrets WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row["value"] if row else None


def secret_get_meta(key):
    """Return (value, updated_at) tuple, or (None, None)."""
    try:
        with db() as c:
            row = c.execute(
                "SELECT value, updated_at FROM secrets WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.DatabaseError:
        return None, None
    if not row:
        return None, None
    return row["value"], row["updated_at"]


def secret_set(key, value):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO secrets (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )


def secret_delete(key):
    with db() as c:
        c.execute("DELETE FROM secrets WHERE key = ?", (key,))


def zendesk_cookie():
    # Sourced from the ZENDESK_COOKIE env var (.env), populated by
    # capture_cookies.py. The in-app Settings page is hidden; restart the app
    # after refreshing the cookie in .env.
    return (os.getenv("ZENDESK_COOKIE") or "").strip()


# Historic env-var → secret-key mappings, used only for the one-time migration
# below. The Settings page is now the only place cookies are managed.
_LEGACY_ENV_COOKIE_KEYS = (
    (SECRET_ZENDESK_COOKIE,   "ZENDESK_COOKIE"),
)


def _migrate_env_cookies_to_db():
    """One-time migration: copy any pre-existing `.env` cookie values into the
    secrets table so users who upgrade don't lose connectivity. Idempotent —
    once a value is in the DB, the env var is ignored on subsequent runs."""
    for secret_key, env_key in _LEGACY_ENV_COOKIE_KEYS:
        if secret_get(secret_key):
            continue
        env_val = (os.getenv(env_key) or "").strip()
        if env_val:
            secret_set(secret_key, env_val)


# Match either `-b 'string'` / `-b "string"` (curl cookie flag) or
# `-H 'cookie: string'` / `-H "Cookie: string"` (cookie as a request header).
_CURL_B_FLAG_RE = re.compile(r"-b\s+'([^']*)'|-b\s+\"([^\"]*)\"")
_CURL_H_COOKIE_RE = re.compile(
    r"-H\s+['\"][Cc]ookie:\s*([^'\"]+)['\"]"
)


_ZENDESK_SESSION_RE = re.compile(r"\b(?:_zendesk_session|_zendesk_shared_session)=")


def _validate_cookie(form_key, cookie):
    """Return (ok, error_msg_or_None). Verifies that a cookie string contains
    the markers we need to track its connection status. Called at save time so
    junk pastes never reach the DB and the UI never has to show
    'Cookie set, but no … to verify'.
    """
    if not cookie:
        return False, "Empty cookie value."
    if form_key == "zendesk":
        if not _ZENDESK_SESSION_RE.search(cookie):
            return False, (
                "Doesn't look like a Zendesk session cookie — expected "
                "_zendesk_session=… or _zendesk_shared_session=… in the value. "
                "Re-copy from DevTools while logged into Zendesk."
            )
        return True, None
    return True, None


def _zendesk_cookie_status(user):
    """Return (ok, detail) for the Zendesk cookie. The Zendesk check is the
    cheapest of the three because `_fetch_current_user` already handles the
    'Anonymous user' response Zendesk hands back when the cookie is invalid."""
    if not zendesk_cookie():
        return False, "Not set"
    if user and user.get("name"):
        return True, f"Connected as {user['name']}"
    return False, "Cookie expired or invalid — Zendesk returned anonymous"


def parse_cookie_input(raw):
    """Accept either a full 'Copy as cURL' command or a raw cookie blob.
    Returns the cookie string, or "" if nothing usable was found.

    Curl detection: input starts with 'curl ' (after whitespace). The cookie
    can be in either `-b '...'` or `-H 'cookie: ...'` form depending on the
    browser version.
    """
    if not raw:
        return ""
    text = raw.strip()
    looks_like_curl = text.lower().startswith("curl ") or text.lower().startswith("curl\t")
    if looks_like_curl:
        # Fold line continuations so the regexes don't have to span newlines.
        flat = re.sub(r"\\\s*\n", " ", text)
        m = _CURL_B_FLAG_RE.search(flat)
        if m:
            return (m.group(1) or m.group(2) or "").strip()
        m = _CURL_H_COOKIE_RE.search(flat)
        if m:
            return m.group(1).strip()
        return ""
    # Raw cookie blob — collapse any whitespace runs but otherwise keep as-is.
    return re.sub(r"\s+", " ", text).strip()


# -------------------- Zendesk client --------------------

class AuthError(Exception):
    pass

def zd_get(path, ttl=CACHE_TTL_SECONDS):
    """GET against authenticated Zendesk JSON endpoint using the session cookie."""
    cookie = zendesk_cookie()
    if not (SUBDOMAIN and cookie):
        raise AuthError(
            "Missing ZENDESK_SUBDOMAIN (in .env) or Zendesk cookie. "
            "Paste a fresh cookie on the Settings page."
        )

    cached = cache_get(path, ttl=ttl)
    if cached is not None:
        return cached

    url = f"https://{SUBDOMAIN}.zendesk.com{path}"
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://{SUBDOMAIN}.zendesk.com/agent/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    }
    r = requests.get(url, headers=headers, timeout=30)

    if r.status_code in (401, 403):
        raise AuthError(
            f"Auth failed ({r.status_code}). Your Zendesk session cookie likely "
            "expired — paste a fresh one on the Settings page."
        )
    if r.status_code == 429:
        raise AuthError("Rate limited by Zendesk. Wait a minute and retry.")
    r.raise_for_status()

    data = r.json()
    cache_set(path, data)
    return data

def fetch_ticket(ticket_id):
    return zd_get(f"/api/v2/tickets/{ticket_id}.json")

def fetch_comments(ticket_id):
    """Paginate through all comments."""
    all_comments = []
    path = f"/api/v2/tickets/{ticket_id}/comments.json?include=users"
    users = {}
    while path:
        data = zd_get(path)
        all_comments.extend(data.get("comments", []))
        for u in data.get("users", []) or []:
            users[u["id"]] = u
        next_page = data.get("next_page")
        if not next_page:
            break
        # Convert absolute next_page URL back to a path so cache key is stable-ish
        parsed = urlparse(next_page)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
    return all_comments, users

def fetch_users(user_ids):
    if not user_ids:
        return {}
    ids = ",".join(str(i) for i in sorted(set(user_ids)))
    data = zd_get(f"/api/v2/users/show_many.json?ids={ids}")
    return {u["id"]: u for u in data.get("users", [])}

def fetch_organization(org_id):
    """Look up a Zendesk organization by ID. Returns the org dict or None."""
    if not org_id:
        return None
    try:
        data = zd_get(f"/api/v2/organizations/{org_id}.json")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    return data.get("organization")

def _condense_organization(org):
    """Strip the Zendesk organization payload to the fields we surface in the UI
    and the chat prompt. Returns None if `org` is falsy."""
    if not org:
        return None
    raw_fields = org.get("organization_fields") or {}
    fields = {k: v for k, v in raw_fields.items() if v not in (None, "", [], {})}
    return {
        "id": org.get("id"),
        "name": org.get("name") or "",
        "domains": [d for d in (org.get("domain_names") or []) if d],
        "details": (org.get("details") or "").strip(),
        "notes": (org.get("notes") or "").strip(),
        "tags": list(org.get("tags") or []),
        "fields": fields,
    }

ACTIVE_STATUSES = ("new", "open", "pending")

def fetch_org_active_tickets(org_id, max_pages=10):
    """Return new + open + pending tickets for an organization, sorted by
    updated_at desc.

    Uses the direct list endpoint /api/v2/organizations/{id}/tickets.json (the
    Search API's `organization_id:` filter is not a supported ticket-search
    field, only `organization:<name>` is). Paginates and filters by status
    client-side. Caps at `max_pages * 100` tickets so a busy org doesn't fan
    out into hundreds of API calls.
    """
    all_tickets = []
    truncated = False
    path = (
        f"/api/v2/organizations/{org_id}/tickets.json"
        f"?sort_by=updated_at&sort_order=desc&per_page=100"
    )
    pages_fetched = 0
    while path and pages_fetched < max_pages:
        data = zd_get(path)
        all_tickets.extend(data.get("tickets", []) or [])
        pages_fetched += 1
        next_page = data.get("next_page")
        if not next_page:
            path = None
            break
        parsed = urlparse(next_page)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
    if path:
        # We hit the page cap but more pages still exist.
        truncated = True
    active = [t for t in all_tickets if t.get("status") in ACTIVE_STATUSES]
    return {
        "results": active,
        "count": len(active),
        "truncated": truncated,
    }

def find_org(slug):
    for o in CUSTOMER_ORGS:
        if o["slug"] == slug:
            return o
    return None


# -------------------- Ticket assembly --------------------

def parse_ticket_id(raw):
    """Accept '12345', '#12345', or a full ticket URL."""
    raw = raw.strip()
    m = re.search(r"(\d+)", raw)
    if not m:
        return None
    return m.group(1)

def assemble_ticket(ticket_id):
    """Returns a single dict with ticket, comments enriched with author info, and users."""
    t_data = fetch_ticket(ticket_id)
    ticket = t_data["ticket"]
    comments, users_from_comments = fetch_comments(ticket_id)

    # Make sure we have author info for everyone
    needed_ids = {c["author_id"] for c in comments}
    needed_ids.add(ticket.get("requester_id"))
    needed_ids.add(ticket.get("assignee_id"))
    needed_ids.add(ticket.get("submitter_id"))
    needed_ids.discard(None)
    missing = needed_ids - set(users_from_comments.keys())
    if missing:
        extra = fetch_users(list(missing))
        users_from_comments.update(extra)

    # Enrich comments
    enriched = []
    for c in comments:
        author = users_from_comments.get(c["author_id"], {})
        enriched.append({
            "id": c["id"],
            "created_at": c["created_at"],
            "public": c.get("public", True),
            "author_id": c["author_id"],
            "author_name": author.get("name", f"User {c['author_id']}"),
            "author_email": author.get("email", ""),
            "author_role": author.get("role", "end-user"),
            "body": c.get("body", "") or c.get("plain_body", ""),
        })

    return {
        "ticket": ticket,
        "comments": enriched,
        "users": users_from_comments,
    }


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def _fmt_dt(dt):
    """Format a UTC datetime as local time with AM/PM, e.g. 'Jun 5, 2026 3:42 PM PDT'."""
    if not dt:
        return "—"
    local = dt.astimezone()
    tz = local.strftime("%Z")
    base = local.strftime("%b %-d, %Y %-I:%M %p")
    return f"{base} {tz}".strip()

_CLUSTER_ID_RE = re.compile(r"\b((?:lkc|pkc)-[a-z0-9]+)\b", re.IGNORECASE)
_CLUSTER_NAME_PATTERNS = (
    re.compile(r"cluster[_\s]*name\s*[:=]\s*[\"']?([A-Za-z0-9_.\-]{2,64})", re.IGNORECASE),
    re.compile(r"cluster\s+(?:called|named)\s+[\"']([^\"']{2,64})[\"']", re.IGNORECASE),
)

def extract_cluster_info(bundle):
    """Scan the ticket subject + comment bodies for Confluent cluster IDs (lkc-/pkc-)
    and best-effort cluster names. Returns deduped, order-preserving lists."""
    parts = [bundle["ticket"].get("subject") or ""]
    for c in bundle["comments"]:
        parts.append(c.get("body") or "")
    text = re.sub(r"<[^>]+>", " ", "\n".join(parts))

    ids, seen = [], set()
    for m in _CLUSTER_ID_RE.finditer(text):
        val = m.group(1).lower()
        if val not in seen:
            seen.add(val)
            ids.append(val)

    names, seen_n = [], set()
    for pat in _CLUSTER_NAME_PATTERNS:
        for m in pat.finditer(text):
            val = re.sub(r"[.\-_]+$", "", m.group(1).strip())
            if not val or _CLUSTER_ID_RE.fullmatch(val):
                continue
            key = val.lower()
            if key not in seen_n:
                seen_n.add(key)
                names.append(val)

    return {"ids": ids, "names": names}


_SNIPPET_SKIP_PATTERNS = re.compile(
    r"(\bat\s+[\w.$]+:\d+|\bexception\b|\btraceback\b|\d{4}-\d{2}-\d{2}T\d|^\s*[{}\[\]<>])",
    re.IGNORECASE,
)
_SNIPPET_GREETINGS = (
    "hi ", "hello", "hey ", "thanks", "thank you", "regards", "best,",
    "best regards", "kind regards", "warm regards", "cheers",
    "good morning", "good afternoon", "good evening", "sincerely",
)

def extract_discussion_snippets(bundle, limit=18):
    """Pull short, topic-ish snippets from the ticket subject + comment bodies
    so the summary loader can flip through what the model is "reading"."""
    snippets, seen = [], set()

    def add(text):
        text = text.strip(" \"'-•*>›»–—\t")
        if not (15 <= len(text) <= 110):
            return
        low = text.lower()
        if low in seen:
            return
        if any(low.startswith(g) for g in _SNIPPET_GREETINGS):
            return
        if _SNIPPET_SKIP_PATTERNS.search(text):
            return
        seen.add(low)
        snippets.append(text)

    subj = (bundle["ticket"].get("subject") or "").strip()
    if subj:
        add(subj)

    for c in bundle["comments"]:
        body = c.get("body") or ""
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
            add(sent)
            if len(snippets) >= limit * 3:
                break
        if len(snippets) >= limit * 3:
            break

    # Shuffle so the loader doesn't always start with the subject.
    random.shuffle(snippets)
    return snippets[:limit]


def build_ticket_meta(bundle, subdomain):
    """Header-strip fields for the ticket view: dates, priority, contacts."""
    t = bundle["ticket"]
    comments = bundle["comments"]

    created_dt = _parse_iso(t.get("created_at"))
    comment_dts = [_parse_iso(c.get("created_at")) for c in comments]
    comment_dts = [d for d in comment_dts if d]
    latest_reply_dt = max(comment_dts) if comment_dts else None

    if created_dt:
        end = datetime.now(timezone.utc)
        total_days = max(0, (end - created_dt).days)
    else:
        total_days = None

    # Split contacts by Zendesk role. Dedupe by author_id, keep first-seen order.
    seen = set()
    customer_contacts = []
    confluent_contacts = []
    for c in comments:
        aid = c.get("author_id")
        if aid in seen:
            continue
        seen.add(aid)
        contact = {
            "name": c.get("author_name") or "(unknown)",
            "email": c.get("author_email") or "",
        }
        if c.get("author_role") == "end-user":
            customer_contacts.append(contact)
        else:
            confluent_contacts.append(contact)

    # Match the ticket's Zendesk organization against CUSTOMER_ORGS so the
    # ticket page can link back to the customer listing page.
    customer_slug = None
    customer_org_name = None
    org_id = t.get("organization_id")
    if org_id:
        for o in CUSTOMER_ORGS:
            if o["id"] == org_id:
                customer_slug = o["slug"]
                customer_org_name = o["name"]
                break
    # Always hit the Zendesk organization-details API so the ticket card
    # shows the authoritative org name (CUSTOMER_ORGS is just a UI shortlist
    # for the home page — not every ticket's org is in it). Fall back to the
    # configured display name only if the API call fails outright.
    organization_name = None
    if org_id:
        try:
            org_obj = fetch_organization(org_id)
            if org_obj:
                organization_name = (org_obj.get("name") or "").strip() or None
        except (AuthError, requests.RequestException):
            organization_name = None
    if not organization_name:
        organization_name = customer_org_name

    cluster = extract_cluster_info(bundle)

    return {
        "ticket_id": t.get("id"),
        "subject": t.get("subject") or "(no subject)",
        "zendesk_url": f"https://{subdomain}.zendesk.com/agent/tickets/{t.get('id')}" if subdomain else "",
        "created_at": _fmt_dt(created_dt),
        "latest_reply_at": _fmt_dt(latest_reply_dt),
        "total_days": total_days,
        "priority": t.get("priority") or "—",
        "status": t.get("status") or "—",
        "customer_name": _derive_customer_name(customer_contacts),
        "customer_slug": customer_slug,
        "customer_org_name": customer_org_name,
        "organization_name": organization_name,
        "customer_contacts": customer_contacts,
        "confluent_contacts": confluent_contacts,
        "cluster_ids": cluster["ids"],
        "cluster_names": cluster["names"],
    }

# Free-mail domains we don't want to mistake for a customer org.
_FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "proton.me", "protonmail.com",
}

def _derive_customer_name(customer_contacts):
    """Best-effort 'who is this customer' label for the header.

    Prefers the second-level email domain (e.g. alice@acme.com -> 'Acme'),
    skipping freemail domains. Falls back to the first customer contact's name.
    """
    for c in customer_contacts:
        email = (c.get("email") or "").lower()
        if "@" not in email:
            continue
        domain = email.split("@", 1)[1]
        if domain in _FREEMAIL_DOMAINS:
            continue
        # Strip subdomains like "support.acme.com" -> "acme.com" -> "acme".
        parts = domain.split(".")
        if len(parts) >= 2:
            label = parts[-2]
        else:
            label = parts[0]
        if label:
            return label[:1].upper() + label[1:]
    if customer_contacts:
        return customer_contacts[0].get("name") or None
    return None


# -------------------- Summary generation --------------------

SHORT_SUMMARY_SYSTEM = """Summarize this support ticket. Your output must start with a single SENTIMENT line, then the three markdown sections below. No preamble, no closing remarks, no bullets.

The very first line must be exactly:
SENTIMENT: <one of: Positive, Neutral, Frustrated, Angry>

Pick the value that best reflects the customer's overall tone across the conversation, based on their language and urgency. Then a blank line, then:

## Issue
One short paragraph describing what the customer is reporting or asking for.

## Attempted
One short paragraph describing what's been tried so far. If nothing has been attempted yet, write exactly: Nothing yet.

## Next step
One short paragraph describing what needs to happen next."""

LONG_SUMMARY_SYSTEM = """You are preparing a comprehensive customer-success briefing on a Zendesk ticket. Your output will be rendered as a visual dashboard with cards, so structure matters.

Output ONLY the markdown structure below — no preamble, no closing remarks. Use these exact section headings (keep the emojis) in this exact order. Follow the within-section format exactly. If a field is unknown, write "—". The customer org and customer personnel are shown elsewhere — do NOT include sections for them.

### 📄 Executive Summary
2-3 sentences covering the core issue, business impact, and current status / final resolution.

### ⏱️ Ticket Metadata
Three bullets, exactly these labels:
- Ticket Age: <total time since opening, e.g. "12 days">
- Agents Involved: <number of unique Confluent agents>
- Avg Response Time: <estimate based on timestamps, e.g. "2.5 hours">

### 📅 Interaction Timeline
Chronological bulleted list, oldest to newest. Each line in this shape:
- YYYY-MM-DD HH:MM — Who did what (one short sentence)

Use the 24-hour HH:MM time from the comment timestamps. If a precise time isn't available for an entry, fall back to just the date.

### 🎯 Resolution & Pending Asks
Two bullets, exactly these labels:
- Resolution: <how it was fixed; if not fixed, "Unresolved" and the blocker>
- Customer Pending Ask: <what the customer is waiting on; "None" if nothing>

### 👤 Customer Sentiment
Two bullets, exactly these labels:
- Sentiment: <one of: Positive, Neutral, Frustrated, Angry>
- Reasoning: <one sentence explaining why, citing customer language/tone>

### 📈 Support Team Evaluation
Two bullets, exactly these labels:
- Handling Quality: <brief, objective assessment>
- Areas for Improvement: <suggestions, or "Handled perfectly">

### 📈 Internal Message to Support Team
A short, supportive internal note to the team (1-3 sentences) if improvements exist. Keep the tone constructive, not blaming. If there is nothing to add, omit this section entirely.

### ✅ Action Items
Markdown table. Owner is one of: Customer, Eng, or Agent <name>. Status is one of: Pending, Done.

| Item | Owner | Status |
|---|---|---|
| <what needs to be done> | <owner> | <Pending / Done> |

Be concise. Bullets and short phrases over prose. Cite facts only from the ticket. Do not invent values."""

def build_brief_prompt(bundle):
    t = bundle["ticket"]
    lines = [
        f"Ticket #{t['id']}: {t.get('subject', '(no subject)')}",
        f"Status: {t.get('status')}    Priority: {t.get('priority')}    Type: {t.get('type')}",
        f"Created: {t.get('created_at')}    Updated: {t.get('updated_at')}",
        f"Tags: {', '.join(t.get('tags', []) or [])}",
        "",
        "--- Conversation ---",
    ]
    for c in bundle["comments"]:
        visibility = "public" if c["public"] else "internal note"
        lines.append(
            f"\n[{c['created_at']}] {c['author_name']} ({c['author_role']}, {visibility}):"
        )
        # Strip HTML lightly
        body = re.sub(r"<[^>]+>", "", c["body"] or "").strip()
        lines.append(body)
    return "\n".join(lines)

_CLAUDE_ENV_BLOCKLIST = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
}

def _run_claude(prompt, timeout=180, model=None, extra_args=None, extra_env=None):
    """Shell out to the Claude Code CLI with the prompt piped via stdin.

    `model` is an optional `--model` flag value (alias like 'haiku'/'sonnet'/'opus'
    or a full model ID). When omitted, the CLI's default model is used.

    `extra_args` is an optional list of additional CLI flags (e.g. allow-list
    entries to enable MCP tools). `extra_env` is an optional dict of env vars
    merged on top of the cleaned base environment.

    Returns (text, error). On success `error` is None and `text` is the
    assistant's output. On any failure `text` is None and `error` is a short
    user-readable explanation.
    """
    if not HAS_CLAUDE:
        return None, (
            "Claude Code CLI not found on PATH. Install Claude Code or set "
            "CLAUDE_CMD in .env to its full path, then restart."
        )
    clean_env = {k: v for k, v in os.environ.items() if k not in _CLAUDE_ENV_BLOCKLIST}
    if extra_env:
        clean_env.update(extra_env)
    args = [CLAUDE_CMD, "-p"]
    if model:
        args += ["--model", model]
    if extra_args:
        args += list(extra_args)
    try:
        result = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=clean_env,
            cwd=str(Path(__file__).parent),
        )
    except subprocess.TimeoutExpired:
        return None, f"Claude Code timed out after {timeout}s."
    except Exception as e:
        return None, f"Error invoking Claude Code: {e}"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        return None, f"Claude Code error: {err[:500]}"
    out = (result.stdout or "").strip()
    if not out:
        return None, "Claude Code returned empty output."
    return out, None


def _iter_claude_stream(prompt, timeout=300, model=None, extra_args=None, extra_env=None):
    """Like `_run_claude` but streams Claude Code's `--output-format stream-json`
    events as they arrive. Yields dicts with one of these shapes:

        {"type": "progress", "message": "<short status>"}
        {"type": "done",     "content": "<final assistant text>"}
        {"type": "error",    "error":   "<short reason>"}

    Caller is responsible for serializing these onto the wire.
    """
    if not HAS_CLAUDE:
        yield {"type": "error", "error": (
            "Claude Code CLI not found on PATH. Install Claude Code or set "
            "CLAUDE_CMD in .env to its full path, then restart."
        )}
        return
    clean_env = {k: v for k, v in os.environ.items() if k not in _CLAUDE_ENV_BLOCKLIST}
    if extra_env:
        clean_env.update(extra_env)
    args = [CLAUDE_CMD, "-p", "--output-format", "stream-json", "--verbose"]
    if model:
        args += ["--model", model]
    if extra_args:
        args += list(extra_args)

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env,
            cwd=str(Path(__file__).parent),
            bufsize=1,  # line-buffered
        )
    except Exception as e:
        yield {"type": "error", "error": f"Error invoking Claude Code: {e}"}
        return

    # Pipe the prompt in and close stdin so claude knows the input is done.
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception as e:
        proc.kill()
        yield {"type": "error", "error": f"Failed to send prompt: {e}"}
        return

    deadline = time.monotonic() + timeout
    final_text = None
    try:
        for raw in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                yield {"type": "error", "error": f"Claude Code timed out after {timeout}s."}
                return
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                # Lines that aren't JSON are usually log noise — skip.
                continue
            msg = _chat_progress_for_event(evt)
            if msg:
                yield {"type": "progress", "message": msg}
            # The `result` event carries the final assistant text in `.result`.
            if evt.get("type") == "result" and "result" in evt:
                final_text = (evt.get("result") or "").strip()
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if proc.returncode and proc.returncode != 0 and not final_text:
        err = ""
        try:
            err = (proc.stderr.read() or "").strip()
        except Exception:
            pass
        yield {"type": "error", "error": f"Claude Code error: {err[:500] or 'unknown error'}"}
        return
    if not final_text:
        yield {"type": "error", "error": "Claude Code returned no final result."}
        return
    yield {"type": "done", "content": final_text}


def _chat_progress_for_event(evt):
    """Translate one Claude Code stream-json event into a short user-facing
    progress string. Returns None for events that aren't worth surfacing."""
    etype = evt.get("type")
    if etype == "system" and evt.get("subtype") == "init":
        return "Thinking…"
    if etype == "assistant":
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name") or ""
                inp = block.get("input") or {}
                if name.startswith("mcp__glean__"):
                    tool = name.split("__", 2)[-1]
                    query = inp.get("query") or inp.get("q") or inp.get("question") or ""
                    if query:
                        snip = query if len(query) <= 60 else query[:57] + "…"
                        return f"Searching Glean ({tool}): {snip}"
                    return f"Calling Glean: {tool}"
                if name:
                    return f"Calling tool: {name}"
            if btype == "text":
                return "Writing response…"
    if etype == "user":
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                return "Reading results…"
    return None


def generate_summary(bundle, system_prompt, model=None, extra_args=None):
    """Generate a summary by shelling out to Claude Code CLI."""
    conversation = build_brief_prompt(bundle)
    prompt = f"{system_prompt}\n\n--- TICKET ---\n{conversation}"
    text, err = _run_claude(prompt, model=model, extra_args=extra_args)
    if err:
        return f"_({err})_"
    return text

SYSTEM_PROMPTS = {
    "short": SHORT_SUMMARY_SYSTEM,
    "long":  LONG_SUMMARY_SYSTEM,
}

# User prefers Opus everywhere — reports it as the fastest model in their
# environment, faster than Haiku/Sonnet. See feedback_prefer_opus.md memory.
SUMMARY_MODELS = {
    "short": "opus",
    "long":  "opus",
}

# Summaries are pure text-in/text-out — they don't need any MCP servers or
# built-in tools. Skipping both shaves 2-5s of CLI startup per call (notably,
# it avoids initializing Glean MCP, which is the slowest server on the user's
# machine and gets loaded by default even though summaries never call it).
SUMMARY_EXTRA_ARGS = ["--strict-mcp-config", "--tools", ""]

def fetch_cached_summary(ticket_id, kind, bundle=None):
    """Return cached summary content, or None if missing/stale.

    If `bundle` is supplied, also invalidate the cache when the ticket has been
    updated since the summary was generated.
    """
    with db() as c:
        row = c.execute(
            "SELECT content, generated_at FROM summaries WHERE ticket_id = ? AND kind = ?",
            (ticket_id, kind),
        ).fetchone()
    if not row:
        return None
    if bundle is not None:
        t_updated = bundle["ticket"].get("updated_at", "")
        try:
            t_dt = datetime.fromisoformat(t_updated.replace("Z", "+00:00"))
            b_dt = datetime.fromtimestamp(row["generated_at"], tz=timezone.utc)
            if t_dt > b_dt:
                return None
        except Exception:
            pass
    return row["content"]

def store_summary(ticket_id, kind, content):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO summaries (ticket_id, kind, content, generated_at) "
            "VALUES (?, ?, ?, ?)",
            (ticket_id, kind, content, int(time.time())),
        )

def get_or_generate_summary(ticket_id, bundle, kind, force=False):
    if not force:
        cached = fetch_cached_summary(ticket_id, kind, bundle=bundle)
        if cached is not None:
            return cached
    content = generate_summary(
        bundle,
        SYSTEM_PROMPTS[kind],
        model=SUMMARY_MODELS.get(kind),
        extra_args=SUMMARY_EXTRA_ARGS,
    )
    store_summary(ticket_id, kind, content)
    return content

_SENTIMENT_RE = re.compile(r"^\s*SENTIMENT\s*:\s*([^\n]+?)\s*\n+", re.IGNORECASE)
_VALID_SENTIMENTS = {"positive", "neutral", "frustrated", "angry"}

def split_short_summary(text):
    """Pull the leading 'SENTIMENT: X' marker out of the short summary.

    Returns (sentiment_value or None, body_without_marker).
    """
    if not text:
        return None, text
    m = _SENTIMENT_RE.match(text)
    if not m:
        return None, text
    val = m.group(1).strip()
    if val.lower() not in _VALID_SENTIMENTS:
        return None, text
    return val, text[m.end():]


# -------------------- Routes --------------------

def _lens_statuses(user):
    """Compute the Ticket Lens connection status for the landing-page tile.
    Returns a dict {ok: True|False|None, label: str, detail: str}.
    """
    z_ok, z_detail = _zendesk_cookie_status(user)
    return {
        "ok": z_ok,
        "label": "Connected" if z_ok is True else ("Disconnected" if z_ok is False else "Cookie set"),
        "detail": z_detail,
    }


@app.route("/")
def index():
    """CSTA Copilot landing page: the Ticket Lens tile."""
    user = _fetch_current_user()
    ticket_status = _lens_statuses(user)
    return render_template(
        "index.html",
        ticket_status=ticket_status,
    )


@app.route("/tickets")
def ticket_lens_home():
    config_ok = bool(SUBDOMAIN and zendesk_cookie())
    orgs = []
    has_error = False
    if config_ok:
        for org in CUSTOMER_ORGS:
            entry = {**org, "new_count": None, "open_count": None, "pending_count": None, "error": None}
            try:
                data = fetch_org_active_tickets(org["id"])
                results = data.get("results", []) or []
                entry["new_count"] = sum(1 for t in results if t.get("status") == "new")
                entry["open_count"] = sum(1 for t in results if t.get("status") == "open")
                entry["pending_count"] = sum(1 for t in results if t.get("status") == "pending")
                entry["truncated"] = bool(data.get("truncated"))
            except AuthError as e:
                entry["error"] = str(e)
                has_error = True
            except requests.RequestException as e:
                entry["error"] = f"Couldn't reach Zendesk: {e}"
                has_error = True
            orgs.append(entry)
    connected = config_ok and not has_error
    return render_template(
        "ticket_lens_home.html",
        config_ok=config_ok,
        subdomain=SUBDOMAIN,
        orgs=orgs,
        connected=connected,
    )


@app.route("/c/<slug>")
def view_customer(slug):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    if request.args.get("refresh") == "1":
        # Wipe cached pages of this org's ticket listing so we re-fetch fresh.
        with db() as c:
            c.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (f"%/organizations/{org['id']}/tickets.json%",),
            )
    try:
        data = fetch_org_active_tickets(org["id"])
    except AuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.ConnectionError as e:
        return render_template("error.html", message=(
            f"Couldn't reach Zendesk at https://{SUBDOMAIN}.zendesk.com — "
            f"verify ZENDESK_SUBDOMAIN in your .env.\n\n{e}"
        )), 502
    except requests.RequestException as e:
        return render_template("error.html", message=f"Zendesk request failed: {e}"), 502
    tickets = []
    for t in data.get("results", []) or []:
        tickets.append({
            "id": t.get("id"),
            "subject": t.get("subject") or "(no subject)",
            "status": t.get("status"),
            "priority": t.get("priority") or "—",
            "created_at": _fmt_dt(_parse_iso(t.get("created_at"))),
            "updated_at": _fmt_dt(_parse_iso(t.get("updated_at"))),
        })
    new_count = sum(1 for t in tickets if t["status"] == "new")
    open_count = sum(1 for t in tickets if t["status"] == "open")
    pending_count = sum(1 for t in tickets if t["status"] == "pending")
    # Priority breakdown across the active set, in display order.
    priority_order = ["urgent", "high", "normal", "low"]
    priority_counts = [
        {"name": p, "count": sum(1 for t in tickets if (t["priority"] or "").lower() == p)}
        for p in priority_order
    ]
    priority_counts = [pc for pc in priority_counts if pc["count"] > 0]
    return render_template(
        "customer.html",
        org=org,
        tickets=tickets,
        new_count=new_count,
        open_count=open_count,
        pending_count=pending_count,
        priority_counts=priority_counts,
        total_count=len(tickets),
        truncated=bool(data.get("truncated")),
        subdomain=SUBDOMAIN,
    )

@app.route("/ticket", methods=["POST"])
def ticket_form():
    raw = (request.form.get("ticket") or "").strip()
    tid = parse_ticket_id(raw)
    if not tid:
        return render_template(
            "error.html",
            message=(
                f"Couldn't read a ticket ID from \"{raw or '(empty)'}\". "
                "Enter a numeric ticket id like 12345, or a full Zendesk ticket URL."
            ),
        ), 400
    return redirect(url_for("view_ticket", ticket_id=tid))

@app.route("/t/<ticket_id>")
def view_ticket(ticket_id):
    force = request.args.get("refresh") == "1"
    try:
        if force:
            # Wipe cache for this ticket's resources + summaries
            with db() as c:
                c.execute("DELETE FROM cache WHERE key LIKE ?", (f"%/tickets/{ticket_id}%",))
                c.execute("DELETE FROM summaries WHERE ticket_id = ?", (ticket_id,))
        bundle = assemble_ticket(ticket_id)
        # Short summary is generated async by the browser so the page itself
        # paints instantly. Only inline a cached value when one is available.
        short_raw = fetch_cached_summary(ticket_id, "short", bundle=bundle)
        if short_raw:
            sentiment, short_summary = split_short_summary(short_raw)
            short_pending = False
        else:
            sentiment, short_summary = None, ""
            short_pending = True
        # Long summary is on-demand: only show if it's already cached and still fresh.
        long_summary = fetch_cached_summary(ticket_id, "long", bundle=bundle)
        meta = build_ticket_meta(bundle, SUBDOMAIN)
    except AuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.ConnectionError as e:
        return render_template("error.html", message=(
            f"Couldn't reach Zendesk at https://{SUBDOMAIN}.zendesk.com — "
            f"verify ZENDESK_SUBDOMAIN in your .env (e.g. it's typically not just "
            f"\"confluent\"; check what's in the URL when you're logged into Zendesk).\n\n{e}"
        )), 502
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            return render_template(
                "error.html",
                message=f"Ticket #{ticket_id} was not found in Zendesk. Double-check the ID and try again.",
            ), 404
        return render_template("error.html", message=f"Zendesk returned an error: {e}"), 502
    except requests.RequestException as e:
        return render_template("error.html", message=f"Zendesk request failed: {e}"), 502
    meta["sentiment"] = sentiment
    discussion_snippets = extract_discussion_snippets(bundle)
    return render_template(
        "ticket.html",
        bundle=bundle,
        meta=meta,
        short_summary=short_summary,
        short_pending=short_pending,
        long_summary=long_summary,
        ticket_id=ticket_id,
        subdomain=SUBDOMAIN,
        discussion_snippets=discussion_snippets,
    )

@app.route("/t/<ticket_id>/long-summary", methods=["POST"])
def generate_long_summary(ticket_id):
    try:
        bundle = assemble_ticket(ticket_id)
        content = get_or_generate_summary(ticket_id, bundle, "long", force=True)
    except AuthError as e:
        return jsonify({"error": str(e)}), 401
    except requests.RequestException as e:
        return jsonify({"error": f"Couldn't reach Zendesk: {e}"}), 502
    return jsonify({"content": content})

@app.route("/t/<ticket_id>/short-summary", methods=["POST"])
def generate_short_summary(ticket_id):
    """Generate (or return cached) short summary as JSON. Called by the browser
    after the page paints so the initial load isn't blocked on Claude."""
    try:
        bundle = assemble_ticket(ticket_id)
        raw = get_or_generate_summary(ticket_id, bundle, "short", force=False)
    except AuthError as e:
        return jsonify({"error": str(e)}), 401
    except requests.RequestException as e:
        return jsonify({"error": f"Couldn't reach Zendesk: {e}"}), 502
    sentiment, content = split_short_summary(raw)
    return jsonify({"content": content, "sentiment": sentiment})

CHAT_SYSTEM_PROMPT = """You are a helpful assistant answering questions about a specific Zendesk customer-success ticket.

Your user is a CSTA (Customer Success Technical Architect) at Confluent — a role equivalent to a Technical Account Manager (TAM). They own the technical relationship with one or more enterprise customers running Confluent / Apache Kafka, and they're using this app to prep for a customer call, follow up internally with Support or Engineering, or draft outbound communication about this ticket.

The full ticket conversation is provided below for context. Answer the user's questions accurately and concisely, citing details from the ticket when relevant. Quote short phrases when they carry weight.

Rules:
- Base every answer on the provided ticket. If the answer isn't in the ticket, say so plainly — do not invent facts.
- Be concise. Bullets for lists, short paragraphs otherwise. Markdown is fine.
- If the user's question is ambiguous, ask one clarifying question before answering.
- Don't propose actions that require side effects (sending emails, creating tickets, etc.). You are read-only.
- For drafting requests (Slack messages, escalation notes, customer updates), match the channel: Slack messages are brief and conversational; internal escalations are direct and factual with what's at risk and what's needed; customer updates are clear, professional, and avoid blame."""

# Appended to CHAT_SYSTEM_PROMPT only when the user has toggled Glean on. It
# relaxes the "ticket-only" rule so the model can consult internal docs via the
# Glean MCP tools.
GLEAN_SYSTEM_ADDENDUM = """

--- GLEAN SEARCH IS AVAILABLE ---
You also have access to Glean MCP tools (named `mcp__glean__*`) for searching
internal documentation (runbooks, design docs, FAQs, past tickets, Slack archives).
Use them whenever the user's question would be answered better by internal docs
than by the ticket alone — for example, lookups about Confluent / Kafka best
practices, internal processes, escalation paths, or how the team has handled
similar issues before. Prefer one targeted Glean search over many broad ones.
The ticket remains your primary source for ticket-specific questions.

Citation rules when you use Glean:
- Cite specific facts inline as markdown links: `[Doc title](URL)`. Keep the
  link text short — the document title is ideal.
- At the end of your response, when you used Glean, add a final section with
  the literal heading `**Sources:**` followed by a bulleted list of every
  document you referenced, each as a markdown link with the title and URL.
  Format example:
      **Sources:**
      - [How to downscale a Kafka cluster](https://glean.example.com/doc/123)
      - [Cluster expansion runbook](https://glean.example.com/doc/456)
- Only cite documents you actually retrieved via Glean — never invent URLs or
  titles. If Glean returned nothing useful, say so plainly and skip the Sources
  section."""

def _format_org_for_prompt(org):
    """Render the condensed org dict as a compact block for the chat prompt.
    Returns empty string when there's nothing useful to include."""
    if not org:
        return ""
    lines = []
    if org.get("name"):
        line = f"Name: {org['name']}"
        if org.get("id"):
            line += f" (id: {org['id']})"
        lines.append(line)
    if org.get("domains"):
        lines.append("Domains: " + ", ".join(org["domains"]))
    if org.get("tags"):
        lines.append("Tags: " + ", ".join(org["tags"]))
    if org.get("details"):
        lines.append(f"Details: {org['details']}")
    if org.get("notes"):
        lines.append(f"Notes: {org['notes']}")
    if org.get("fields"):
        kv = "; ".join(f"{k}={v}" for k, v in org["fields"].items())
        lines.append(f"Fields: {kv}")
    return "\n".join(lines)

def _format_chat_history(messages):
    """Render the prior chat turns as a transcript for Claude. Excludes the
    final 'user' turn — the caller appends it separately."""
    lines = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = (m.get("content") or "").strip()
        if not content or role not in ("user", "assistant"):
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n\n".join(lines)

@app.route("/t/<ticket_id>/chat", methods=["POST"])
def chat_with_ticket(ticket_id):
    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "Provide a non-empty 'messages' array."}), 400
    last = messages[-1]
    if (last.get("role") or "").lower() != "user" or not (last.get("content") or "").strip():
        return jsonify({"error": "The last message must be a user message with non-empty content."}), 400
    use_glean = bool(payload.get("use_glean"))

    try:
        bundle = assemble_ticket(ticket_id)
    except AuthError as e:
        return jsonify({"error": str(e)}), 401
    except requests.RequestException as e:
        return jsonify({"error": f"Couldn't reach Zendesk: {e}"}), 502

    conversation = build_brief_prompt(bundle)
    org_block = _format_org_for_prompt(
        _condense_organization(fetch_organization(bundle["ticket"].get("organization_id")))
    )
    history = _format_chat_history(messages[:-1])
    user_msg = last["content"].strip()

    system_prompt = CHAT_SYSTEM_PROMPT + (GLEAN_SYSTEM_ADDENDUM if use_glean else "")
    parts = [system_prompt, "--- TICKET ---", conversation]
    if org_block:
        parts += ["--- ORGANIZATION ---", org_block]
    if history:
        parts += ["--- CHAT HISTORY ---", history]
    parts += [f"User: {user_msg}", "Assistant:"]
    prompt = "\n\n".join(parts)

    if use_glean:
        model = "opus"
        extra_args = ["--allowedTools", "mcp__glean__*"]
        extra_env = {"MCP_TOOL_TIMEOUT": "600000"}  # 10 min per Glean tool call
        timeout = 660  # subprocess ceiling: 10 min + a small buffer for the model turn
    else:
        # Plain chat doesn't need any tools or MCP servers — skip them so the
        # CLI doesn't waste startup time initializing them.
        model = "opus"
        extra_args = ["--strict-mcp-config", "--tools", ""]
        extra_env = None
        timeout = 120

    def event_stream():
        for evt in _iter_claude_stream(
            prompt, timeout=timeout, model=model, extra_args=extra_args, extra_env=extra_env
        ):
            yield json.dumps(evt) + "\n"

    return Response(
        stream_with_context(event_stream()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )

@app.route("/api/ticket/<ticket_id>")
def api_ticket(ticket_id):
    try:
        return jsonify(assemble_ticket(ticket_id))
    except AuthError as e:
        return jsonify({"error": str(e)}), 401


# -------------------- Resource Lens routes --------------------

# -------------------- Settings (cookie paste UI) --------------------

# Each section in the Settings page is keyed by `form_key`. The dict drives
# both the GET render (status summary per section) and the POST handler
# (which form field maps to which secret).
SETTINGS_SECTIONS = [
    {
        "form_key": "zendesk",
        "secret_key": SECRET_ZENDESK_COOKIE,
        "title": "Zendesk",
        "description": (
            "Used for ticket lookups against "
            "{subdomain}.zendesk.com."
        ),
        "how_to": (
            "Open any ticket in Zendesk, DevTools → Network → click any "
            "request to *.zendesk.com → right-click → Copy → Copy as cURL. "
            "Paste below."
        ),
    },
]


def _settings_status(user):
    """Build the per-section view-model for the Settings page. `user` is the
    Zendesk identity (or None) — passed in so we don't re-fetch it once per
    section."""
    sections = []
    for spec in SETTINGS_SECTIONS:
        db_value, updated_at = secret_get_meta(spec["secret_key"])
        cookie_present = bool(db_value and db_value.strip())
        updated_dt = None
        if updated_at:
            updated_dt = _fmt_dt(datetime.fromtimestamp(updated_at, tz=timezone.utc))

        # Per-cookie health check. `ok` is True/False/None; None means we
        # couldn't determine status (treat as configured-but-unknown in UI).
        if spec["form_key"] == "zendesk":
            ok, detail = _zendesk_cookie_status(user)
        else:
            ok, detail = (None, "")

        sections.append({
            **spec,
            "cookie_present": cookie_present,
            "updated_at": updated_dt,
            # length only — never echo the cookie back into HTML.
            "value_len": len(db_value.strip()) if cookie_present else 0,
            "status_ok": ok,
            "status_detail": detail,
        })
    return sections


@app.route("/settings", methods=["GET"])
def settings_page():
    # Settings page is hidden — cookies are managed via the ZENDESK_COOKIE env
    # var in .env (see capture_cookies.py). Endpoint kept so existing
    # url_for('settings_page') references still resolve; it just sends home.
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def settings_save():
    # Disabled along with the hidden Settings page (see settings_page above).
    return redirect(url_for("index"))


def _settings_save_disabled():
    """Save any non-empty paste fields. Each section is independent — empty
    fields are ignored so the user can update one at a time without clobbering
    the others.

    Supports two actions besides save:
      - clear=<form_key>  delete the stored secret for that section
      - reset_cache=1     wipe the cached /api/v2/users/me.json
    """
    saved = []
    cleared = []
    errors = []

    clear_key = (request.form.get("clear") or "").strip()
    if clear_key:
        for spec in SETTINGS_SECTIONS:
            if spec["form_key"] == clear_key:
                secret_delete(spec["secret_key"])
                cleared.append(spec["title"])
                break
    else:
        for spec in SETTINGS_SECTIONS:
            raw = (request.form.get(spec["form_key"]) or "").strip()
            if not raw:
                continue
            parsed = parse_cookie_input(raw)
            if not parsed:
                errors.append(
                    f"{spec['title']}: couldn't find a cookie in the pasted text. "
                    "Make sure the curl command includes -b '...' (or paste just "
                    "the cookie string)."
                )
                continue
            ok, validation_err = _validate_cookie(spec["form_key"], parsed)
            if not ok:
                errors.append(f"{spec['title']}: {validation_err}")
                continue
            secret_set(spec["secret_key"], parsed)
            saved.append(spec["title"])

    # Always drop the cached `me.json` payload so the welcome name reflects
    # the freshly-pasted Zendesk cookie on the next render.
    if saved or cleared:
        try:
            with db() as c:
                c.execute(
                    "DELETE FROM cache WHERE key = ?",
                    ("/api/v2/users/me.json",),
                )
        except sqlite3.DatabaseError:
            pass

    msg_parts = []
    if saved:
        msg_parts.append("Saved: " + ", ".join(saved))
    if cleared:
        msg_parts.append("Cleared: " + ", ".join(cleared))
    if errors:
        msg_parts.append("Errors: " + " | ".join(errors))
    flash = " · ".join(msg_parts) if msg_parts else "No changes."
    return redirect(url_for("settings_page", flash=flash))


# Ensure tables exist whenever this module is imported — needed for the
# secrets resolvers (zendesk_cookie) which
# can be called before app.run().
init_db()
# One-shot upgrade path: lift any cookie values still living in `.env` into
# the DB so existing users don't have to re-paste after the .env fallback was
# removed.
_migrate_env_cookies_to_db()


if __name__ == "__main__":
    print(f"\n  CSTA Copilot — http://localhost:5001")
    print(f"  Subdomain: {SUBDOMAIN or '(not set)'}\n")
    app.run(host="127.0.0.1", port=5001, debug=False)
