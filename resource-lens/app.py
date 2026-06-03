"""
CSTA Copilot — Resource Lens
----------------------------
Browse Confluent Cloud environments, clusters, and their cloud-resource details
(networks, endpoints, placement) using your browser session cookie — no API key.

Setup:
  1. pip install -r requirements.txt
  2. Set CONFLUENT_COOKIE in .env (your admin.confluent.cloud session cookie)
  3. python app.py
  4. Open http://localhost:5002

This was split out of the Ticket Lens app; the two are independent apps that
share only the customers.json shape and the cookie-capture approach.
"""

import os
import re
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for

from dotenv import load_dotenv

load_dotenv()

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # 10 min default

CUSTOMERS_JSON = Path(__file__).parent / "customers.json"
DB_PATH = Path(__file__).parent / "cache.db"

app = Flask(__name__)


def _load_customers():
    """Load customer config from customers.json.

    Each entry: {name, slug, id, zendesk_org_id, confluent_org_id}. `id`
    mirrors `zendesk_org_id` for back-compat with the Ticket Lens shape.
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
        try:
            cf_int = int(entry["confluent_org_id"]) if entry.get("confluent_org_id") is not None else None
        except (TypeError, ValueError):
            cf_int = None
        if not slug:
            slug = str(zd_int or "")
        out.append({
            "name": name,
            "slug": slug,
            "id": zd_int,                  # back-compat alias
            "zendesk_org_id": zd_int,
            "confluent_org_id": cf_int,
        })
    return out


CUSTOMER_ORGS = _load_customers()


def find_org(slug):
    for o in CUSTOMER_ORGS:
        if o["slug"] == slug:
            return o
    return None


@app.context_processor
def _inject_current_user():
    # Resource Lens has no Zendesk identity; templates guard on this being set.
    return {"current_user": None}


# -------------------- Cache / secrets --------------------

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


SECRET_CONFLUENT_COOKIE = "confluent_cloud_cookie"


def secret_get(key):
    try:
        with db() as c:
            row = c.execute(
                "SELECT value FROM secrets WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row["value"] if row else None


def secret_set(key, value):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO secrets (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )


def confluent_cookie():
    """The admin.confluent.cloud session cookie. Prefer the env var (.env), then
    fall back to anything stored in the DB secrets table."""
    env = (os.getenv("CONFLUENT_COOKIE") or os.getenv("CONFLUENT_CLOUD_COOKIE") or "").strip()
    if env:
        return env
    return (secret_get(SECRET_CONFLUENT_COOKIE) or "").strip()


# -------------------- Cookie status (no network) --------------------

_INTERNAL_AUTH_TOKEN_RE = re.compile(r"internal_auth_token=([^;\s]+)")


def _decode_jwt_payload(token):
    """Decode a JWT payload *without* signature verification — only to inspect
    the `exp` claim for status display. Never trust it for authorization."""
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


def _humanize_window_seconds(secs):
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h" + (f" {m}m" if m else "")
    return f"{secs // 86400}d"


def _fmt_dt(dt):
    if not dt:
        return "—"
    local = dt.astimezone()
    tz = local.strftime("%Z")
    base = local.strftime("%b %-d, %Y %-I:%M %p")
    return f"{base} {tz}".strip()


def _confluent_cookie_status():
    """Return (ok: bool | None, detail: str) for the Confluent Cloud cookie,
    via the embedded internal_auth_token JWT's exp claim (no network call)."""
    cookie = confluent_cookie()
    if not cookie:
        return False, "Not set"
    m = _INTERNAL_AUTH_TOKEN_RE.search(cookie)
    if not m:
        return None, "Cookie set, but no internal_auth_token to verify"
    payload = _decode_jwt_payload(m.group(1))
    if not payload:
        return None, "Cookie set, but auth token can't be decoded"
    exp = payload.get("exp")
    if not exp:
        return None, "Cookie set, but auth token has no exp claim"
    now = int(time.time())
    if exp < now:
        when = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
        return False, f"Cookie expired {_fmt_dt(when)}"
    return True, f"Valid · expires in {_humanize_window_seconds(exp - now)}"


# -------------------- Confluent Cloud client --------------------

class MetricLensAuthError(Exception):
    pass


_CFC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0 Safari/537.36"
)


