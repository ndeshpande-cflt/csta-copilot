#!/usr/bin/env python3
"""
topic_report.py — list a Kafka cluster's topics by received bytes, decrypting
pseudonymised topic names (the ones starting with "NH").

It does two things:

  1. Queries the CCloud telemetry Grafana datasource (the "Received Bytes"
     panel) for `io.confluent.kafka.server/received_bytes` grouped by
     `metric.topic`, summing each topic's bytes over the time window.
  2. For every pseudonymised topic name (starts with "NH"), calls the internal
     topic decrypt API to recover the real topic name.

Usage:
    ./topic_report.py --cluster-id lkc-pjpx5 --cookie '<grafana cookie>'

The Grafana cookie is the `-b` cookie string from a
grafana.telemetry.aws.confluent.cloud request (must contain grafana_session).

The decrypt API uses CONFLUENT_COOKIE from the project .env (your
admin.confluent.cloud session cookie, containing internal_auth_token).
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load .env from this folder or the parent project dir.
HERE = Path(__file__).resolve().parent
for env_path in (HERE / ".env", HERE.parent / ".env"):
    if env_path.exists():
        load_dotenv(env_path)
        break

GRAFANA_URL = (
    "https://grafana.telemetry.aws.confluent.cloud/api/ds/query"
    "?ds_type=confluent-metricsapi-datasource&requestId=SQR124"
)
DECRYPT_URL = "https://confluent.cloud/api/internal/topic/v1/decrypt"


def human_bytes(n):
    """Format a byte count like Grafana does (1024-based)."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:,.2f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024.0
    return f"{n:,.2f} EB"


def fetch_topic_bytes(cluster_id, cookie, hours=24, debug=False):
    """Return a list of (topic_pseudonym, total_received_bytes) for the cluster."""
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - hours * 3600 * 1000

    raw_query = json.dumps(
        {
            "group_by": ["metric.topic"],
            "aggregations": [
                {"metric": "io.confluent.kafka.server/received_bytes"}
            ],
            "filter": {
                "field": "resource.kafka.id",
                "op": "EQ",
                "value": cluster_id,
            },
            "limit": 1000,
        }
    )

    payload = {
        "queries": [
            {
                "dataType": "query",
                "datasource": {
                    "type": "confluent-metricsapi-datasource",
                    "uid": "_CfltMtrApgn",
                },
                "queryType": "raw",
                "rawQuery": raw_query,
                "refId": "A",
                "view": "cloud",
                "datasourceId": 5,
                "intervalMs": 60000,
                "maxDataPoints": 1680,
            }
        ],
        "from": str(from_ms),
        "to": str(now_ms),
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://grafana.telemetry.aws.confluent.cloud",
        "x-datasource-uid": "_CfltMtrApgn",
        "x-grafana-org-id": "1",
        "x-plugin-id": "confluent-metricsapi-datasource",
        "user-agent": "topic-report/1.0",
    }

    resp = requests.post(
        GRAFANA_URL, headers=headers, cookies=_cookie_dict(cookie),
        data=json.dumps(payload), timeout=60,
    )
    if resp.status_code == 401 or resp.status_code == 403:
        sys.exit(
            f"Grafana auth failed ({resp.status_code}). The --cookie is likely "
            "expired; grab a fresh one from a grafana.telemetry.aws.confluent.cloud request."
        )
    resp.raise_for_status()
    body = resp.json()
    if debug:
        print(f"[debug] response keys: {list(body.keys())}", file=sys.stderr)
    return _parse_frames(body, debug=debug)


