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

import io
import logging
import tarfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import typer
import typer.main
from typer.testing import CliRunner, Result

from chart_manager.api.v1alpha1.chart_workspace import ChartWorkspaceSpec
from chart_manager.cli._container import reset_invocation
from chart_manager.plumbing.commands import CommandResult, redact
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.shared.workspace import RepositoryWorkspace

#: Repo root, anchored to this file rather than the process cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The conventional layout, for domain loaders that take it explicitly.
CHARTS_DIR = Path("charts")
LOCAL_CONFIG = Path(".chart-manager/local-cluster.yaml")
POLICIES_DIR = Path("policies")
RENDER_DIR = Path(".chart-manager/rendered")

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


@pytest.fixture(autouse=True)
def hermetic_logging() -> Iterator[None]:
    """Undo any process-wide logging configuration a test installs.

    `setup_logging` is deliberately process-wide (`logging.basicConfig(
    force=True)`), so a single CLI test that exercises `-v` for real leaves a
    `RichLogHandler` on the *root* logger, bound to the `CliRunner` stderr
    buffer that is closed the moment that test returns. Every later test that
    logs anything then writes into a closed stream, and `logging` swallows the
    result as "--- Logging error ---" on stderr.

    That was invisible while `services/` emitted no log records. It is not
    invisible now, and the fix belongs here rather than in each test: the
    leaked state is global, and no test should have to know which earlier one
    configured logging.
    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)


@pytest.fixture(autouse=True)
def fresh_cli_invocation() -> Iterator[None]:
    """Drop the CLI's per-invocation `Container` when a test ends."""
    try:
        yield
    finally:
        reset_invocation()


@pytest.fixture(autouse=True)
def hermetic_workspace_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's `CHART_MANAGER_ROOT` from redirecting CLI tests."""
    monkeypatch.delenv("CHART_MANAGER_ROOT", raising=False)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> None:
    """Mark `tmp_path` as a chart repository (use via `pytest.mark.usefixtures`)."""
    write_workspace(tmp_path)


def _spec_body(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The conventional layout, with camelCase ``spec`` keys overriding it."""
    return {
        "chartsDir": CHARTS_DIR.as_posix(),
        "localCluster": LOCAL_CONFIG.as_posix(),
        "renderDir": RENDER_DIR.as_posix(),
        "policiesDir": POLICIES_DIR.as_posix(),
        **spec,
    }


def workspace_for(root: Path, *, name: str = "test-workspace", **spec: Any) -> RepositoryWorkspace:
    """A validated `RepositoryWorkspace` over ``root``, without touching disk."""
    return RepositoryWorkspace(
        root=root.resolve(),
        name=name,
        spec=ChartWorkspaceSpec.model_validate(_spec_body(spec)),
    )


def write_workspace(root: Path, **spec: Any) -> Path:
    """Write a minimal `.chart-manager/workspace.yaml` under ``root``; return its path."""
    marker = root / ".chart-manager" / "workspace.yaml"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartWorkspace",
                "metadata": {"name": "test-workspace"},
                "spec": _spec_body(spec),
            }
        ),
        encoding="utf-8",
    )
    return marker


@pytest.fixture
def chart_root(tmp_path: Path) -> Path:
    """An empty repo root: a `charts/` directory and a workspace.yaml."""
    (tmp_path / "charts").mkdir()
    write_workspace(tmp_path)
    return tmp_path


@pytest.fixture
def make_chart(chart_root: Path) -> MakeChart:
    """Write a minimal Helm chart with enabled cluster tests into ``chart_root``.

    `profiles` is the raw cluster-test profile mapping, so tests express
    requirements exactly as a chart author would:

        make_chart("alloy", profiles={"minimal": {"requires": [{"chart": "prom"}]}})

    Every values file any profile references is created empty, since
    `ChartTestCatalog.value_paths` requires them to exist. A profile that
    names no `namespace` is written with `default`, because the lifecycle
    API requires one and most tests have no opinion about it.
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
            dump_yaml({"apiVersion": "v2", "name": name, "version": version}),
            encoding="utf-8",
        )

        spec_profiles: dict[str, Any] = {
            profile_name: {"namespace": "default", **profile}
            for profile_name, profile in (profiles or {"minimal": {}}).items()
        }
        referenced = set(values)
        for profile in spec_profiles.values():
            referenced.update(profile.get("values", []))
        for value_file in sorted(referenced):
            (chart_dir / value_file).write_text("", encoding="utf-8")

        (chart_dir / "chart-lifecycle.yaml").write_text(
            dump_yaml(
                {
                    "apiVersion": "chartmanager.io/v1alpha1",
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


def write_validation_chart(root: Path, name: str, **validation: Any) -> Path:
    """Write a chart whose `spec.validation` is `validation` over a `dev` default."""
    chart = root / "charts" / name
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(dump_yaml({"apiVersion": "v2", "name": name, "version": "0.1.0"}))
    (chart / "values.yaml").write_text("")
    spec = {
        "releaseName": name,
        "namespaceTemplate": "lab-${env}",
        "environments": {"dev": {"values": ["values.yaml"]}},
        **validation,
    }
    (chart / "chart-lifecycle.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": name},
                "spec": {"validation": spec},
            }
        )
    )
    return chart


def crd_manifest(*, nested_type: str = "string") -> str:
    """A CustomResourceDefinition for example.io/v1 Widget."""
    return f"""apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.io
spec:
  group: example.io
  names:
    kind: Widget
    plural: widgets
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                name:
                  type: {nested_type}
                labels:
                  type: object
                  additionalProperties:
                    type: string
                arbitrary:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
    - name: v1beta1
      served: false
      storage: false
      schema:
        openAPIV3Schema:
          type: object
"""


