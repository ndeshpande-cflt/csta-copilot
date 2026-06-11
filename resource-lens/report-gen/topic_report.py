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
import html
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

# received_bytes is a per-window rate we sum for the normal report. retained_bytes
# is a storage gauge used for idle detection: it surfaces topics regardless of
# recent ingress, so genuinely-empty topics show up (received_bytes omits them).
METRIC_RECEIVED_BYTES = "io.confluent.kafka.server/received_bytes"
METRIC_RETAINED_BYTES = "io.confluent.kafka.server/retained_bytes"


def human_bytes(n):
    """Format a byte count like Grafana does (1024-based)."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:,.2f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024.0
    return f"{n:,.2f} EB"


def fetch_topic_bytes(cluster_id, cookie, hours=24, debug=False,
                      metric=METRIC_RECEIVED_BYTES, agg="sum"):
    """Fetch a per-topic byte metric for the cluster.

    `metric` is the Confluent metric to query; `agg` is how to combine a topic's
    time-series points ("sum" for rate metrics like received_bytes, "max" for
    gauges like retained_bytes). Returns (topics, from_ms, to_ms): the list of
    (topic_pseudonym, value) plus the epoch-ms query window actually used."""
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - hours * 3600 * 1000

    raw_query = json.dumps(
        {
            "group_by": ["metric.topic"],
            "aggregations": [
                {"metric": metric}
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
    return _parse_frames(body, debug=debug, agg=agg), from_ms, now_ms


def _parse_frames(body, debug=False, agg="sum"):
    """Pull (topic, value) out of the Grafana dataframe response.

    Grafana returns either one frame per series ("long") or a single "wide"
    frame with a time column plus *one value field per topic*. We handle both by
    treating EVERY numeric (non-time) field as its own topic series, keyed by its
    `metric.topic` label. `agg` controls how a series' points are combined and how
    duplicate topic rows merge: "sum" (rate totals) or "max" (gauge peak).
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
            nums = [v for v in values[idx] if isinstance(v, (int, float))]
            value = (max(nums) if nums else 0.0) if agg == "max" else sum(nums)
            if topic:
                results.append((topic, value))

    # Merge any duplicate topic rows using the same aggregation.
    merged = {}
    for topic, value in results:
        if topic in merged:
            merged[topic] = max(merged[topic], value) if agg == "max" else merged[topic] + value
        else:
            merged[topic] = value
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


def _format_range(from_ms, to_ms, hours):
    """Human-readable query window, e.g. '2026-06-09 10:00 → 2026-06-10 10:00 IST
    (last 24h)'. Falls back to just the duration if timestamps are missing."""
    if from_ms is None or to_ms is None:
        return f"last {hours}h"
    fmt = "%Y-%m-%d %H:%M"
    start = time.strftime(fmt, time.localtime(from_ms / 1000))
    end = time.strftime(fmt + " %Z", time.localtime(to_ms / 1000))
    return f"{start} → {end} (last {hours}h)"