def _parse_frames(body, debug=False):
    """Pull (topic, summed bytes) out of the Grafana dataframe response.

    Grafana returns either one frame per series ("long") or a single "wide"
    frame with a time column plus *one value field per topic*. We handle both by
    treating EVERY numeric (non-time) field as its own topic series, keyed by its
    `metric.topic` label.
    """
    results = []
    frames = body.get("results", {}).get("A", {}).get("frames", [])
    if debug:
        print(f"[debug] {len(frames)} frame(s) returned", file=sys.stderr)
    if not frames:
        return results

    for fi, frame in enumerate(frames):
        schema = frame.get("schema", {})
        fields = schema.get("fields", [])
        values = frame.get("data", {}).get("values", [])
        if debug:
            kinds = [(f.get("name"), f.get("type"), (f.get("labels") or {}).get("metric.topic"))
                     for f in fields]
            print(f"[debug] frame {fi}: {len(fields)} field(s) -> {kinds}", file=sys.stderr)

        for idx, field in enumerate(fields):
            if field.get("type") == "time":
                continue  # skip the timestamp column
            labels = field.get("labels") or {}
            topic = labels.get("metric.topic") or field.get("name") or schema.get("name")
            # Only count numeric columns; a wide frame's time field is the only
            # non-numeric one, but be defensive about string columns too.
            if field.get("type") not in (None, "number", "double", "float", "long", "int"):
                continue
            if idx >= len(values):
                continue
            total = sum(v for v in values[idx] if isinstance(v, (int, float)))
            if topic:
                results.append((topic, total))

    # Merge any duplicate topic rows.
    merged = {}
    for topic, total in results:
        merged[topic] = merged.get(topic, 0.0) + total
    return sorted(merged.items(), key=lambda kv: kv[1], reverse=True)


def decrypt_topic(pseudonym, confluent_cookie, cache, lock=None):
    """Decrypt a pseudonymised topic name (starts with 'NH'). Cached.

    Thread-safe when a `lock` is supplied (used by the parallel decrypt pool)."""
    if lock is not None:
        with lock:
            if pseudonym in cache:
                return cache[pseudonym]
    elif pseudonym in cache:
        return cache[pseudonym]

    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://admin.confluent.cloud",
        "referer": "https://admin.confluent.cloud/",
        "user-agent": "topic-report/1.0",
    }
    try:
        resp = requests.post(
            DECRYPT_URL, headers=headers,
            cookies=_cookie_dict(confluent_cookie),
            data=json.dumps({"pseudonym": pseudonym}), timeout=30,
        )
        if resp.status_code in (401, 403):
            name = "<decrypt auth failed — refresh CONFLUENT_COOKIE>"
        else:
            resp.raise_for_status()
            name = _extract_decrypted(resp)
    except requests.RequestException as exc:
        name = f"<decrypt error: {exc}>"

    if lock is not None:
        with lock:
            cache[pseudonym] = name
    else:
        cache[pseudonym] = name
    return name


