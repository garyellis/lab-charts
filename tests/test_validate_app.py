"""Unit tests for `services/validate/app.ValidateApp`.

No Typer anywhere in this module — that is the point of the type. Every
test drives the service directly with an injected runner factory, so the
whole validate pipeline (worklist -> filters -> row assembly -> helm
binding -> aggregation -> retention) is exercised without helm,
kubeconform, kyverno, or a terminal.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from chart_manager.plumbing.errors import ChartManagerError, ChartNotFoundError
from chart_manager.services.validate.app import (
    ALL_PHASES,
    RunnerSpec,
    RunRequest,
    SingleRequest,
    ValidateApp,
    ValidateInputError,
    default_namespace,
    default_workers,
    resolve_workers,
)
from chart_manager.services.validate.domain.models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.services.validate.runner import RowConfig

# --- fixtures ---------------------------------------------------------------

_SPEC = """
version: 1
release_name: {name}
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml, values-prod.yaml]
triggers:
  "values.yaml": [dev, prod]
"""


def _chart(root: Path, name: str, *, spec: str | None = _SPEC, extra: str = "") -> Path:
    """Synthesize <root>/charts/<name> with a Chart.yaml (+ optional spec)."""
    chart_dir = root / "charts" / name
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n")
    # Bare `--chart <name>` resolves through ChartRepository, which insists
    # on a test-spec.yaml even though validate never reads it.
    (chart_dir / "test-spec.yaml").write_text("version: 1\nprofiles:\n  minimal: {}\n")
    (chart_dir / "values.yaml").write_text("replicas: 1\n")
    (chart_dir / "values-prod.yaml").write_text("replicas: 3\n")
    if spec is not None:
        body = textwrap.dedent(spec.format(name=name))
        if extra:
            body += textwrap.dedent(extra)
        (chart_dir / "validate-spec.yaml").write_text(body)
    return chart_dir


class FakeRunner:
    """Stands in for ValidateRunner; records the configs it was handed."""

    def __init__(self, spec: RunnerSpec, log: list[tuple[RunnerSpec, list[RowConfig]]]):
        self.spec = spec
        self._log = log

    def run(
        self,
        configs: list[RowConfig],
        *,
        enabled_phases: frozenset[str] | None = None,
    ) -> RunResult:
        """Return a PASS row per config (or a FAIL when the chart says so)."""
        self._log.append((self.spec, list(configs)))
        rows = tuple(
            RowResult(
                row=cfg.row,
                phases={
                    "render": PhaseResult(
                        phase="render",
                        status="FAIL" if cfg.row.chart.startswith("bad") else "PASS",
                    ),
                },
            )
            for cfg in configs
        )
        return RunResult(rows=rows, rendered_root=self.spec.output_root)


class Recorder:
    """Captures runner specs + configs and the warnings the app emitted."""

    def __init__(self) -> None:
        self.runs: list[tuple[RunnerSpec, list[RowConfig]]] = []
        self.warnings: list[str] = []

    def factory(self, spec: RunnerSpec):
        """Runner factory injected into ValidateApp."""
        return FakeRunner(spec, self.runs)

    @property
    def configs(self) -> list[RowConfig]:
        """Every RowConfig across every sub-run, in submission order."""
        return [cfg for _spec, cfgs in self.runs for cfg in cfgs]


class FakeGit:
    """Git stub: returns a canned changed-files list or raises."""

    def __init__(self, files: list[str] | None = None, error: str | None = None) -> None:
        self.files = files or []
        self.error = error
        self.calls: list[str] = []

    def changed_files(self, *, base: str) -> list[str]:
        """Record the base ref, then answer or raise."""
        self.calls.append(base)
        if self.error is not None:
            raise ChartManagerError(self.error)
        return self.files


def _app(rec: Recorder, *, git: FakeGit | None = None, **kwargs) -> ValidateApp:
    """Build a ValidateApp wired to the recorder (and optionally a fake git)."""
    return ValidateApp(
        runner_factory=rec.factory,
        on_warn=rec.warnings.append,
        git_factory=(lambda _root: git) if git is not None else None,
        run_id_factory=lambda: "RUNID",
        **kwargs,
    )


# --- resource policy --------------------------------------------------------


def test_default_workers_floors_at_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert default_workers() == 2


def test_default_workers_ceilings_at_eight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    assert default_workers() == 8


def test_resolve_workers_zero_is_auto_and_negative_is_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    assert resolve_workers(0) == 4
    assert resolve_workers(3) == 3
    assert resolve_workers(-1) == 1


def test_run_passes_resolved_workers_to_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, all_charts=True))

    assert rec.runs[0][0].max_workers == 4


def test_verbose_forces_serial_and_streams_helm(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, all_charts=True, workers=8, verbose=True))

    spec = rec.runs[0][0]
    assert spec.max_workers == 1
    assert spec.verbose is True


def test_zero_timeouts_become_unbounded_at_the_runner_boundary(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).run(
        RunRequest(root=tmp_path, all_charts=True, row_timeout=0.0, dep_update_timeout=0.0)
    )

    spec = rec.runs[0][0]
    assert spec.row_timeout is None
    assert spec.dep_update_timeout is None


# --- changed-files precedence ----------------------------------------------


def test_all_charts_never_consults_git(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    git = FakeGit(files=["charts/alpha/values.yaml"])
    rec = Recorder()

    outcome = _app(rec, git=git).run(RunRequest(root=tmp_path, all_charts=True))

    assert git.calls == []
    assert {r.row.env for r in outcome.result.rows} == {"dev", "prod"}


def test_explicit_changed_files_wins_over_git(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    listing = tmp_path / "changed.txt"
    listing.write_text("charts/alpha/values-prod.yaml\n\n")
    git = FakeGit(files=["charts/alpha/values.yaml"])
    rec = Recorder()

    outcome = _app(rec, git=git).run(
        RunRequest(root=tmp_path, changed_files=listing)
    )

    assert git.calls == []
    # values-prod.yaml matches no trigger in the fixture spec => no rows.
    assert outcome.result.rows == ()


def test_git_diff_supplies_changed_files_by_default(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    git = FakeGit(files=["charts/alpha/values.yaml"])
    rec = Recorder()

    outcome = _app(rec, git=git).run(RunRequest(root=tmp_path, base="origin/trunk"))

    assert git.calls == ["origin/trunk"]
    assert {r.row.env for r in outcome.result.rows} == {"dev", "prod"}


def test_failed_git_diff_warns_and_validates_everything(tmp_path: Path) -> None:
    """The silent downgrade is a policy: a broken diff must widen, not fail."""
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "beta")
    git = FakeGit(error="bad revision")
    rec = Recorder()

    outcome = _app(rec, git=git).run(RunRequest(root=tmp_path))

    assert len(outcome.result.rows) == 4  # 2 charts x 2 envs
    assert any("git diff failed" in w for w in rec.warnings)


def test_unreadable_changed_files_raises_input_error(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    with pytest.raises(ValidateInputError) as exc:
        _app(rec).run(RunRequest(root=tmp_path, changed_files=tmp_path / "nope.txt"))

    assert exc.value.hint == "changed_files"


# --- filters ----------------------------------------------------------------


def test_chart_and_env_filters_narrow_the_worklist(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "beta")
    rec = Recorder()

    outcome = _app(rec).run(
        RunRequest(root=tmp_path, all_charts=True, charts=("alpha",), envs=("dev",))
    )

    assert [(r.row.chart, r.row.env) for r in outcome.result.rows] == [("alpha", "dev")]
    # The worklist now knows what the filters removed.
    assert outcome.rows_filtered_out == 3


# --- row assembly -----------------------------------------------------------


def test_row_config_resolves_values_policies_and_spec_settings(tmp_path: Path) -> None:
    extra = """
