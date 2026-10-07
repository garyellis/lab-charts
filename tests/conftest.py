"""Shared pytest fixtures and the one `CommandRunner` fake.

`chart_root` + `make_chart` build a synthetic `<root>/charts/` tree under
tmp_path. Unit tests that assert against the repo's own `charts/` directory
break every time a chart is added -- which makes a data change look exactly
like a code regression, and cost this suite three red tests. Build the tree
the test needs instead, and keep real-tree coverage to explicit smoke tests
that assert containment rather than an inventory.

`FakeCommandRunner` is the single record-and-replay subprocess seam. See its
docstring for why there is exactly one of it.

`cli()` is the single seam through which tests name a CLI command.
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from typer.testing import CliRunner, Result

from chart_manager.api.v1alpha1.chart_workspace import ChartWorkspaceSpec
from chart_manager.cli._container import reset_invocation
from chart_manager.plumbing.commands import (
    CommandResult,
    CommandRunner,
    SubprocessRunner,
    redact,
)
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.preflight import Check
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.shared.workspace import RepositoryWorkspace

#: Repo root, anchored to this file rather than the process cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The conventional layout, for loaders that take it explicitly.
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
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_TOKEN",
        "RENOVATE_TOKEN",
        "FORCE_COLOR",
        "CHART_MANAGER_OCI_REPOSITORY",
        "CHART_MANAGER_OCI_CA_FILE",
        "CHART_MANAGER_SCHEMA_CACHE_ROOT",
        # Never inherit a developer's event sink and persist records in Cosmos DB.
        "EVENTS_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def hermetic_logging() -> Iterator[None]:
    """Undo any process-wide logging configuration a test installs.

    `setup_logging` is deliberately process-wide (`logging.basicConfig(
    force=True)`), so a single CLI test that exercises `-v` for real leaves a
    `RichLogHandler` on the *root* logger, bound to the `CliRunner` stderr
    buffer that is closed the moment that test returns. Every later test that
    logs anything then writes into a closed stream, and `logging` swallows the
    result as "--- Logging error ---" on stderr.

    That was invisible while nothing below the CLI emitted log records. It is not
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
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`tmp_path` as the workspace root, set through the real `CHART_MANAGER_ROOT`."""
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(tmp_path))
    return tmp_path


#: Where the fake PATH lookup claims every binary lives.
FAKE_BIN = "/opt/fake/bin"

OnPath = Callable[..., None]


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> OnPath:
    """Control what `probe_binary` finds on PATH: only the names passed are present."""

    def install(*names: str) -> None:
        present = set(names)

        def which(binary: str, *args: Any, **kwargs: Any) -> str | None:
            if binary not in present:
                return None
            # An absolute name (a mise-resolved helm) is already a path.
            return binary if binary.startswith("/") else f"{FAKE_BIN}/{binary}"

        monkeypatch.setattr(shutil, "which", which)

    return install


def checks_by_name(checks: Sequence[Check]) -> dict[str, Check]:
    """Index a preflight result by check name."""
    return {check.name: check for check in checks}


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
    """Write a minimal Helm chart with enabled chart tests into ``chart_root``.

    `profiles` is the raw chart-test profile mapping, so tests express
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
                        "chartTest": {
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
    (chart / "Chart.yaml").write_text(
        dump_yaml({"apiVersion": "v2", "name": name, "version": "0.1.0"})
    )
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


#: A Grafana dashboard that satisfies every lint rule.
PASSING_DASHBOARD = """{
  "title": "T", "uid": "u", "schemaVersion": 38, "editable": true,
  "panels": [{"id": 1, "title": "p",
              "datasource": {"type":"prometheus","uid":"${DS_PROMETHEUS}"},
              "targets":[{"expr":"rate(x[$__rate_interval])"}]}],
  "templating": {"list":[{"type":"datasource","name":"DS_PROMETHEUS"}]}
}"""


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
    chart_yaml = (f"apiVersion: v2\nname: {name}\nversion: {version}\n").encode()
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
def schema_cache(tmp_path: Path) -> tuple[Any, Any]:
    """A locked schema generation and an empty store under `tmp_path / "schema-cache"`."""
    from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore
    from tests import schema_fixtures  # imports this module

    lock, _, snapshots = schema_fixtures.schema_store(tmp_path / "upstream")
    return lock, KubeconformSchemaStore(cache_root=tmp_path / "schema-cache", snapshots=snapshots)


@pytest.fixture
def schema_workspace(tmp_path: Path, schema_cache: tuple[Any, Any]) -> RepositoryWorkspace:
    """A workspace whose locked schema generation is synced into a tmp cache."""
    from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
    from chart_manager.shared.workspace import SCHEMA_LOCK_FILE
    from tests import schema_fixtures  # imports this module

    lock, store = schema_cache
    store.sync(lock)
    write_schema_lock_atomic(tmp_path / SCHEMA_LOCK_FILE, lock)
    return schema_fixtures.workspace(tmp_path)


def git_runner() -> FakeCommandRunner:
    """A fake that runs git for real, so the schema store can inspect a tmp cache."""
    return FakeCommandRunner().forward(("git",), SubprocessRunner())


# --- the CLI ---------------------------------------------------------------


def cli(*argv: str, input: str | None = None, catch_exceptions: bool = True) -> Result:
    """Invoke the real root app with `argv`, then reset the per-invocation container."""
    # Imported lazily so an import error in the CLI fails the CLI tests, not collection.
    from chart_manager.main import app

    try:
        return CliRunner().invoke(app, argv, input=input, catch_exceptions=catch_exceptions)
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
    """One scripted subprocess outcome; `raises` is raised instead of returning."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    raises: BaseException | None = None


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
        self._forwards: list[tuple[Predicate, CommandRunner]] = []

    # --- scripting ----------------------------------------------------------

    def respond(
        self,
        matcher: Matcher,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> FakeCommandRunner:
        """Answer every argv matching `matcher` with this reply, or raise `raises`. Chainable."""
        return self.respond_each(
            matcher, Reply(returncode=returncode, stdout=stdout, stderr=stderr, raises=raises)
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

    def forward(self, matcher: Matcher, runner: CommandRunner) -> FakeCommandRunner:
        """Run every argv matching `matcher` on `runner` instead of replying. Chainable."""
        self._forwards.append((_as_predicate(matcher), runner))
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
        for predicate, runner in self._forwards:
            if predicate(argv):
                return runner.run(
                    argv, cwd=cwd, check=check, capture=capture, timeout=timeout, env=env
                )
        reply = self._reply_for(argv)
        if reply.raises is not None:
            raise reply.raises
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


def plain_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Argv with the binary reduced to its name and a pinned kube context dropped."""
    head = (Path(argv[0]).name, *argv[1:])
    for flag in ("--kube-context", "--context"):
        if flag in head:
            at = head.index(flag)
            head = head[:at] + head[at + 2 :]
    return head


def argv_prefix(*prefix: str) -> Callable[[tuple[str, ...]], bool]:
    """A `FakeCommandRunner` matcher on the start of `plain_argv`."""
    return lambda argv: plain_argv(argv)[: len(prefix)] == prefix


class FakeCosmosContainer:
    """Record-and-replay `CosmosContainer`: records writes and queries, returns `documents`."""

    def __init__(self, documents: list[dict[str, Any]] | None = None) -> None:
        self.documents = documents or []
        self.items: list[dict[str, Any]] = []
        self.upserted: list[dict[str, Any]] = []
        self.queries: list[tuple[str, list[dict[str, Any]], str | None]] = []

    def write(self, item: dict[str, Any], *, upsert: bool) -> None:
        (self.upserted if upsert else self.items).append(item)

    def query(
        self, sql: str, parameters: list[dict[str, Any]], partition_key: str | None
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, parameters, partition_key))
        return list(self.documents)