def _render_html(cluster_id, hours, rows, grand_total, from_ms=None, to_ms=None,
                 idle=False):
    """Render the topic report as a small, self-contained HTML page."""
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    range_str = _format_range(from_ms, to_ms, hours)
    heading = ("Idle topics (0 bytes retained)" if idle
               else "Received bytes by topic")
    title_prefix = "Idle topics" if idle else "Topic report"
    value_col = "Retained bytes" if idle else "Received bytes"
    body_rows = []
    for name, pseudonym, total in rows:
        decrypted = name != pseudonym
        # Show the pseudonym as a tooltip when we resolved a real name.
        title = f' title="{html.escape(pseudonym)}"' if decrypted else ""
        cls = "" if decrypted else ' class="raw"'
        body_rows.append(
            f"      <tr><td{title}{cls}>{html.escape(name)}</td>"
            f"<td class='num'>{human_bytes(total)}</td></tr>"
        )
    rows_html = "\n".join(body_rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_prefix} — {html.escape(cluster_id)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 2rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
  .meta {{ color: #666; font-size: .85rem; margin-bottom: 1.25rem; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 760px;
          background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ padding: .5rem .75rem; border-bottom: 1px solid #eee; text-align: left; }}
  th {{ background: #f0f3f7; font-size: .8rem; text-transform: uppercase;
       letter-spacing: .04em; color: #555; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums;
                   white-space: nowrap; }}
  tr:hover td {{ background: #f7faff; }}
  td.raw {{ color: #999; font-family: ui-monospace, Menlo, monospace; font-size: .85rem; }}
  td {{ font-family: ui-monospace, Menlo, monospace; font-size: .9rem; }}
  tfoot td {{ font-weight: 600; border-top: 2px solid #ddd; }}
</style>
</head>
<body>
  <h1>{heading} — {html.escape(cluster_id)}</h1>
  <div class="meta">
    <div><strong>Time range:</strong> {html.escape(range_str)}</div>
    <div><strong>Generated:</strong> {html.escape(generated)}</div>
    <div><strong>Topics:</strong> {len(rows)}</div>
  </div>
  <table>
    <thead>
      <tr><th>Topic</th><th class="num">{value_col}</th></tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
    <tfoot>
      <tr><td>{"Idle topics" if idle else "Total"} ({len(rows)} topics)</td>
          <td class="num">{"—" if idle else human_bytes(grand_total)}</td></tr>
    </tfoot>
  </table>
</body>
</html>
"""


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
    parser.add_argument("--idle", action="store_true",
                        help="Only include topics that received 0 bytes in the window "
                             "(idle topics); these are then the only ones decrypted")
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

    # --idle uses retained_bytes (a storage gauge, peak over the window) rather
    # than received_bytes, because received_bytes omits topics with no ingress
    # entirely — so they'd never be seen. metric_word labels the output column.
    metric = METRIC_RETAINED_BYTES if args.idle else METRIC_RECEIVED_BYTES
    agg = "max" if args.idle else "sum"
    metric_word = "retained" if args.idle else "received"

    print(f"Fetching {metric_word} bytes for {args.cluster_id} (last {args.hours}h)...")
    topics, from_ms, to_ms = fetch_topic_bytes(
        args.cluster_id, grafana_cookie, hours=args.hours, debug=args.debug,
        metric=metric, agg=agg)
    if not topics:
        print("No topic data returned. Check the cluster id / cookie / time window.")
        return

    # --idle: keep only topics holding no data (retained bytes round to 0 B), so
    # that the subsequent decryption runs against just those. The gauge is a float,
    # so a topic shown as "0 B" can be a sub-byte fraction — match the display by
    # rounding to whole bytes.
    if args.idle:
        total_count = len(topics)
        topics = [(t, b) for t, b in topics if round(float(b)) == 0]
        print(f"--idle: {len(topics)} of {total_count} topic(s) have 0 retained bytes.")
        if not topics:
            print("No idle topics — every topic received data in the window.")
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
    report_kind = "Idle topics (0 bytes retained)" if args.idle else "Topic received-bytes report"
    col_header = f"{metric_word.upper()} BYTES"
    lines = [
        f"# {report_kind} for {args.cluster_id}",
        f"# Window: last {args.hours}h  |  Generated: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"{'TOPIC':<{name_w}}  {col_header:>16}",
        f"{'-' * name_w}  {'-' * 16}",
    ]
    for name, pseudonym, total in rows:
        display = name if len(name) <= name_w else name[: name_w - 1] + "…"
        lines.append(f"{display:<{name_w}}  {human_bytes(total):>16}")
    lines.append("")
    if args.idle:
        lines.append(f"{len(rows)} idle topic(s) with 0 retained bytes.")
    else:
        lines.append(f"{len(rows)} topics, {human_bytes(grand_total)} total received.")

    print("\n".join(lines))

    # Idle runs go to a separate file so they don't overwrite the full report.
    out_dir = HERE / "reports-output" / "topics"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-idle" if args.idle else ""
    out_path = out_dir / f"{args.cluster_id}{suffix}.html"
    out_path.write_text(_render_html(args.cluster_id, args.hours, rows, grand_total,
                                     from_ms=from_ms, to_ms=to_ms, idle=args.idle))
    print(f"\nSaved report to {out_path}")


if __name__ == "__main__":
    main()