kubernetes_version: "1.31.2"
schema_locations: ["default"]
policies:
  extra: [extra-policies]
"""
    _chart(tmp_path, "alpha", extra=extra)
    (tmp_path / "policies").mkdir()
    (tmp_path / "charts" / "alpha" / "policies").mkdir()
    (tmp_path / "extra-policies").mkdir()
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, all_charts=True, envs=("prod",)))

    cfg = rec.configs[0]
    assert cfg.chart_path == (tmp_path / "charts" / "alpha").resolve()
    assert cfg.values == [
        (tmp_path / "charts" / "alpha" / "values.yaml").resolve(),
        (tmp_path / "charts" / "alpha" / "values-prod.yaml").resolve(),
    ]
    assert cfg.kubernetes_version == "1.31.2"
    assert cfg.schema_locations == ["default"]
    assert cfg.policy_paths == [
        tmp_path / "policies",
        tmp_path / "charts" / "alpha" / "policies",
        (tmp_path / "extra-policies").resolve(),
    ]


def test_spec_policy_extra_that_does_not_exist_is_skipped(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", extra="policies:\n  extra: [missing-dir]\n")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, all_charts=True, envs=("dev",)))

    assert rec.configs[0].policy_paths == []


# --- helm bindings ----------------------------------------------------------


def test_rows_group_by_helm_binding_and_results_are_resorted(tmp_path: Path) -> None:
    """One runner per distinct binding; the union stays globally ordered."""
    _chart(tmp_path, "zulu", extra='helm_version: "3.20.0"\n')
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True, envs=("dev",)))

    assert len(rec.runs) == 2
    assert {spec.helm_version for spec, _ in rec.runs} == {None, "3.20.0"}
    # zulu ran in its own (later) sub-run, but sorts first in the union.
    assert [r.row.chart for r in outcome.result.rows] == ["alpha", "zulu"]


def test_all_rows_share_one_runner_when_bindings_match(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "beta")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, all_charts=True))

    assert len(rec.runs) == 1
    assert len(rec.runs[0][1]) == 4


# --- result assembly --------------------------------------------------------


def test_run_builds_the_run_result_itself(tmp_path: Path) -> None:
    """The service — not the surface — owns RunResult construction."""
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "broken", spec="version: 99\nrelease_name: broken\n")
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True))

    assert isinstance(outcome.result, RunResult)
    assert outcome.result.rendered_root == outcome.out_dir
    assert outcome.result.spec_errors  # surfaced from the worklist build
    assert outcome.exit_code == 3
    assert outcome.ok is False


def test_missing_spec_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "nospec", spec=None)
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True))

    assert outcome.charts_unvalidated == 1
    assert any("nospec" in w for w in outcome.warnings)
    assert outcome.ok is True


def test_empty_worklist_still_yields_a_result(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True, charts=("ghost",)))

    assert outcome.result.rows == ()
    assert outcome.exit_code == 0
    assert rec.runs == []


def test_enabled_phases_reach_the_runner_and_the_outcome(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).run(
        RunRequest(root=tmp_path, all_charts=True, phases=frozenset({"render"}))
    )

    assert outcome.enabled_phases == frozenset({"render"})


def test_unknown_phase_is_rejected() -> None:
    with pytest.raises(ValidateInputError) as exc:
        RunRequest(phases=frozenset({"render", "lint"}))
    assert exc.value.hint == "phases"


def test_empty_phase_set_is_rejected() -> None:
    with pytest.raises(ValidateInputError):
        RunRequest(phases=frozenset())


# --- run identity + retention ----------------------------------------------


def test_default_out_dir_is_a_minted_run_id_under_the_repo(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True))

    assert outcome.out_dir == (tmp_path / ".chart-manager" / "rendered" / "RUNID").resolve()
    assert outcome.keep is False


def test_explicit_out_dir_is_an_implicit_keep(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()
    target = tmp_path / "named"

    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True, out=target))

    assert outcome.out_dir == target.resolve()
    assert outcome.keep is True


def _outcome_for_cleanup(tmp_path: Path, rec: Recorder, **kwargs):
    """Run a passing worklist so cleanup has a real out dir to consider."""
    _chart(tmp_path, "alpha")
    outcome = _app(rec).run(RunRequest(root=tmp_path, all_charts=True, **kwargs))
    outcome.out_dir.mkdir(parents=True, exist_ok=True)
    return outcome


def test_cleanup_removes_the_render_dir_on_success(tmp_path: Path) -> None:
    rec = Recorder()
    app = _app(rec)
    outcome = _outcome_for_cleanup(tmp_path, rec)

    app.cleanup(outcome)

    assert not outcome.out_dir.exists()


def test_cleanup_keeps_the_render_dir_when_asked(tmp_path: Path) -> None:
    rec = Recorder()
    app = _app(rec)
    outcome = _outcome_for_cleanup(tmp_path, rec, keep=True)

    app.cleanup(outcome)

    assert outcome.out_dir.exists()


def test_cleanup_keeps_the_render_dir_on_failure(tmp_path: Path) -> None:
    """Artifacts are the evidence for a failed run."""
    rec = Recorder()
    app = _app(rec)
    _chart(tmp_path, "bad-app")
    outcome = app.run(RunRequest(root=tmp_path, all_charts=True))
    outcome.out_dir.mkdir(parents=True, exist_ok=True)

    assert outcome.exit_code == 1
    app.cleanup(outcome)

    assert outcome.out_dir.exists()


def test_cleanup_keeps_the_render_dir_when_debug_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEBUG", "TRUE")
    rec = Recorder()
    app = _app(rec)
    outcome = _outcome_for_cleanup(tmp_path, rec)

    app.cleanup(outcome)

    assert outcome.out_dir.exists()


def test_cleanup_warns_instead_of_raising_when_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder()
    app = _app(rec)
    outcome = _outcome_for_cleanup(tmp_path, rec)

    def boom(_path):
        raise OSError("device busy")

    monkeypatch.setattr("shutil.rmtree", boom)
    app.cleanup(outcome)  # must not raise

    assert any("cleanup failed" in w for w in rec.warnings)


# --- progress ---------------------------------------------------------------


class SpySink:
    """Progress sink that records lifecycle calls."""

    def __init__(self) -> None:
        self.started: list[list[WorklistRow]] = []
        self.stops = 0

    def start(self, rows) -> None:
        self.started.append(list(rows))

    def on_event(self, row, phase, status, elapsed_s=None) -> None:
        return

    def stop(self) -> None:
        self.stops += 1


def test_progress_sink_is_started_with_every_row_and_always_stopped(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()
    sink = SpySink()

    ValidateApp(
        runner_factory=rec.factory, progress=sink, run_id_factory=lambda: "RUNID"
    ).run(RunRequest(root=tmp_path, all_charts=True))

    assert len(sink.started) == 1
    assert len(sink.started[0]) == 2
    assert sink.stops == 1


def test_progress_sink_stops_even_when_a_sub_run_explodes(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    sink = SpySink()

    def exploding_factory(_spec):
        raise RuntimeError("boom")

    app = ValidateApp(
        runner_factory=exploding_factory, progress=sink, run_id_factory=lambda: "RUNID"
    )
    with pytest.raises(RuntimeError):
        app.run(RunRequest(root=tmp_path, all_charts=True))

    assert sink.stops == 1


# --- single row -------------------------------------------------------------


def test_single_defaults_namespace_release_and_values(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).single(SingleRequest(chart="alpha", env="dev", root=tmp_path))

    cfg = rec.configs[0]
    assert cfg.row.namespace == "lab-dev" == default_namespace("dev")
    assert cfg.row.release == "alpha"
    assert cfg.values == [(tmp_path / "charts" / "alpha" / "values.yaml").resolve()]
    assert outcome.result.rendered_root == outcome.out_dir


def test_single_honors_explicit_namespace_release_and_values(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).single(
        SingleRequest(
            chart="alpha",
            env="dev",
            root=tmp_path,
            namespace="custom",
            release="rel",
            values=(Path("values-prod.yaml"),),
        )
    )

    cfg = rec.configs[0]
    assert cfg.row.namespace == "custom"
    assert cfg.row.release == "rel"
    assert cfg.values == [(tmp_path / "charts" / "alpha" / "values-prod.yaml").resolve()]


def test_single_without_policies_passes_none_so_the_phase_skips(tmp_path: Path) -> None:
    """render/schema must not silently start enforcing policy."""
    _chart(tmp_path, "alpha")
    (tmp_path / "policies").mkdir()
    rec = Recorder()

    _app(rec).single(SingleRequest(chart="alpha", env="dev", root=tmp_path))

    assert rec.configs[0].policy_paths is None


def test_single_discovers_policies_when_asked(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    (tmp_path / "policies").mkdir()
    rec = Recorder()

    _app(rec).single(
        SingleRequest(chart="alpha", env="dev", root=tmp_path, discover_policies=True)
    )

    assert rec.configs[0].policy_paths == [tmp_path / "policies"]


def test_single_explicit_policy_dirs_beat_discovery(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    (tmp_path / "policies").mkdir()
    (tmp_path / "mine").mkdir()
    rec = Recorder()

    _app(rec).single(
        SingleRequest(
            chart="alpha",
            env="dev",
            root=tmp_path,
            policy_dirs=(Path("mine"),),
            discover_policies=True,
        )
    )

    assert rec.configs[0].policy_paths == [(tmp_path / "mine").resolve()]


def test_single_accepts_a_chart_path_outside_the_charts_dir(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "outside"
    fixture.mkdir(parents=True)
    (fixture / "Chart.yaml").write_text("apiVersion: v2\nname: outside\nversion: 0.1.0\n")
    rec = Recorder()

    _app(rec).single(
        SingleRequest(chart="fixtures/outside", env="dev", root=tmp_path)
    )

    cfg = rec.configs[0]
    assert cfg.chart_path == fixture.resolve()
    assert cfg.row.chart == "outside"
    assert cfg.values == []  # no values.yaml in the fixture chart


def test_single_raises_a_domain_error_for_an_unknown_chart(tmp_path: Path) -> None:
    """Resolution failures must not be coupled to Typer's exception type."""
    (tmp_path / "charts").mkdir()
    rec = Recorder()

    with pytest.raises(ChartNotFoundError):
        _app(rec).single(SingleRequest(chart="ghost", env="dev", root=tmp_path))


