#!/usr/bin/env python3
"""
log_viewer.py

A small web page that shows the end of log.txt and follows it live, with
a text filter and a warnings-and-errors switch.

It listens on the server's own loopback address only (127.0.0.1) and is
reached through an SSH tunnel — never from the internet. The log quotes
correspondents' mail, so it must not be public; an SSH tunnel gives the
viewer the same protection as logging in: your SSH key.

From Windows (PowerShell):

    ssh -N -L 8765:127.0.0.1:8765 indi@77.42.46.44

and while that runs, open http://localhost:8765 in a browser. Ctrl+C in
PowerShell closes the tunnel.

From an iPhone, put Tailscale on the server and the phone, run
`tailscale serve --bg 8765` on the server, and list the server's
Tailscale name in INDI_LOGVIEW_HOSTS (see that function). The viewer
still listens on loopback only; Tailscale carries the phone's request to
it over the private tailnet, never the public internet. Never use
`tailscale funnel` for this: that would make it public.

Other safeguards: it refuses to start on any address but loopback; it
answers only requests addressed to localhost (so a web page elsewhere
can't reach it through DNS tricks); it only ever reads the one log file;
nothing it serves runs code from the log (lines are shown as text); and
the systemd unit (deploy/indi-logview.service) runs it read-only with
network access limited to loopback. Standard library only.

It is protected from self-modification: it decides who can read the log.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8765
MAX_LINES = 5000
READ_LIMIT = 8 * 1024 * 1024        # never read more than the last 8 MB
_ENTRY_START = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def allowed_hosts() -> set[str]:
    """localhost, plus any names in INDI_LOGVIEW_HOSTS (comma-separated) —
    for a private proxy in front of the viewer, such as `tailscale serve`,
    which passes on the name the phone asked for. Exact names only."""
    extra = {h.strip().lower() for h in os.environ.get("INDI_LOGVIEW_HOSTS", "").split(",")
             if h.strip()}
    return _ALLOWED_HOSTS | extra


# ---------------------------------------------------------------------------
# reading the log
# ---------------------------------------------------------------------------

def tail_lines(path: Path, limit_bytes: int = READ_LIMIT) -> list[str]:
    """The last `limit_bytes` of the file, as whole lines."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    with path.open("rb") as fh:
        start = max(0, size - limit_bytes)
        fh.seek(start)
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]           # the first line was cut part-way
    return lines


def entries(lines: list[str]) -> list[list[str]]:
    """Group lines into log entries: a timestamped line plus the lines
    that follow it without one (tracebacks, multi-line messages)."""
    out: list[list[str]] = []
    for line in lines:
        if _ENTRY_START.match(line) or not out:
            out.append([line])
        else:
            out[-1].append(line)
    return out


def select(lines: list[str], n: int, query: str = "", problems_only: bool = False) -> list[str]:
    """The last `n` lines of the entries that match."""
    query = (query or "").strip().lower()
    kept = []
    for entry in entries(lines):
        head = entry[0]
        if problems_only and " WARNING " not in head and " ERROR " not in head \
                and " CRITICAL " not in head:
            continue
        if query and not any(query in line.lower() for line in entry):
            continue
        kept.extend(entry)
    n = max(1, min(n, MAX_LINES))
    return kept[-n:]


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Indi log</title>
<style>
:root { --bg:#fbfbf9; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e3de;
        --warn:#8a5a00; --warnbg:#fff4dc; --err:#a1261b; --errbg:#fde8e5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161615; --fg:#e8e6e1; --muted:#9a988f; --line:#2c2b28;
          --warn:#f2c46b; --warnbg:#3a2e12; --err:#ff9a8f; --errbg:#40201c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line);
         padding:10px 14px; display:flex; flex-wrap:wrap; gap:10px 16px; align-items:center; }
header h1 { font-size:15px; margin:0 8px 0 0; }
label { color:var(--muted); display:flex; gap:6px; align-items:center; }
input[type=search] { width:16em; padding:4px 8px; border:1px solid var(--line);
                     border-radius:6px; background:transparent; color:var(--fg); }
select { padding:3px 6px; border:1px solid var(--line); border-radius:6px;
         background:transparent; color:var(--fg); }
