"""Unit tests for ``ManifestValidationService``.

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

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.manifest_validation.app import (
    ManifestValidationService,
    RunnerSpec,
    RunRequest,
    ValidateInputError,
    default_workers,
    resolve_workers,
)
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunResult,
    WorklistRow,
)
from chart_manager.services.manifest_validation.runner import RowConfig
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KyvernoConfig,
)

# --- fixtures ---------------------------------------------------------------

_SPEC = """
releaseName: {name}
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
    (chart_dir / "values.yaml").write_text("replicas: 1\n")
    (chart_dir / "values-prod.yaml").write_text("replicas: 3\n")
    if spec is not None:
        body = textwrap.dedent(spec.format(name=name))
        if extra:
            body += textwrap.dedent(extra)
        envelope = (
            "apiVersion: lifecycle.chartmanager.io/v1alpha1\n"
            "kind: ChartLifecycle\n"
            "metadata:\n"
            f"  name: {name}\n"
            "spec:\n"
            "  validation:\n"
            + textwrap.indent(body, "    ")
        )
        (chart_dir / "chart-lifecycle.yaml").write_text(envelope)
    return chart_dir


class FakeRunner:
    """Stands in for ManifestValidationRunner; records the configs it was handed."""

    def __init__(self, spec: RunnerSpec, log: list[tuple[RunnerSpec, list[RowConfig]]]):
        self.spec = spec
        self._log = log

    def run(
        self,
        configs: list[RowConfig],
        *,
        enabled_phases: frozenset[str] | None = None,
        fail_fast: bool = False,
    ) -> RunResult:
        """Return a PASS row per config (or a FAIL when the chart says so)."""
        _ = fail_fast
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
        """Runner factory injected into ManifestValidationService."""
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


def _app(rec: Recorder, *, git: FakeGit | None = None, **kwargs) -> ManifestValidationService:
    """Build a ManifestValidationService wired to the recorder (and optionally a fake git)."""
    return ManifestValidationService(
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

    _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert rec.runs[0][0].max_workers == 4


def test_verbose_forces_serial_and_streams_helm(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, workers=8, verbose=True))

    spec = rec.runs[0][0]
    assert spec.max_workers == 1
    assert spec.verbose is True


def test_zero_timeouts_become_unbounded_at_the_runner_boundary(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    _app(rec).run(
        RunRequest(root=tmp_path, skip_change_detection=True, tool_timeout=0.0, dep_update_timeout=0.0)
    )

    spec = rec.runs[0][0]
    assert spec.tool_timeout is None
    assert spec.dep_update_timeout is None


# --- changed-files precedence ----------------------------------------------


def test_skip_change_detection_never_consults_git(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    git = FakeGit(files=["charts/alpha/values.yaml"])
    rec = Recorder()

    outcome = _app(rec, git=git).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert git.calls == []
    assert {r.row.env for r in outcome.result.rows} == {"dev", "prod"}


def test_explicit_changed_files_wins_over_git(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    listing = tmp_path / "changed.txt"
    listing.write_text("charts/alpha/values-prod.yaml\n\n")
    git = FakeGit(files=["charts/alpha/values.yaml"])
    rec = Recorder()

    outcome = _app(rec, git=git).run(RunRequest(root=tmp_path, changed_files=listing))

    assert git.calls == []
    # values-prod.yaml matches no trigger in the fixture spec => no rows.
    assert outcome.result.rows == ()
    assert outcome.unmatched_changes == (Path("charts/alpha/values-prod.yaml"),)
    assert outcome.ignored_changes == ()
    assert any("matches no trigger" in warning for warning in outcome.warnings)


def test_explicit_chart_selection_validates_all_environments_without_git(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "beta")
    git = FakeGit(files=[])
    rec = Recorder()

    outcome = _app(rec, git=git).run(RunRequest(root=tmp_path, charts=("alpha",)))

    assert git.calls == []
    assert {(row.row.chart, row.row.env) for row in outcome.result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }


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
        RunRequest(root=tmp_path, skip_change_detection=True, charts=("alpha",), envs=("dev",))
    )

    assert [(r.row.chart, r.row.env) for r in outcome.result.rows] == [("alpha", "dev")]
    # Targeted planning never loads beta; only alpha/prod is filtered out.
    assert outcome.rows_filtered_out == 1


# --- row assembly -----------------------------------------------------------


def test_row_config_resolves_values_policies_and_spec_settings(tmp_path: Path) -> None:
    extra = """
kubernetesVersion: "1.31.2"
schemaLocations: ["default"]
policies:
  extra: [extra-policies]
"""
    _chart(tmp_path, "alpha", extra=extra)
    (tmp_path / "policies").mkdir()
    (tmp_path / "charts" / "alpha" / "policies").mkdir()
    (tmp_path / "charts" / "alpha" / "extra-policies").mkdir()
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, envs=("prod",)))

    cfg = rec.configs[0]
    assert cfg.chart_path == (tmp_path / "charts" / "alpha").resolve()
    assert cfg.values == [
        (tmp_path / "charts" / "alpha" / "values.yaml").resolve(),
        (tmp_path / "charts" / "alpha" / "values-prod.yaml").resolve(),
    ]
    kubeconform = cfg.validator_invocations[0].config
    kyverno = cfg.validator_invocations[1].config
    assert isinstance(kubeconform, KubeconformConfig)
    assert isinstance(kyverno, KyvernoConfig)
    assert kubeconform.kubernetes_version == "1.31.2"
    assert kubeconform.schema_locations == ("default",)
    assert kyverno.policy_paths == (
        tmp_path / "policies",
        tmp_path / "charts" / "alpha" / "policies",
        (tmp_path / "charts" / "alpha" / "extra-policies").resolve(),
    )


