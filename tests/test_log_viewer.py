"""
tests/test_log_viewer.py

The log viewer: loopback only, localhost requests only, read-only, and
the filtering it offers.

This directory is protected. Indi can read it but not edit it.
"""

import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import log_viewer

LOG = """2026-09-29 18:52:01,333 WARNING indi.consult: Pre-send check failed to run.
Traceback (most recent call last):
  File "consult.py", line 193, in check
anthropic.BadRequestError: Error code: 400
2026-09-29 18:52:03,084 INFO    indi: Outcome: sent
2026-09-29 18:57:48,138 INFO    indi: No unread messages.
2026-09-29 19:00:00,000 ERROR   indi: something broke
"""


@pytest.fixture
def server(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text(LOG)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), log_viewer.make_handler(log))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def _get(port, path, host="localhost"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": f"{host}:{port}"})
    r = conn.getresponse()
    return r.status, r.read(), dict(r.getheaders())


def test_the_page_and_script_are_served_with_a_strict_policy(server):
    status, body, headers = _get(server, "/")
    assert status == 200 and b"Indi log" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert _get(server, "/app.js")[0] == 200


def test_lines_are_returned_as_data(server):
    status, body, _ = _get(server, "/lines?n=50")
    lines = json.loads(body)["lines"]
    assert status == 200 and lines[-1].endswith("something broke")


def test_problems_only_keeps_whole_entries(server):
    lines = json.loads(_get(server, "/lines?n=50&problems=1")[1])["lines"]
    assert any("Traceback" in l for l in lines)            # the traceback stays with its warning
    assert not any("No unread" in l for l in lines)


def test_the_filter_matches_anywhere_in_an_entry(server):
    lines = json.loads(_get(server, "/lines?n=50&q=badrequest")[1])["lines"]
    assert lines[0].startswith("2026-09-29 18:52:01") and len(lines) == 4


def test_other_hostnames_are_refused(server):
    assert _get(server, "/lines", host="evil.example.com")[0] == 403


def test_it_is_read_only(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("POST", "/lines", body=b"x", headers={"Host": "localhost"})
    assert conn.getresponse().status == 405


@pytest.mark.parametrize("host", ["0.0.0.0", "77.42.46.44", "::", "example.com"])
def test_it_will_not_listen_beyond_loopback(host):
    with pytest.raises(SystemExit):
        log_viewer.loopback_only(host)


def test_a_huge_log_is_read_from_the_end(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("x" * 100 + "\n" + "2026-09-29 19:00:00,000 INFO    indi: last line\n")
    assert log_viewer.tail_lines(log, limit_bytes=60)[-1].endswith("last line")


def test_a_named_private_proxy_host_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("INDI_LOGVIEW_HOSTS", "indi-server.tail1234.ts.net")
    log = tmp_path / "log.txt"
    log.write_text(LOG)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), log_viewer.make_handler(log))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert _get(port, "/lines", host="indi-server.tail1234.ts.net")[0] == 200
        assert _get(port, "/lines", host="other.tail1234.ts.net")[0] == 403
    finally:
        srv.shutdown()