#status { margin-left:auto; color:var(--muted); font-size:12px; }
pre { margin:0; padding:10px 14px 40px; font: 12.5px/1.45 ui-monospace, Menlo, Consolas, monospace;
      white-space:pre-wrap; word-break:break-word; }
.l { display:block; }
.WARNING { color:var(--warn); background:var(--warnbg); }
.ERROR, .CRITICAL { color:var(--err); background:var(--errbg); }
.cont { color:var(--muted); }
</style></head>
<body>
<header>
  <h1>Indi log</h1>
  <label>Filter <input id="q" type="search" placeholder="text to match"></label>
  <label><input id="problems" type="checkbox"> Warnings &amp; errors only</label>
  <label>Lines <select id="n"><option>200</option><option selected>500</option>
    <option>2000</option><option>5000</option></select></label>
  <label><input id="live" type="checkbox" checked> Follow</label>
  <span id="status">loading…</span>
</header>
<pre id="log"></pre>
<script src="/app.js"></script>
</body></html>
"""

APP_JS = """
const $ = id => document.getElementById(id);
let timer = null, last = "";

function render(lines) {
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
  const pre = $("log"); pre.textContent = "";
  let level = "";
  for (const line of lines) {
    const m = /^\\d{4}-\\d\\d-\\d\\d [\\d:,]+ (\\w+)/.exec(line);
    const span = document.createElement("span");
    span.className = "l " + (m ? (level = m[1]) : "cont " + level);
    span.textContent = line + "\\n";          // text, never HTML
    pre.appendChild(span);
  }
  if (atBottom || !last) window.scrollTo(0, document.body.scrollHeight);
}

async function refresh() {
  const p = new URLSearchParams({ n: $("n").value, q: $("q").value,
                                  problems: $("problems").checked ? "1" : "" });
  try {
    const r = await fetch("/lines?" + p, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    const key = JSON.stringify(data.lines);
    if (key !== last) { render(data.lines); last = key; }
    $("status").textContent = `${data.lines.length} lines · log ${data.size_kb} KB · ` +
      `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) { $("status").textContent = "can't reach the viewer (" + e.message + ")"; }
}

function schedule() { clearInterval(timer); if ($("live").checked) timer = setInterval(refresh, 5000); }
let debounce;
$("q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => { last = ""; refresh(); }, 300); });
for (const id of ["problems", "n"]) $(id).addEventListener("change", () => { last = ""; refresh(); });
$("live").addEventListener("change", schedule);
refresh(); schedule();
"""

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; "
                               "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def make_handler(log_path: Path, hosts: set[str] | None = None):
    hosts = set(hosts or allowed_hosts())

    class Handler(BaseHTTPRequestHandler):
        server_version = "indi-log-viewer"
        sys_version = ""

        def log_message(self, fmt, *args):      # keep the viewer out of its own log
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            if host not in hosts:
                # A page on some other site pointing a name at 127.0.0.1
                # arrives with that name here; refuse it.
                return self._send(403, b"forbidden", "text/plain; charset=utf-8")
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if url.path == "/app.js":
                return self._send(200, APP_JS.encode(), "text/javascript; charset=utf-8")
            if url.path == "/lines":
                qs = parse_qs(url.query)
                try:
                    n = int((qs.get("n") or ["500"])[0])
                except ValueError:
                    n = 500
                lines = select(tail_lines(log_path), n, (qs.get("q") or [""])[0][:200],
                               bool((qs.get("problems") or [""])[0]))
                try:
                    size_kb = round(log_path.stat().st_size / 1024)
                except FileNotFoundError:
                    size_kb = 0
                body = json.dumps({"lines": lines, "size_kb": size_kb}).encode()
                return self._send(200, body, "application/json")
            return self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            self._send(405, b"read-only", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def loopback_only(host: str) -> None:
    try:
        ok = ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = host == "localhost"
    if not ok:
        raise SystemExit(f"Refusing to listen on {host!r}: the log quotes people's mail, "
                         "so the viewer only listens on loopback. Reach it with an SSH tunnel.")


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Serve the Indi log on localhost.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--log", type=Path,
                   default=Path(os.environ.get("INDI_LOG_PATH") or here / "log.txt"))
    args = p.parse_args(argv)
    loopback_only(args.host)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.log))
    print(f"Serving {args.log} on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