def _extract_decrypted(resp):
    """The decrypt API returns the plaintext name; tolerate a few shapes."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        for key in ("topic", "text", "plaintext", "decrypted", "value", "name", "result"):
            if key in body and isinstance(body[key], str):
                return body[key]
        return json.dumps(body)
    return str(body)


def _progress(done, total, pseudonym):
    """Print a single-line, in-place decryption progress indicator to stderr."""
    label = pseudonym if len(pseudonym) <= 32 else pseudonym[:31] + "…"
    msg = f"\r  Decrypting [{done}/{total}] {label}"
    # Pad to clear any longer previous line, then return to start.
    sys.stderr.write(msg.ljust(60))
    sys.stderr.flush()


def _progress_done(total):
    """Clear the progress line and print a completion summary."""
    sys.stderr.write("\r".ljust(61) + "\r")
    sys.stderr.write(f"  Decrypted {total} topic name(s).\n")
    sys.stderr.flush()


def _cookie_dict(cookie_str):
    """Parse a 'k=v; k2=v2' cookie header string into a dict for requests."""
    jar = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar


def _resolve_grafana_cookie(args):
    """Decide which Grafana cookie to use, capturing a fresh one if needed.

    Order: explicit --cookie  >  --login (force browser)  >  GRAFANA_COOKIE from
    .env (if still valid)  >  browser capture.
    """
    import capture_grafana_cookie as cap

    if args.cookie:
        return args.cookie.strip()

    if args.login:
        return cap.capture().strip()

    env_cookie = (os.getenv("GRAFANA_COOKIE") or "").strip()
    if env_cookie and cap.check_cookie() == 0:
        return env_cookie

    print("No valid Grafana cookie in .env — opening a browser to capture one.")
    captured = cap.capture().strip()
    if captured:
        # capture() wrote it to .env; make it visible to this process too.
        os.environ["GRAFANA_COOKIE"] = captured
    return captured


def main():
    parser = argparse.ArgumentParser(
        description="List Kafka topics by received bytes, decrypting NH-pseudonyms."
    )
    parser.add_argument("--cluster-id", required=True,
                        help="Kafka cluster id (lkc-...)")
    parser.add_argument("--cookie", default=None,
                        help="Grafana cookie string (must contain grafana_session). "
                             "Defaults to GRAFANA_COOKIE from .env; if missing/expired, "
                             "a browser opens to capture it.")
    parser.add_argument("--login", action="store_true",
                        help="Force the browser login flow to (re)capture the Grafana cookie")
    parser.add_argument("--hours", type=int, default=24,
                        help="Look-back window in hours (default 24)")
    parser.add_argument("--no-decrypt", action="store_true",
                        help="Skip decryption, just show pseudonyms")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Parallel decrypt requests (default 8)")
    parser.add_argument("--debug", action="store_true",
                        help="Print the Grafana response frame structure to stderr")
    args = parser.parse_args()

    if not args.cluster_id.startswith("lkc-"):
        sys.exit("--cluster-id must start with 'lkc-'")

    grafana_cookie = _resolve_grafana_cookie(args)
    if not grafana_cookie:
        sys.exit("No Grafana cookie available — capture aborted.")

    confluent_cookie = (os.getenv("CONFLUENT_COOKIE") or "").strip()
    if not args.no_decrypt and not confluent_cookie:
        sys.exit("CONFLUENT_COOKIE not set in .env (needed for the decrypt API). "
                 "Use --no-decrypt to skip decryption.")

    print(f"Fetching received bytes for {args.cluster_id} (last {args.hours}h)...")
    topics = fetch_topic_bytes(args.cluster_id, grafana_cookie, hours=args.hours,
                               debug=args.debug)
    if not topics:
        print("No topic data returned. Check the cluster id / cookie / time window.")
        return

    to_decrypt = [p for p, _ in topics
                  if not args.no_decrypt and p.startswith("NH")]
    print(f"Fetched {len(topics)} topic(s); "
          f"{len(to_decrypt)} need decryption.\n")

    # Decrypt the unique pseudonyms in parallel (one API call each), updating a
    # thread-safe progress counter as results come back.
    names = {}  # pseudonym -> decrypted name
    if to_decrypt:
        unique = list(dict.fromkeys(to_decrypt))  # de-dup, preserve order
        cache = {}
        lock = threading.Lock()
        done = 0
        workers = max(1, min(args.concurrency, len(unique)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(decrypt_topic, p, confluent_cookie, cache, lock): p
                for p in unique
            }
            for fut in as_completed(futures):
                p = futures[fut]
                names[p] = fut.result()
                with lock:
                    done += 1
                    _progress(done, len(unique), p)
        _progress_done(len(unique))

    rows = []
    for pseudonym, total in topics:
        name = names.get(pseudonym, pseudonym)
        rows.append((name, pseudonym, total))

    # Build the report once, then both print it and save it to a file.
    name_w = min(max((len(r[0]) for r in rows), default=10), 60)
    grand_total = sum(r[2] for r in rows)
    lines = [
        f"# Topic received-bytes report for {args.cluster_id}",
        f"# Window: last {args.hours}h  |  Generated: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"{'TOPIC':<{name_w}}  {'RECEIVED BYTES':>16}",
        f"{'-' * name_w}  {'-' * 16}",
    ]
    for name, pseudonym, total in rows:
        display = name if len(name) <= name_w else name[: name_w - 1] + "…"
        lines.append(f"{display:<{name_w}}  {human_bytes(total):>16}")
    lines.append("")
    lines.append(f"{len(rows)} topics, {human_bytes(grand_total)} total received.")

    report = "\n".join(lines)
    print(report)

    out_dir = HERE / "clusters"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.cluster_id}.txt"
    out_path.write_text(report + "\n")
    print(f"\nSaved report to {out_path}")


if __name__ == "__main__":
    main()
