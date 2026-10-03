"""vl's messages and docs name only commands that exist, in vl's own words."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from vaultlines import cli

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted([*ROOT.glob("src/vaultlines/**/*.py"), ROOT / "README.md", *ROOT.glob("docs/*.md")])


def parsers(parser=None) -> list:
    """vl's parser and every subcommand's, with a [source] kind's keys as flags of `vl vault create`."""
    import argparse

    from vaultlines import plugins

    found = [parser]
    for p in [parser] if parser else [cli.build_parser(["vault", "create", "--source", k]) for k in plugins.SOURCES]:
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for child in a.choices.values():
                    found += parsers(child)
    return [p for p in found if p]


def keys() -> set[str]:
    """Every key: vault.toml's, and each [source] kind's."""
    from vaultlines import plugins, vaults

    return {*vaults.KEYS, *(k for kind in plugins.SOURCES.values() for k in kind.OPTIONS)}


def test_the_flag_rule():
    """A flag with _ is a key (--notes_from, --folder_id); a flag with - is only a command option."""
    known = keys()
    flags = {f for p in parsers() for a in p._actions for f in a.option_strings if f.startswith("--")}
    assert "--folder_id" in flags and "--fetch-only" in flags
    for flag in flags:
        name = flag[2:]
        if "_" in name:
            assert name in known, f"{flag} has _ but isn't a key: spell it with -"
        assert name.replace("-", "_") not in known or "-" not in name, f"{flag} is a key: spell it with _"


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


# The old API. A vault is always its ID, OWNER/REPO: never a short name like acme-public.
SHORT_NAME = re.compile(r"(?<![\w/.-])(?:acme|kabir|sam|alice|bob|local)-(?:public|private|hq|drive|design|founders|"
                        r"everyone|recipes|side|personal|[a-z]+-personal)(?![\w-])")
GONE = ["vl org ", "--publish", "vaultlines_", "google_client_", "this computer only"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_old_names(path):
    text = path.read_text()
    assert SHORT_NAME.findall(text) == [], "a vault is OWNER/REPO"
    assert [word for word in GONE if word in text.lower()] == []


@pytest.mark.parametrize("path", [p for p in FILES if p.name != "gdrive.py"], ids=lambda p: str(p.relative_to(ROOT)))
def test_the_gdrive_setup_steps_are_only_in_its_guide(path):
    """`vl vault create --source gdrive --help` shows them; nothing else repeats them."""
    assert "console.cloud.google.com" not in path.read_text()


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
