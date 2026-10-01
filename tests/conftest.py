"""
tests/conftest.py

config.py loads the live .env at import, so any setting there reaches
every test that doesn't pin it. INDI_CONSULT is one that changes the
reply path's shape — it sends the pre-send check to a store the fakes in
test_reply_all.py never needed to implement — so a machine with
consultation switched on failed those tests for a reason that had nothing
to do with them. Each test starts with it off; the tests that want it on
say so by patching `consult.enabled`, as test_consult.py already does.

This only makes the suite independent of the machine it runs on. No
assertion anywhere is changed by it.
"""

import pytest


@pytest.fixture(autouse=True)
def consultation_off_unless_asked(monkeypatch):
    monkeypatch.setenv("INDI_CONSULT", "false")
    for suffix in ("_LINUX", "_WINDOWS", "_MACOS"):
        monkeypatch.delenv("INDI_CONSULT" + suffix, raising=False)


@pytest.fixture(autouse=True)
def plain_text_mail_unless_asked(monkeypatch):
    """Most tests read the plain-text body straight off the message they
    capture. Replies now also carry an HTML copy (gmail_io.text_to_html);
    those tests run with it off, and test_html_email.py turns it on."""
    monkeypatch.setenv("INDI_HTML_EMAIL", "false")