def test_spec_policy_extra_that_does_not_exist_is_skipped(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", extra="policies:\n  extra: [missing-dir]\n")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, envs=("dev",)))

    config = rec.configs[0].validator_invocations[1].config
    assert isinstance(config, KyvernoConfig)
    assert config.policy_paths == ()


# --- helm bindings ----------------------------------------------------------


def test_mixed_helm_bindings_reach_one_runner_carrying_their_binding(
    tmp_path: Path,
) -> None:
    """No grouping: every row goes to one runner, tagged with its own binding.

    Partitioning by binding here is what used to force a second copy of
    fan-out, fail-fast, NOT_RUN synthesis and ordering on top of the runner --
    and made a pinned chart wait for every unpinned one to finish.
    """
    _chart(tmp_path, "zulu", extra='helmVersion: "3.20.0"\n')
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, envs=("dev",)))

    assert len(rec.runs) == 1
    assert {cfg.row.chart: cfg.helm_binding for cfg in rec.runs[0][1]} == {
        "alpha": (None, None),
        "zulu": ("3.20.0", None),
    }
    assert [r.row.chart for r in outcome.result.rows] == ["alpha", "zulu"]


def test_all_rows_share_one_runner_when_bindings_match(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "beta")
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert len(rec.runs) == 1
    assert len(rec.runs[0][1]) == 4


def test_fail_fast_is_delegated_to_the_single_runner(tmp_path: Path) -> None:
    """The service no longer stops anything itself; the runner owns the flag."""
    _chart(tmp_path, "bad-alpha")
    _chart(tmp_path, "zulu", extra='helmVersion: "3.20.0"\n')
    seen: list[bool] = []

    class _FailFastRecordingRunner(FakeRunner):
        def run(self, configs, *, enabled_phases=None, fail_fast=False):
            seen.append(fail_fast)
            return super().run(configs, enabled_phases=enabled_phases, fail_fast=fail_fast)

    rec = Recorder()
    outcome = ManifestValidationService(
        runner_factory=lambda spec: _FailFastRecordingRunner(spec, rec.runs),
        run_id_factory=lambda: "RUNID",
    ).run(
        RunRequest(
            root=tmp_path,
            skip_change_detection=True,
            envs=("dev",),
            fail_fast=True,
        )
    )

    assert seen == [True]
    assert {cfg.row.chart for cfg in rec.runs[0][1]} == {"bad-alpha", "zulu"}
    assert outcome.result.rows[0].row.chart == "bad-alpha"


# --- result assembly --------------------------------------------------------


def test_run_builds_the_run_result_itself(tmp_path: Path) -> None:
    """The service — not the surface — owns RunResult construction."""
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "broken", spec="releaseName: broken\nmystery: true\n")
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert isinstance(outcome.result, RunResult)
    assert outcome.result.rendered_root == outcome.out_dir
    assert outcome.result.spec_errors  # surfaced from the worklist build
    assert outcome.outcome is Outcome.SPEC
    assert outcome.ok is False


def test_missing_spec_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "nospec", spec=None)
    rec = Recorder()

    outcome = _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert outcome.charts_unvalidated == 1
    assert any("nospec" in w for w in outcome.warnings)
    assert outcome.ok is True


def test_unknown_explicit_chart_filter_is_an_input_error(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    with pytest.raises(ValidateInputError) as exc:
        _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, charts=("ghost",)))

    assert exc.value.hint == "charts"
    assert rec.runs == []


def test_explicit_chart_without_config_fails_precisely(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=None)

    with pytest.raises(
        ValidateInputError,
        match=r"has no validation configuration in chart-lifecycle\.yaml",
    ):
        _app(Recorder()).run(
            RunRequest(root=tmp_path, skip_change_detection=True, charts=("alpha",))
        )


