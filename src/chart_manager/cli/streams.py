"""The one place `cli/` decides which stream a console writes to.

The rule this module exists to enforce:

    The command's selected `--output` projection goes to stdout;
    everything else goes to stderr.

A human-readable table is the selected projection when `--output` is `table`,
so it goes to stdout too. `.github/workflows/ci.yaml` captures CLI stdout into
shell variables, so a stray warning on stdout corrupts the value.

These consoles pass `stderr=`, never `file=`: `Console(file=sys.stdout)` binds
the stream at construction, which bypasses `CliRunner` and
`redirect_stdout`. Ruff's TID251 bans importing `Console` outside the
modules allowlisted in `pyproject.toml`.
"""

from __future__ import annotations

from rich.console import Console


def data_console() -> Console:
    """Console for the selected `--output` projection. Writes to stdout.

    Never quietable: `-q` and `--output json` suppress narration, not the answer.
    """
    return Console(stderr=False)


def set_narration_quiet(quiet: bool) -> None:
    """Silence (or restore) the narration console.

    Callers set it on every invocation, including `False`, so quiet never
    carries over from an earlier command.
    """
    narration.quiet = quiet


# --- the three shared consoles ----------------------------------------------

#: The selected `--output` projection -- tables, listings, JSON documents.
console = data_console()

#: Everything the caller did not ask for as output -- progress, hints, warnings.
narration = Console(stderr=True)

#: Terminal error reporting. Not silenced by `-q`, so a quiet run still says why it failed.
errors = Console(stderr=True)