#: Chart.lock for one `foo 1.0.0` dependency on https://example.test/charts.
ONE_DEPENDENCY_LOCK = (
    "dependencies:\n"
    "  - name: foo\n"
    "    version: 1.0.0\n"
    "    repository: https://example.test/charts\n"
    "digest: sha256:ac904eb48ba9649a9d5261dfc887cd08080cdddfd2e7bca3217ff88cfaadb27b\n"
)


def materialize_dependency(
    chart: Path,
    name: str = "foo",
    version: str = "1.0.0",
    *,
    helm_gzip_extra: bool = False,
) -> None:
    """Create a minimal real Helm package under ``charts/``."""
    chart_yaml = (
        f"apiVersion: v2\nname: {name}\nversion: {version}\n"
    ).encode()
    info = tarfile.TarInfo(f"{name}/Chart.yaml")
    info.size = len(chart_yaml)
    package = chart / "charts" / f"{name}-{version}.tgz"
    with tarfile.open(package, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(chart_yaml))
    if helm_gzip_extra:
        compressed = package.read_bytes()
        # Helm's Go gzip writer includes FEXTRA. Insert a minimal valid extra
        # field into Python's otherwise equivalent gzip header.
        package.write_bytes(
            compressed[:3]
            + bytes([compressed[3] | 0x04])
            + compressed[4:10]
            + b"\x04\x00HELM"
            + compressed[10:]
        )


@pytest.fixture
def schema_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepositoryWorkspace:
    """A workspace whose locked schema generation is synced into a tmp cache."""
    from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
    from chart_manager.commands.validate.schemas.store import (
        KubeconformSchemaStore,
        default_schema_cache_root,
    )
    from chart_manager.shared.workspace import SCHEMA_LOCK_FILE
    from tests import schema_fixtures  # imports this module

    lock, _, snapshots = schema_fixtures.schema_store(tmp_path / "upstream")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    KubeconformSchemaStore(cache_root=default_schema_cache_root(), snapshots=snapshots).sync(lock)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    return schema_fixtures.workspace(tmp_path)


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
#     A rename is one table edit first, and a call-site sweep second, and the
#     suite is green after each.
#
# Renaming `charts list` to `chart list` starts as one edit -- point the
# `("charts",)` entry at `("chart",)` -- and every `cli("charts", "list", ...)`
# keeps passing unmodified, still asserting exactly what it asserted before.
# That decouples "did the rename break behaviour?" from "did the sweep touch
# the right lines?", which is the whole value: the reviewer reads two small
# diffs instead of one large mixed one.
#
# The second half is not optional. Once the sweep lands, the entry goes back
# to identity. A table left permanently translating an old spelling would let
# the suite go on exercising a vocabulary the CLI has stopped accepting -- a
# test lying about the product, and precisely what the deprecation aliases
# were deleted to prevent.


#: Test vocabulary -> the command path the app actually registers.
#:
#: Longest matching prefix wins, so a group rename is one entry and a command
#: that *moves between groups* is a second, more specific entry that overrides
#: it. The right-hand side is a full argv prefix, not a single token, so a
#: command that gains a flag in its new home (`plan -o github`) can be
#: expressed here rather than at N call sites.
#:
#: **Every entry is identity, and that is the steady state.** The CLI accepts
#: exactly one spelling per command -- there are no deprecation aliases -- so
#: a test written in an older vocabulary would be a test asserting against a
#: product that no longer exists. When the next rename lands, point the
#: affected entry at the new path, let the suite stay green in one commit,
#: then migrate the call sites and restore the entry to identity. The table is
#: a *migration* seam, not a permanent translation layer.
#:
#: `tests/test_cli_argv_table.py` guards it both ways: every right-hand side
#: must resolve against the live app, and every command the app registers must
#: appear here, so a new group cannot quietly bypass the seam.
_COMMAND_PATHS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("chart",): ("chart",),
    ("doctor",): ("doctor",),
    ("event",): ("event",),
    ("grafana",): ("grafana",),
    ("helmrelease",): ("helmrelease",),
    ("local",): ("local",),
    ("plan",): ("plan",),
    ("schemas",): ("schemas",),
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
    those commands flat, with no group above them. Mid-migration, when an
    entry is non-identity, translating a root path into such an app would
    rewrite e.g. `publish` to `chart publish` against an app where only
    `publish` exists. Those modules are already insulated --
    `register()` owns the command name, `main.py` owns the group name -- so
    they keep a plain `CliRunner` and need nothing from this table.
    """
    # Historical tests addressed synthetic repositories with the removed
    # `--root` option. Translate that test-only spelling onto the supported
    # operator override so those tests continue to exercise command behavior,
    # while dedicated surface tests assert that real argv rejects `--root`.
    tokens: list[str] = []
    root_override: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--root" and index + 1 < len(argv):
            root_override = argv[index + 1]
            index += 2
            continue
        if token.startswith("--root="):
            root_override = token.partition("=")[2]
            index += 1
            continue
        tokens.append(token)
        index += 1
    env = {"CHART_MANAGER_ROOT": root_override} if root_override is not None else None
    try:
        return CliRunner().invoke(
            _root_app(),
            resolve_argv(tokens),
            input=input,
            catch_exceptions=catch_exceptions,
            env=env,
        )
    finally:
        reset_invocation()


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
