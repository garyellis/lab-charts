"""Shared pytest fixtures and the one `CommandRunner` fake.

`chart_root` + `make_chart` build a synthetic `<root>/charts/` tree under
tmp_path. Unit tests that assert against the repo's own `charts/` directory
break every time a chart is added -- which makes a data change look exactly
like a code regression, and cost this suite three red tests. Build the tree
the test needs instead, and keep real-tree coverage to explicit smoke tests
that assert containment rather than an inventory.

`FakeCommandRunner` is the single record-and-replay subprocess seam. See its
docstring for why there is exactly one of it.

`cli()` is the single seam through which tests name a CLI command. See
`_COMMAND_PATHS` for why the suite never writes a group name into an
`invoke()` call directly.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import typer
import typer.main
import yaml
from typer.testing import CliRunner, Result

from chart_manager.plumbing.commands import CommandResult, redact
from chart_manager.plumbing.errors import ExternalCommandError

#: Repo root, anchored to this file rather than the process cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]

MakeChart = Callable[..., Path]


@pytest.fixture(autouse=True)
def hermetic_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin Rich's terminal detection so rendered output is environment-independent.

    Rich inspects the ambient environment to decide width and colour. Under
    `GITHUB_ACTIONS` it forces an 80-column terminal *with* ANSI codes even
    with no TTY attached, so Typer's `--help` output wraps differently and
    every `assert "--format" in result.output` in this suite fails -- 12 of
    them, green locally and red in CI, with nothing about the code changed.

    Neutralising it here rather than per-test keeps the fixed point in one
    place: a test that asserts on rendered text is asserting about *our*
    formatting, never about the terminal it happens to run in. Tests that
    genuinely exercise CI-detection (`--output auto`, `--github-step-summary`)
    set the variables they need explicitly, and those `monkeypatch.setenv`
    calls run after this fixture, so they still win.
    """
    for var in (
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_TOKEN",
        "RENOVATE_TOKEN",
        "FORCE_COLOR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("NO_COLOR", "1")
    # Unit and integration tests must never inherit a developer's external
    # event sink configuration and persist records in Cosmos DB.
    monkeypatch.setenv("EVENTS_BACKEND", "none")


@pytest.fixture
def chart_root(tmp_path: Path) -> Path:
    """An empty repo root containing a `charts/` directory."""
    (tmp_path / "charts").mkdir()
    return tmp_path


@pytest.fixture
def make_chart(chart_root: Path) -> MakeChart:
    """Write a minimal Helm chart with enabled cluster tests into ``chart_root``.

    `profiles` is the raw cluster-test profile mapping, so tests express
    requirements exactly as a chart author would:

        make_chart("alloy", profiles={"minimal": {"requires": [{"chart": "prom"}]}})

    Every values file any profile references is created empty, since
    `ClusterTestCatalog.value_paths` requires them to exist.
    """

    def build(
        name: str,
        *,
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
        values: Sequence[str] = ("values.yaml",),
        version: str = "0.1.0",
    ) -> Path:
        chart_dir = chart_root / "charts" / name
        chart_dir.mkdir(parents=True, exist_ok=True)
        (chart_dir / "Chart.yaml").write_text(
            yaml.safe_dump({"apiVersion": "v2", "name": name, "version": version}),
            encoding="utf-8",
        )

        spec_profiles: dict[str, Any] = dict(profiles or {"minimal": {}})
        referenced = set(values)
        for profile in spec_profiles.values():
            referenced.update(profile.get("values", []))
        for value_file in sorted(referenced):
            (chart_dir / value_file).write_text("", encoding="utf-8")

        (chart_dir / "chart-lifecycle.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "lifecycle.cmg.io/v1alpha1",
                    "kind": "ChartLifecycle",
                    "metadata": {"name": name},
                    "spec": {
                        "enabled": True,
                        "clusterTest": {
                            "enabled": True,
                            "profiles": spec_profiles,
                            "dependentTests": [],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        return chart_dir

    return build


# --- the CLI argv seam -------------------------------------------------------
#
# Every test that drives the CLI names a command as a sequence of argv
# tokens, and Typer resolves those tokens against the registered command
# tree. A rename in `cli/main.py` therefore breaks every test that spelled
# the old name -- silently at the source level, loudly and in bulk at run
# time. Before this seam existed, ~49 assertion sites across nine modules
# each carried a literal group name, so renaming one group was a nine-file
# diff mixed in with the rename that motivated it, and the review could not
# tell a mechanical edit from a behavioural one.
#
# The invariant this seam buys:
#
#     A test names a command in the vocabulary it was WRITTEN in.
#     `_COMMAND_PATHS` translates that vocabulary into the one the app
#     currently registers.
#
# So renaming `charts list` to `chart list` is one edit -- the value of the
# `("charts",)` entry below -- and every test that says `cli("charts",
# "list", ...)` keeps passing, unmodified, still asserting exactly what it
# asserted before. The keys are frozen history; only the values move.


#: Test vocabulary -> the command path the app actually registers.
#:
#: Longest matching prefix wins, so a group rename is one entry and a command
#: that *moves between groups* (`validate run` -> `chart validate`) is a
#: second, more specific entry that overrides it. The right-hand side is a
#: full argv prefix, not a single token, which is what lets a command that
#: gains a flag in its new home (`ci cluster-test-matrix` -> `plan -o
#: github`) be expressed here rather than at 7 call sites.
#:
#: Entries are identity today: nothing has been renamed yet. That is the
#: point -- the table is installed *before* the rename wave so the wave is a
#: table diff. `tests/test_cli_argv_table.py` guards it both ways: every
#: right-hand side must resolve against the live app, and every command the
#: app registers must appear here, so a new group cannot quietly bypass the
#: seam.
_COMMAND_PATHS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("charts",): ("charts",),
    ("ci",): ("ci",),
    ("events",): ("events",),
    ("grafana",): ("grafana",),
    ("helmrelease",): ("helmrelease",),
    ("local",): ("local",),
    ("publish",): ("publish",),
    ("upgrade",): ("upgrade",),
    ("validate",): ("validate",),
    ("version",): ("version",),
    # FROZEN. `renovate-global.json` pins the literal string
    # `chart-manager upgrade-finalize --path <dir>` in a security allowlist
    # regex, flag order included. This value must never change; the entry
    # exists so that intent is visible here rather than only in a doc.
    ("upgrade-finalize",): ("upgrade-finalize",),
}


def _root_app() -> typer.Typer:
    """The real CLI app, imported lazily.

    Lazily so that a conftest import -- which every test in the suite pays
    for, including the ones that never touch the surface -- does not drag
    Rich, Typer's command tree and the whole service layer into the process,
    and so that an import error in `cli/` fails the CLI tests rather than
    collection of the entire suite.
    """
    from chart_manager.cli.main import app

    return app


def _global_option_arity() -> dict[str, int]:
    """Long/short option -> how many argv tokens follow it, for root options.

    Read off the live root callback rather than hard-coded: the set of
    global options is exactly what Click will consume before it starts
    looking for a subcommand, so deriving it here keeps `resolve_argv`
    correct when a global option is added or removed.
    """
    command = typer.main.get_command(_root_app())
    arity: dict[str, int] = {}
    for param in command.params:
        takes_value = not (getattr(param, "is_flag", False) or getattr(param, "count", False))
        for opt in param.opts + param.secondary_opts:
            arity[opt] = param.nargs if takes_value else 0
    return arity


def _split_leading_options(argv: Sequence[str], arity: Mapping[str, int]) -> int:
    """Index of the first token Click would treat as the command path.

    An unrecognised leading option stops the scan instead of being skipped:
    `cli("-o", "json", "version")` asserts that no global `-o` exists, and
    must reach the app spelled exactly as the test wrote it.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-") or token == "-" or token == "--":
            break
        name, _, inline_value = token.partition("=")
        if name in arity:
            index += 1 if inline_value else 1 + arity[name]
            continue
        # Clustered short flags, e.g. `-vv` for a counted `-v`.
        if (
            not token.startswith("--")
            and len(token) > 2
            and all(arity.get(f"-{char}") == 0 for char in token[1:])
        ):
            index += 1
            continue
        break
    return index


def resolve_argv(argv: Sequence[str]) -> list[str]:
    """Translate one argv from the test vocabulary into the app's.

    Exposed separately from `cli()` so the translation itself can be
    asserted on (`tests/test_cli_argv_table.py`).
    """
    tokens = list(argv)
    start = _split_leading_options(tokens, _global_option_arity())
    path = tokens[start:]
    for length in range(min(len(path), max((len(k) for k in _COMMAND_PATHS), default=0)), 0, -1):
        replacement = _COMMAND_PATHS.get(tuple(path[:length]))
        if replacement is not None:
            return [*tokens[:start], *replacement, *path[length:]]
    return tokens


def cli(*argv: str, input: str | None = None, catch_exceptions: bool = True) -> Result:
    """Invoke the real CLI with `argv` written in the test's own vocabulary.

    Use this instead of `CliRunner().invoke(main.app, [...])` everywhere, so
    a command rename stays a `_COMMAND_PATHS` diff.

    Deliberately offers no `app=` override. `_COMMAND_PATHS` is expressed in
    *root-app* paths, and a module that assembles a partial app from a
    `cli/*.py` `register()` function (`tests/test_cli_publish.py`,
    `tests/test_cli_upgrade.py`, `tests/test_cli_helmrelease.py`) registers
    those commands flat, with no group above them. Translating a root path
    into such an app would rewrite `publish` to `chart publish` against an
    app where only `publish` exists. Those modules are already insulated --
    `register()` owns the command name, `main.py` owns the group name -- so
    they keep a plain `CliRunner` and need nothing from this table.
    """
    return CliRunner().invoke(
        _root_app(),
        resolve_argv(argv),
        input=input,
        catch_exceptions=catch_exceptions,
    )


# --- the command-runner seam -------------------------------------------------

#: Decides whether a scripted response applies to one invocation's argv.
Predicate = Callable[[tuple[str, ...]], bool]

#: A leading-argv prefix is accepted anywhere a Predicate is, since almost
#: every real match is "this is the `docker ps` call" rather than a
#: computation over the whole argv.
Matcher = Predicate | tuple[str, ...]


@dataclass(frozen=True)
class Reply:
    """One scripted subprocess outcome."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class RecordedCall:
    """One `run()` invocation: argv plus every keyword the caller passed.

    Recording the keywords is the point, not incidental. Cluster addressing,
    per-subprocess timeouts and per-request environment are all expressed as
    keywords, so a fake that captures argv alone cannot distinguish a
    correctly-scoped call from one that silently inherited process-global
    state -- which is exactly the class of bug this seam exists to catch.
    """

    args: tuple[str, ...]
    cwd: Path | None
    check: bool
    capture: bool
    timeout: float | None
    env: Mapping[str, str] | None


class FakeCommandRunner:
    """Record-and-replay `CommandRunner` for adapter tests.

    There is one of these, deliberately. Every adapter test file used to
    carry its own fake that subclassed the (then concrete) `CommandRunner`
    and hand-copied the `run` signature. Adding a single keyword to the seam
    broke all of them at once, so the seam could not evolve -- which is the
    documented reason `env` was never added and why `Kubectl`/`Kind` had
    nowhere to put a cluster address. This fake satisfies the Protocol
    structurally; a signature change is one edit here.

    Response resolution, first hit wins:

      1. the scripted queue (`script`), consumed in call order;
      2. the predicate table (`respond`), first matching matcher;
      3. the constructor default.

    `when_exhausted` says what a drained queue means: ``"default"`` falls
    through to 2/3, ``"repeat"`` replays the last scripted reply (poll loops
    that must keep answering), ``"raise"`` fails the test on an unscripted
    call.

    `check=True` failures raise `ExternalCommandError` with the same message
    shape and the same populated `stderr`/`returncode` as `SubprocessRunner`.
    Fakes that were *more* capable than production once let a consumer pass
    its tests and read `None` at runtime; keep the two in step.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        when_exhausted: Literal["default", "repeat", "raise"] = "default",
    ) -> None:
        """Set the fall-through reply and the drained-queue policy."""
        self.records: list[RecordedCall] = []
        self._default = Reply(returncode=returncode, stdout=stdout, stderr=stderr)
        self._table: list[tuple[Predicate, list[Reply]]] = []
        self._queue: list[Reply] = []
        self._when_exhausted = when_exhausted
        self._last: Reply | None = None

    # --- scripting ----------------------------------------------------------

    def respond(
        self,
        matcher: Matcher,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> FakeCommandRunner:
        """Answer every argv matching `matcher` with this reply. Chainable."""
        return self.respond_each(
            matcher, Reply(returncode=returncode, stdout=stdout, stderr=stderr)
        )

    def respond_each(self, matcher: Matcher, *replies: Reply) -> FakeCommandRunner:
        """Answer successive matching calls with successive replies.

        The final reply repeats, so a caller that polls one command until it
        converges is expressed as the responses that matter followed by
        nothing, rather than as a call count. Chainable.
        """
        if not replies:
            raise ValueError("respond_each needs at least one reply")
        self._table.append((_as_predicate(matcher), list(replies)))
        return self

    def script(self, *replies: Reply) -> FakeCommandRunner:
        """Queue replies consumed in call order, regardless of argv. Chainable."""
        self._queue.extend(replies)
        return self

    # --- inspection ---------------------------------------------------------

    @property
    def calls(self) -> list[tuple[str, ...]]:
        """Argv of every invocation, in order -- the common assertion."""
        return [record.args for record in self.records]

    # --- the seam -----------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Record the invocation and replay the matching scripted reply."""
        argv = tuple(args)
        self.records.append(
            RecordedCall(
                args=argv,
                cwd=cwd,
                check=check,
                capture=capture,
                timeout=timeout,
                env=env,
            )
        )
        reply = self._reply_for(argv)
        result = CommandResult(
            args=argv,
            returncode=reply.returncode,
            stdout=reply.stdout,
            stderr=reply.stderr,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ExternalCommandError(
                f"command failed ({result.returncode}): {redact(argv)}\n{detail}",
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return result

    def _reply_for(self, argv: tuple[str, ...]) -> Reply:
        """Resolve queue -> table -> default, honoring `when_exhausted`."""
        if self._queue:
            self._last = self._queue.pop(0)
            return self._last
        if self._when_exhausted == "raise":
            raise AssertionError(f"unscripted call: {argv}")
        if self._when_exhausted == "repeat" and self._last is not None:
            return self._last
        for predicate, replies in self._table:
            if predicate(argv):
                return replies.pop(0) if len(replies) > 1 else replies[0]
        return self._default


def _as_predicate(matcher: Matcher) -> Predicate:
    """Coerce a leading-argv prefix into a predicate; pass callables through."""
    if isinstance(matcher, tuple):
        prefix = matcher
        return lambda argv: argv[: len(prefix)] == prefix
    return matcher