def test_explicit_chart_with_disabled_capability_fails_precisely(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", extra="enabled: false\n")

    with pytest.raises(
        ValidateInputError,
        match="manifest validation is disabled for chart 'alpha'",
    ):
        _app(Recorder()).run(
            RunRequest(root=tmp_path, skip_change_detection=True, charts=("alpha",))
        )


def test_explicit_chart_with_malformed_config_returns_a_spec_outcome(tmp_path: Path) -> None:
    _chart(tmp_path, "broken", spec="releaseName: broken\nmystery: true\n")

    outcome = _app(Recorder()).run(
        RunRequest(root=tmp_path, skip_change_detection=True, charts=("broken",))
    )

    assert outcome.result.spec_errors
    assert outcome.outcome is Outcome.SPEC


def test_explicit_chart_is_isolated_from_unrelated_repository_errors(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha")
    _chart(tmp_path, "broken", spec="releaseName: broken\nmystery: true\n")
    _chart(tmp_path, "unconfigured", spec=None)
    rec = Recorder()

    outcome = _app(rec).run(
        RunRequest(root=tmp_path, skip_change_detection=True, charts=("alpha",))
    )

    assert {(row.row.chart, row.row.env) for row in outcome.result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }
    assert outcome.result.spec_errors == ()
    assert outcome.warnings == ()
    assert outcome.charts_unvalidated == 0
    assert outcome.outcome is Outcome.SUCCESS


def test_enabled_phases_reach_the_runner_and_the_outcome(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()

    outcome = _app(rec).run(
        RunRequest(root=tmp_path, skip_change_detection=True, phases=frozenset({"render"}))
    )

    assert outcome.enabled_phases == frozenset({"render"})


def test_mixed_charts_keep_validator_selection_on_each_row(tmp_path: Path) -> None:
    _chart(
        tmp_path,
        "alpha",
        extra="validators:\n  kubeconform: false\n  policy: true\n",
    )
    _chart(
        tmp_path,
        "beta",
        extra="validators:\n  kubeconform: true\n  policy: false\n",
    )
    rec = Recorder()

    _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True))

    enabled_by_chart = {
        config.row.chart: frozenset(
            invocation.category.value
            for invocation in config.validator_invocations
            if invocation.enabled
        )
        for config in rec.configs
    }
    assert enabled_by_chart == {
        "alpha": frozenset({"policy"}),
        "beta": frozenset({"schema"}),
    }


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

    outcome = _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert outcome.out_dir == (tmp_path / ".chart-manager" / "rendered" / "RUNID").resolve()
    assert outcome.keep is False


def test_explicit_out_dir_is_an_implicit_keep(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()
    target = tmp_path / "named"

    outcome = _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, out=target))

    assert outcome.out_dir == target.resolve()
    assert outcome.keep is True


def _outcome_for_cleanup(tmp_path: Path, rec: Recorder, **kwargs):
    """Run a passing worklist so cleanup has a real out dir to consider."""
    _chart(tmp_path, "alpha")
    outcome = _app(rec).run(RunRequest(root=tmp_path, skip_change_detection=True, **kwargs))
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
    outcome = app.run(RunRequest(root=tmp_path, skip_change_detection=True))
    outcome.out_dir.mkdir(parents=True, exist_ok=True)

    assert outcome.outcome is Outcome.FAILED
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
        self.events: list[tuple[str, str, str]] = []
        self.stops = 0

    def start(self, rows) -> None:
        self.started.append(list(rows))

    def on_event(self, row, phase, status, elapsed_s=None) -> None:
        self.events.append((row.chart, phase, status))

    def stop(self) -> None:
        self.stops += 1


def test_progress_sink_is_started_with_every_row_and_always_stopped(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha")
    rec = Recorder()
    sink = SpySink()

    ManifestValidationService(
        runner_factory=rec.factory, progress=sink, run_id_factory=lambda: "RUNID"
    ).run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert len(sink.started) == 1
    assert len(sink.started[0]) == 2
    assert sink.stops == 1


def test_runner_construction_failure_becomes_outcomes_and_progress(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha")
    sink = SpySink()

    def exploding_factory(_spec):
        raise RuntimeError("boom")

    app = ManifestValidationService(
        runner_factory=exploding_factory, progress=sink, run_id_factory=lambda: "RUNID"
    )
    outcome = app.run(RunRequest(root=tmp_path, skip_change_detection=True))

    assert sink.stops == 1
    assert len(outcome.result.rows) == 2
    assert outcome.outcome is Outcome.TOOL
    assert {(event[0], event[1], event[2]) for event in sink.events} == {
        ("alpha", "render", "FAIL"),
        ("alpha", "schema", "SKIP"),
        ("alpha", "policy", "SKIP"),
    }
    assert len(sink.events) == 6


def test_runner_construction_failure_preserves_disabled_validator_semantics(
    tmp_path: Path,
) -> None:
    _chart(
        tmp_path,
        "alpha",
        extra="validators:\n  kubeconform: false\n  policy: false\n",
    )

    def exploding_factory(_spec):
        raise RuntimeError("boom")

    outcome = ManifestValidationService(
        runner_factory=exploding_factory,
        run_id_factory=lambda: "RUNID",
    ).run(RunRequest(root=tmp_path, skip_change_detection=True))

    phases = outcome.result.rows[0].phases
    assert phases["render"].status == "FAIL"
    assert phases["schema"].status == "SKIP"
    assert phases["schema"].skip_cause == "validator_disabled"
    assert phases["policy"].status == "SKIP"
    assert phases["policy"].skip_cause == "validator_disabled"
    assert outcome.outcome is Outcome.TOOL
