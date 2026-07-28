"""Shared pytest fixtures and the one `CommandRunner` fake.

`chart_root` + `make_chart` build a synthetic `<root>/charts/` tree under
tmp_path. Unit tests that assert against the repo's own `charts/` directory
break every time a chart is added -- which makes a data change look exactly
like a code regression, and cost this suite three red tests. Build the tree
the test needs instead, and keep real-tree coverage to explicit smoke tests
that assert containment rather than an inventory.

`FakeCommandRunner` is the single record-and-replay subprocess seam. See its
docstring for why there is exactly one of it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

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