def test_single_raises_a_domain_error_for_a_path_without_chart_yaml(
    tmp_path: Path,
) -> None:
    (tmp_path / "not-a-chart").mkdir()
    rec = Recorder()

    with pytest.raises(ChartNotFoundError):
        _app(rec).single(SingleRequest(chart="not-a-chart/", env="dev", root=tmp_path))


def test_single_rejects_both_helm_bindings() -> None:
    with pytest.raises(ValidateInputError) as exc:
        SingleRequest(chart="a", env="dev", helm_version="3.20.0", helm_bin=Path("/x"))
    assert exc.value.hint == "helm_version"


def test_single_threads_the_helm_binding_and_schema_inputs_through(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).single(
        SingleRequest(
            chart="alpha",
            env="dev",
            root=tmp_path,
            helm_version="3.20.0",
            kubernetes_version="1.31.2",
            schema_locations=("default", "crds"),
        )
    )

    spec, cfgs = rec.runs[0]
    assert spec.helm_version == "3.20.0"
    assert spec.verbose is True  # single-row commands stream helm output
    assert cfgs[0].kubernetes_version == "1.31.2"
    assert cfgs[0].schema_locations == ["default", "crds"]


def test_single_defaults_to_every_phase(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).single(SingleRequest(chart="alpha", env="dev", root=tmp_path))

    assert outcome.enabled_phases == ALL_PHASES


def test_single_explicit_out_dir_is_an_implicit_keep(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()
    target = tmp_path / "named"

    outcome = _app(rec).single(
        SingleRequest(chart="alpha", env="dev", root=tmp_path, out=target)
    )

    assert outcome.out_dir == target.resolve()
    assert outcome.keep is True
