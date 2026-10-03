"""
tests/test_prompt_privacy.py

Indi is told how its sharing rules work and not to claim who said what
without evidence, so that it describes its own behaviour truthfully.

This directory is protected. Indi can read it but not edit it.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audience
import check_and_reply


def test_the_system_prompt_explains_sharing_and_accuracy(tmp_path):
    prompt_file = tmp_path / "system_prompt.txt"
    prompt_file.write_text("You are Indi.")
    cfg = SimpleNamespace(system_prompt_path=prompt_file, send_automatically=True,
                          enable_outbound=True)
    text = check_and_reply.load_system_prompt(cfg, "indi@example.org")
    assert text.startswith("You are Indi.")
    assert "HOW SHARING WORKS" in text and "ACCURACY ABOUT WHO SAID WHAT" in text
    assert "schedule_follow_up" in text
    assert "isn't used when I write to others" in audience.PROMPT