def _cfc_headers():
    return {
        "Cookie": confluent_cookie(),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://admin.confluent.cloud",
        "Referer": "https://admin.confluent.cloud/",
        "User-Agent": _CFC_UA,
    }


def cfc_get(path, ttl=CACHE_TTL_SECONDS, cache_key=None):
    """GET against confluent.cloud internal APIs using the browser session cookie."""
    if not confluent_cookie():
        raise MetricLensAuthError(
            "Missing Confluent Cloud cookie. Set CONFLUENT_COOKIE in .env."
        )
    key = cache_key or f"cfc:{path}"
    cached = cache_get(key, ttl=ttl)
    if cached is not None:
        return cached
    url = f"https://confluent.cloud{path}"
    r = requests.get(url, headers=_cfc_headers(), timeout=30)
    if r.status_code in (401, 403):
        raise MetricLensAuthError(
            f"Confluent Cloud auth failed ({r.status_code}). Your session cookie "
            "likely expired — refresh CONFLUENT_COOKIE in .env."
        )
    if r.status_code == 404:
        raise MetricLensAuthError(
            f"Confluent Cloud returned 404 for {path}. Double-check the org/env id."
        )
    r.raise_for_status()
    data = r.json()
    cache_set(key, data)
    return data


def fetch_environments(confluent_org_id):
    return cfc_get(
        f"/api/internal/organizations/{confluent_org_id}/details?include_deactivated=true"
    )


def fetch_clusters(account_id):
    return cfc_get(f"/api/internal/clusters?account_id={account_id}")


def _extract_environments(raw):
    """Best-effort: pull a list of {id, name, deactivated} from the org-details
    response. Confluent's internal API isn't documented externally, so we try a
    few obvious shapes."""
    candidates = []
    if isinstance(raw, dict):
        for key in ("accounts", "environments", "deployments"):
            val = raw.get(key)
            if isinstance(val, list):
                candidates = val
                break
        if not candidates:
            inner = raw.get("organization") if isinstance(raw.get("organization"), dict) else None
            if inner:
                for key in ("accounts", "environments", "deployments"):
                    val = inner.get(key)
                    if isinstance(val, list):
                        candidates = val
                        break
    out = []
    for e in candidates or []:
        if not isinstance(e, dict):
            continue
        eid = e.get("id") or e.get("account_id") or e.get("resource_id")
        if not eid:
            continue
        out.append({
            "id": str(eid),
            "name": e.get("name") or e.get("display_name") or str(eid),
            "deactivated": bool(e.get("deactivated", False)),
        })
    return out


def _extract_clusters(raw):
    """Flatten the nested `/api/internal/clusters` response into per-cluster dicts."""
    candidates = []
    if isinstance(raw, dict):
        for key in ("clusters", "kafka_clusters", "logical_clusters"):
            val = raw.get(key)
            if isinstance(val, list):
                candidates = val
                break
    elif isinstance(raw, list):
        candidates = raw
    out = []
    for entry in candidates or []:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("cluster") if isinstance(entry.get("cluster"), dict) else {}
        lkc = entry.get("id") or inner.get("logical_cluster_id")
        if not (isinstance(lkc, str) and lkc.startswith("lkc-")):
            continue
        placement = inner.get("placement") if isinstance(inner.get("placement"), dict) else {}
        cloud_provider = placement.get("cloud_provider") if isinstance(placement.get("cloud_provider"), dict) else {}
        config = inner.get("config") if isinstance(inner.get("config"), dict) else {}
        zones_raw = cloud_provider.get("zones") or []
        zones = [
            (z.get("name") or z.get("zone_id") or "")
            for z in zones_raw if isinstance(z, dict)
        ]
        zones = [z for z in zones if z]
        out.append({
            "id": lkc,
            "name": entry.get("name") or config.get("name") or lkc,
            "status": inner.get("status") or "",
            "cloud": cloud_provider.get("cloud") or "",
            "region": cloud_provider.get("region") or "",
            "zones": zones,
            "physical_cluster_id": (
                inner.get("physical_cluster_id")
                or placement.get("physical_cluster_id")
                or ""
            ),
            "network_id": inner.get("network_id") or "",
            "network_type": inner.get("selected_network_type") or "",
            "k8s_cluster_id": inner.get("k8s_cluster_id") or "",
            "bootstrap_endpoint": inner.get("bootstrap_endpoint") or "",
            "http_endpoint": inner.get("http_endpoint") or "",
            "created": inner.get("created") or "",
            "modified": inner.get("modified") or "",
            "pending_cku": inner.get("pending_cku"),
            "account_id": (
                entry.get("account_id") or config.get("environment") or ""
            ),
        })
    return out


