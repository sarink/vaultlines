"""vl's messages and docs name only commands that exist, in vl's own words."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from vaultlines import cli

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted([*ROOT.glob("src/vaultlines/**/*.py"), ROOT / "README.md", *ROOT.glob("docs/*.md")])


def commands() -> dict[str, set[str]]:
    """{command: its actions} from vl's parser."""
    import argparse

    parser = cli.build_parser([])
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    out = {}
    for name, p in sub.choices.items():
        inner = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        out[name] = set(inner[0].choices) if inner else set()
    return out


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_vl_command_named_exists(path):
    known = commands()
    bad = []
    for m in re.finditer(r"`vl ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", path.read_text()):
        command, action = m.groups()
        if command not in known or (action and known[command] and action not in known[command]):
            bad.append(m.group(0))
    assert bad == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_one_word_for_each_thing(path):
    text = path.read_text().lower()
    for old, word in [("sign in", "log in"), ("signed in", "logged in"), ("sign-in", "login"), ("fill job", "refresh job")]:
        assert old not in text, f"say {word!r}"


# Real names that must never appear: a real company, its people, repos and drive. Written in
# pieces, so this file doesn't name them either.
REAL = ["mix" + "im", "jor" + "ge", "shee" + "ty", "0AHF" + "8p0HI9"]
TRACKED = sorted(ROOT / f for f in subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, check=True,
                                                  text=True).stdout.split())


def test_no_real_company_anywhere():
    """Code, help, tests, docs and comments use acme, never a real company."""
    found = []
    for path in TRACKED:
        try:
            text = path.read_text().lower()
        except (UnicodeDecodeError, OSError):
            continue  # binary fixtures
        found += [f"{path.relative_to(ROOT)}: {word}" for word in REAL if word.lower() in text]
    assert found == []
