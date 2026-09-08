#!/usr/bin/env python3
"""
Pulls the per-minute HRV series out of Zepp's cloud and serves it on a small
local HTTP endpoint. An iOS Shortcut reads it and writes the samples to Apple
Health (can't do that from Linux, hence the phone).

    pull    grab new data from Zepp into sqlite (cron this hourly)
    serve   run the http server (systemd)
    dump    print the last few samples
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --- edit these --------------------------------------------------------------

ZEPP_CLI_DIR = os.path.expanduser("~/zepp-health")
ZEPP_CLI = os.path.join(ZEPP_CLI_DIR, "zepp_health.py")

ZEPP_PY = os.path.join(ZEPP_CLI_DIR, ".venv", "bin", "python3")
if not os.path.exists(ZEPP_PY):
    ZEPP_PY = "python3"

DB_PATH = os.path.expanduser("~/zepp-bridge/hrv.db")

# your timezone, e.g. "America/New_York"
LOCAL_TZ = "Europe/Rome"

PULL_DAYS = 3

HOST = "0.0.0.0"
PORT = 8765

# must match the Shortcut. keep it alphanumeric, punctuation breaks in URLs
SHARED_SECRET = "CHANGE_ME_random_string"

# ntfy.sh topic to ping when the token dies, or None
NTFY_URL = None

# -----------------------------------------------------------------------------


def local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(LOCAL_TZ)
    except Exception:
        return timezone(timedelta(hours=1))


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hrv (
            ts_ms INTEGER PRIMARY KEY,
            rmssd REAL NOT NULL,
            night TEXT
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta(k, v) VALUES(?, ?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    conn.commit()


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


class TokenExpired(Exception):
    pass


def run_cli(preset, days):
    cmd = [ZEPP_PY, ZEPP_CLI, "events", "--preset", preset,
           "--days", str(days), "--json"]
    proc = subprocess.run(cmd, cwd=ZEPP_CLI_DIR, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if "invalid token" in out or "0102" in out:
        raise TokenExpired(out.strip()[:300])
    if proc.returncode != 0:
        raise RuntimeError(f"CLI exited {proc.returncode}: {out.strip()[:300]}")

    text = proc.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # sometimes it prints one object per line, grab the events one
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "items" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    raise RuntimeError("couldn't parse CLI output")


def base_time_ms(value, wrapper_ts):
    # sample 's' is ms from session start. startTime is the anchor; it sometimes
    # arrives as "5,1788..." so strip the prefix. fall back to the wrapper ts.
    st = value.get("startTime")
    if st is not None:
        if isinstance(st, str):
            st = st.split(",")[-1].strip()
        try:
            st = int(float(st))
            if st > 10_000_000_000:
                return st
            if st > 0:
                return st * 1000
        except (ValueError, TypeError):
            pass
    return int(wrapper_ts)


def parse_rmssd(payload):
    samples = []
    for item in payload.get("items", []):
        value = item.get("value") or {}
        base = base_time_ms(value, item.get("timestamp", 0))
        for s in value.get("samples") or []:
            hrv, off = s.get("hrv"), s.get("s")
            if hrv is None or off is None:
                continue
            try:
                ts = base + int(off)
                rmssd = float(hrv)
            except (ValueError, TypeError):
                continue
            if rmssd <= 0 or rmssd > 400:
                continue
            night = datetime.fromtimestamp(base / 1000, timezone.utc).strftime("%Y-%m-%d")
            samples.append((ts, rmssd, night))
    return samples


def do_pull(verbose=True):
    conn = db()
    try:
        payload = run_cli("hrv-rmssd", PULL_DAYS)
    except TokenExpired as e:
        meta_set(conn, "token_ok", "0")
        meta_set(conn, "last_error", f"token expired (0102): {e}")
        if NTFY_URL:
            try:
                import requests
                requests.post(NTFY_URL,
                              data=b"Zepp token expired - recapture and update config.json",
                              timeout=10)
            except Exception:
                pass
        if verbose:
            print("token expired (0102), recapture it and update config.json", file=sys.stderr)
        return 0

    samples = parse_rmssd(payload)
    inserted = 0
    for ts, rmssd, night in samples:
        cur = conn.execute(
            "INSERT INTO hrv(ts_ms, rmssd, night) VALUES(?,?,?) "
            "ON CONFLICT(ts_ms) DO NOTHING", (ts, rmssd, night))
        inserted += cur.rowcount
    conn.commit()
    meta_set(conn, "token_ok", "1")
    meta_set(conn, "last_error", "")
    meta_set(conn, "last_pull_ms", int(time.time() * 1000))
    total = conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0]
    if verbose:
        print(f"pulled {len(samples)} samples, {inserted} new, {total} total")
    return inserted


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self, qs):
        hdr = self.headers.get("X-Secret", "")
        q = qs.get("secret", [""])[0]
        return not SHARED_SECRET or hdr == SHARED_SECRET or q == SHARED_SECRET

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._auth_ok(qs):
            return self._send(401, {"error": "unauthorized"})
        conn = db()

        if u.path == "/health":
            total = conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0]
            return self._send(200, {
                "token_ok": meta_get(conn, "token_ok", "unknown") == "1",
                "last_pull_ms": int(meta_get(conn, "last_pull_ms", 0) or 0),
                "last_error": meta_get(conn, "last_error", ""),
                "rows": total,
                "acked_upto_ms": int(meta_get(conn, "acked_upto_ms", 0) or 0),
            })

        if u.path == "/hrv":
            since = int(qs.get("since", ["0"])[0] or 0)
            if since == 0:  # no since -> pick up where the last ack left off
                since = int(meta_get(conn, "acked_upto_ms", 0) or 0)
            rows = conn.execute(
                "SELECT ts_ms, rmssd FROM hrv WHERE ts_ms > ? ORDER BY ts_ms ASC LIMIT 5000",
                (since,)).fetchall()
            tz = local_tz()
            samples = [{
                "local": datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d %H:%M:%S"),
                "iso": datetime.fromtimestamp(ts / 1000, timezone.utc)
                          .isoformat().replace("+00:00", "Z"),
                "date": ts // 1000,
                "ts_ms": ts,
                "rmssd": round(v, 2),
            } for ts, v in rows]
            return self._send(200, {
                "count": len(samples),
                "max_ts_ms": samples[-1]["ts_ms"] if samples else since,
                "unit": "ms",
                "samples": samples,
            })

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._auth_ok(qs):
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})

        # Shortcut calls this after logging to Health so we know where to resume
        if u.path == "/ack":
            upto = int(data.get("upto", 0) or 0)
            conn = db()
            prev = int(meta_get(conn, "acked_upto_ms", 0) or 0)
            if upto > prev:
                meta_set(conn, "acked_upto_ms", upto)
            return self._send(200, {"ok": True, "acked_upto_ms": max(upto, prev)})

        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def serve():
    db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"listening on http://{HOST}:{PORT}  (/hrv /health /ack)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Zepp -> Apple Health HRV bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    sub.add_parser("serve")
    p_dump = sub.add_parser("dump")
    p_dump.add_argument("-n", type=int, default=20)
    args = ap.parse_args()

    if args.cmd == "pull":
        do_pull()
    elif args.cmd == "serve":
        serve()
    elif args.cmd == "dump":
        conn = db()
        for ts, v, night in conn.execute(
                "SELECT ts_ms, rmssd, night FROM hrv ORDER BY ts_ms DESC LIMIT ?", (args.n,)):
            iso = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
            print(f"{iso}  rmssd={v:5.1f}  night={night}")


if __name__ == "__main__":
    main()