# -------------------- Routes --------------------

def _render_home():
    config_ok = bool(confluent_cookie())
    return render_template(
        "metric_lens_home.html",
        config_ok=config_ok,
        orgs=CUSTOMER_ORGS,
        connected=config_ok,
    )


@app.route("/")
def index():
    return _render_home()


@app.route("/metrics")
def metric_lens_home():
    return _render_home()


_CLUSTER_ID_SEARCH_RE = re.compile(r"(lkc-[a-z0-9]+)", re.IGNORECASE)


@app.route("/metrics/search", methods=["POST"])
def metric_cluster_search():
    raw = (request.form.get("cluster") or "").strip()
    m = _CLUSTER_ID_SEARCH_RE.search(raw)
    if not m:
        return render_template(
            "error.html",
            message=(
                f"Couldn't read a cluster ID from \"{raw or '(empty)'}\". "
                "Enter a logical Kafka cluster ID like lkc-22xqy."
            ),
        ), 400
    return redirect(url_for("metric_cluster_direct", cluster_id=m.group(1).lower()))


@app.route("/m/<slug>")
def metric_envs(slug):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    cf_org_id = org.get("confluent_org_id")
    if not cf_org_id:
        return render_template("error.html", message=(
            f"No confluent_org_id configured for {org['name']} in customers.json. "
            "Add it and reload."
        )), 400
    if request.args.get("refresh") == "1":
        with db() as c:
            c.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (f"cfc:/api/internal/organizations/{cf_org_id}/details%",),
            )
    try:
        raw = fetch_environments(cf_org_id)
    except MetricLensAuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.RequestException as e:
        return render_template("error.html", message=f"Confluent Cloud request failed: {e}"), 502
    envs = _extract_environments(raw)
    return render_template("metric_envs.html", org=org, envs=envs, raw_count=len(envs))


@app.route("/m/<slug>/env/<env_id>")
def metric_clusters(slug, env_id):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    if request.args.get("refresh") == "1":
        with db() as c:
            c.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (f"cfc:/api/internal/clusters?account_id={env_id}%",),
            )
    try:
        raw = fetch_clusters(env_id)
    except MetricLensAuthError as e:
        return render_template("error.html", message=str(e)), 401
    except requests.RequestException as e:
        return render_template("error.html", message=f"Confluent Cloud request failed: {e}"), 502
    clusters = _extract_clusters(raw)
    return render_template(
        "metric_clusters.html", org=org, env_id=env_id, clusters=clusters,
    )


@app.route("/m/<slug>/env/<env_id>/cluster/<cluster_id>")
def metric_cluster(slug, env_id, cluster_id):
    org = find_org(slug)
    if not org:
        return render_template("error.html", message=f"Unknown customer: {slug}"), 404
    cluster = None
    try:
        raw = fetch_clusters(env_id)
        for c in _extract_clusters(raw):
            if c["id"] == cluster_id:
                cluster = c
                break
    except (MetricLensAuthError, requests.RequestException):
        cluster = None
    return render_template(
        "metric_cluster.html",
        org=org, env_id=env_id, cluster_id=cluster_id, cluster=cluster,
    )


@app.route("/m/cluster/<cluster_id>")
def metric_cluster_direct(cluster_id):
    """Direct entry to a cluster's details, no customer/env breadcrumb. Used by
    the home page's cluster-id search."""
    return render_template(
        "metric_cluster.html",
        org=None, env_id=None, cluster_id=cluster_id, cluster=None,
    )


@app.route("/settings")
def settings_page():
    # No in-app settings yet — cookies come from CONFLUENT_COOKIE in .env.
    # Endpoint kept so templates' url_for('settings_page') still resolves.
    return redirect(url_for("index"))


# Ensure tables exist on import (cache/secrets used by the cookie resolvers).
init_db()


if __name__ == "__main__":
    print("\n  CSTA Copilot — Resource Lens — http://localhost:5002\n")
    app.run(host="127.0.0.1", port=5002, debug=False)
