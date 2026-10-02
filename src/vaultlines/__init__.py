"""Personal and team markdown vaults for Claude Code, guarded by a session label."""

__version__ = "0.4.0"


def main() -> None:
    """The `vl` command. `vl hook` runs on every matching tool call, so it skips the CLI's imports."""
    import sys

    if sys.argv[1:2] == ["hook"]:
        from .hook import main as hook_main

        hook_main()
    else:
        from .cli import main as cli_main

        cli_main()
