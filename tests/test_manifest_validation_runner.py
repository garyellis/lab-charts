"""Manifest-validation runner tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import (
    Kubeconform,
    KubeconformReport,
    ResourceResult,
)
from chart_manager.integrations.kyverno import Kyverno, KyvernoReport, PolicyResult
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.manifest_validation.models import WorklistRow
from chart_manager.services.manifest_validation.runner import (
    ManifestValidationRunner as _ManifestValidationRunner,
)
from chart_manager.services.manifest_validation.runner import (
    RowConfig,
)
from chart_manager.services.manifest_validation.validator_adapters import (
    KubeconformValidator,
    KyvernoValidator,
)
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KyvernoConfig,
    ValidatorCategory,
    ValidatorInvocation,
)


def ManifestValidationRunner(
    *,
    helm,
    output_root,
    kubeconform=None,
    kyverno=None,
    validators=None,
    **kwargs,
) -> _ManifestValidationRunner:
    """Keep test setup compact while production runner stays tool-neutral."""
    executors = validators or {
        "kubeconform": KubeconformValidator(kubeconform or Kubeconform()),
        "kyverno": KyvernoValidator(kyverno or Kyverno()),
    }
    return _ManifestValidationRunner(
        helm=helm,
        output_root=output_root,
        validators=executors,
        **kwargs,
    )


class _StubHelm(Helm):
    def __init__(self, *, succeed: bool, raise_exc: Exception | None = None) -> None:
        # Skip parent __init__: don't construct a CommandRunner or resolve a binary.
        self._succeed = succeed
        self._raise = raise_exc
        self.calls: list[dict] = []
        self.dep_update_calls: list[Path] = []

    def dependency_update(self, chart_path: Path, *, timeout: float | None = None) -> None:  # type: ignore[override]
        # Stub the runner's dep-prefetch pass: track calls, don't shell out.
        _ = timeout  # accepted for signature parity with the real Helm.
        self.dep_update_calls.append(chart_path.resolve())

    def template(  # type: ignore[override]
        self,
        release: str,
        chart_ref,
        *,
        namespace: str,
        output_dir: Path,
        values=None,
        sets=None,
        api_versions=None,
        kube_version=None,
        skip_tests: bool = True,
    ) -> Path:
        self.calls.append({"release": release, "output_dir": output_dir})
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if self._raise is not None:
            raise self._raise
        if not self._succeed:
            raise ExternalCommandError("helm fake failure")
        # Seed a manifest so downstream phases find something.
        (output_dir / "rendered.yaml").write_text("kind: Deployment\n")
        return output_dir


class _StubKubeconform(Kubeconform):
    def __init__(self, report: KubeconformReport) -> None:
        self._report = report
        self.calls: list[Path] = []

    def validate(  # type: ignore[override]
        self,
        manifests_dir: Path,
        *,
        kubernetes_version=None,
        schema_locations=None,
        skip_kinds=None,
        strict: bool = True,
        extra_args=None,
    ) -> KubeconformReport:
        self.calls.append(manifests_dir)
        return self._report


class _StubKyverno(Kyverno):
    def __init__(
        self,
        *,
        report: KyvernoReport | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        # Skip parent __init__ — no runner or binary needed.
        self._report = report
        self._raise = raise_exc
        self.calls: list[dict] = []

    def apply(  # type: ignore[override]
        self,
        manifests_dir: Path,
        *,
        policy_paths: list[Path],
        extra_args=None,
    ) -> KyvernoReport:
        self.calls.append({"manifests_dir": manifests_dir, "policy_paths": policy_paths})
        if self._raise is not None:
            raise self._raise
        assert self._report is not None
        return self._report


def _row(chart: str = "demo") -> WorklistRow:
    return WorklistRow(chart=chart, env="dev", release=chart, namespace="lab-dev")


def _cfg(
    row: WorklistRow,
    chart_path: Path,
    *,
    policy_paths: list[Path] | None = None,
    kubernetes_version: str | None = None,
    schema_locations: list[str] | None = None,
    enabled_validators: frozenset[str] = frozenset({"schema", "policy"}),
) -> RowConfig:
    return RowConfig(
        row=row,
        chart_path=chart_path,
        values=[],
        validator_invocations=(
            ValidatorInvocation(
                validator_id="kubeconform",
                category=ValidatorCategory.SCHEMA,
                order=100,
                lifecycle_action_kind="schema-validate",
                enabled="schema" in enabled_validators,
                config=KubeconformConfig(
                    kubernetes_version=kubernetes_version,
                    schema_locations=tuple(schema_locations or ()),
                ),
            ),
            ValidatorInvocation(
                validator_id="kyverno",
                category=ValidatorCategory.POLICY,
                order=200,
                lifecycle_action_kind="policy-validate",
                enabled="policy" in enabled_validators,
                config=KyvernoConfig(policy_paths=tuple(policy_paths or ())),
            ),
        ),
    )


def _ok_report() -> KubeconformReport:
    return KubeconformReport(
        resources=(),
        summary={"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
    )


def _kyverno_pass() -> KyvernoReport:
    return KyvernoReport(results=(), summary={"pass": 1, "fail": 0})


def test_render_pass_triggers_schema_phase(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    runner = ManifestValidationRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    row = _row()
    # No policy_paths -> policy SKIPs cleanly (no policies discovered).
    result = runner.run([_cfg(row, tmp_path / "chart")])

    assert len(kc.calls) == 1
    row_result = result.rows[0]
    assert row_result.phases["render"].status == "PASS"
    assert row_result.phases["schema"].status == "PASS"
    assert row_result.phases["policy"].status == "SKIP"
    assert row_result.phases["policy"].detail == "no policies discovered"
    assert result.outcome() is Outcome.SUCCESS


@pytest.mark.parametrize(
    ("chart", "env"),
    [
        ("", "dev"),
        ("   ", "dev"),
        (".", "dev"),
        ("..", "dev"),
        ("../outside", "dev"),
        ("/absolute", "dev"),
        (r"chart\child", "dev"),
        ("C:chart", "dev"),
        ("chart\nchild", "dev"),
        ("demo", ""),
        ("demo", " "),
        ("demo", "."),
        ("demo", ".."),
        ("demo", "../outside"),
        ("demo", "/absolute"),
        ("demo", r"env\child"),
    ],
)
def test_unsafe_output_identifiers_are_rejected_before_mutation(
    tmp_path: Path,
    chart: str,
    env: str,
) -> None:
    output_root = tmp_path / "out"
    helm = _StubHelm(succeed=True)
    runner = ManifestValidationRunner(helm=helm, output_root=output_root)
    row = WorklistRow(chart=chart, env=env, release="demo", namespace="lab-dev")

    with pytest.raises(SpecError, match=r"path segment|absolute path"):
        runner.run([_cfg(row, tmp_path / "chart")])

    assert not output_root.exists()
    assert helm.dep_update_calls == []
    assert helm.calls == []


def test_output_component_symlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.yaml"
    sentinel.write_text("do not remove\n")
    output_root.mkdir()
    (output_root / "demo").symlink_to(outside, target_is_directory=True)
    helm = _StubHelm(succeed=True)
    runner = ManifestValidationRunner(helm=helm, output_root=output_root)

    with pytest.raises(SpecError, match="must not be a symlink"):
        runner.run([_cfg(_row(), tmp_path / "chart")])

    assert sentinel.read_text() == "do not remove\n"
    assert helm.dep_update_calls == []
    assert helm.calls == []


def test_existing_case_output_is_cleared_before_render(tmp_path: Path) -> None:
    output_root = tmp_path / "out"
    case_dir = output_root / "demo" / "dev"
    case_dir.mkdir(parents=True)
    stale = case_dir / "stale.yaml"
    stale.write_text("kind: Stale\n")
    helm = _StubHelm(succeed=True)
    runner = ManifestValidationRunner(
        helm=helm,
        output_root=output_root,
        kubeconform=_StubKubeconform(_ok_report()),
    )

    result = runner.run([_cfg(_row(), tmp_path / "chart")])

    assert result.rows[0].phases["render"].status == "PASS"
    assert not stale.exists()
    assert (case_dir / "rendered.yaml").is_file()


def test_stale_output_symlink_is_removed_without_following_it(tmp_path: Path) -> None:
    output_root = tmp_path / "out"
    case_dir = output_root / "demo" / "dev"
    case_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.yaml"
    sentinel.write_text("do not remove\n")
    (case_dir / "linked").symlink_to(outside, target_is_directory=True)
    helm = _StubHelm(succeed=True)
    runner = ManifestValidationRunner(
        helm=helm,
        output_root=output_root,
        kubeconform=_StubKubeconform(_ok_report()),
    )

    result = runner.run([_cfg(_row(), tmp_path / "chart")])

    assert result.rows[0].phases["render"].status == "PASS"
    assert sentinel.read_text() == "do not remove\n"
    assert not (case_dir / "linked").exists()


def test_non_directory_case_output_is_rejected_without_removing_it(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "out"
    case_path = output_root / "demo" / "dev"
    case_path.parent.mkdir(parents=True)
    case_path.write_text("keep\n")
    helm = _StubHelm(succeed=True)
    runner = ManifestValidationRunner(helm=helm, output_root=output_root)

    with pytest.raises(SpecError, match="is not a directory"):
        runner.run([_cfg(_row(), tmp_path / "chart")])

    assert case_path.read_text() == "keep\n"
    assert helm.calls == []


def test_render_fail_skips_schema_and_policy_with_upstream_detail(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=False)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    row = _row()
    result = runner.run([_cfg(row, tmp_path / "chart", policy_paths=[Path("/p")])])

    assert kc.calls == []
    assert ky.calls == []
    row_result = result.rows[0]
    assert row_result.phases["render"].status == "FAIL"
    assert row_result.phases["render"].error_type == "tool"
    assert row_result.phases["schema"].status == "SKIP"
    assert row_result.phases["schema"].detail == "upstream render FAIL"
    assert row_result.phases["policy"].status == "SKIP"
    assert row_result.phases["policy"].detail == "upstream render FAIL"
    # Render tool crash promotes the run to a tool outcome (exit 4).
    assert result.outcome() is Outcome.TOOL


def test_render_fail_preserves_disabled_validator_semantics(tmp_path: Path) -> None:
    runner = ManifestValidationRunner(
        helm=_StubHelm(succeed=False),
        output_root=tmp_path / "out",
        kubeconform=_StubKubeconform(_ok_report()),
        kyverno=_StubKyverno(report=_kyverno_pass()),
    )

    result = runner.run(
        [
            _cfg(
                _row(),
                tmp_path / "chart",
                enabled_validators=frozenset(),
            )
        ]
    )

    phases = result.rows[0].phases
    assert phases["render"].status == "FAIL"
    assert phases["schema"].status == "SKIP"
    assert phases["schema"].skip_cause == "validator_disabled"
    assert phases["policy"].status == "SKIP"
    assert phases["policy"].skip_cause == "validator_disabled"
    assert result.outcome() is Outcome.TOOL


def test_schema_fail_skips_policy_with_upstream_detail(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(
        KubeconformReport(
            resources=(
                ResourceResult(
                    filename="/r/x.yaml",
                    kind="Deployment",
                    name="bad",
                    status="invalid",
                    msg="/spec/replicas: got string, want integer",
                ),
            ),
            summary={"valid": 0, "invalid": 1, "errors": 0, "skipped": 0},
        )
    )
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run([_cfg(_row(), tmp_path / "chart", policy_paths=[Path("/p")])])

    assert ky.calls == []
    row_result = result.rows[0]
    assert row_result.phases["schema"].status == "FAIL"
    assert row_result.phases["policy"].status == "SKIP"
    assert row_result.phases["policy"].detail == "upstream schema FAIL"
    assert result.outcome() is Outcome.FAILED


def test_schema_fail_preserves_disabled_policy_semantics(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kubeconform = _StubKubeconform(
        KubeconformReport(
            resources=(
                ResourceResult(
                    filename="/r/x.yaml",
                    kind="Deployment",
                    name="bad",
                    status="invalid",
                    msg="invalid",
                ),
            ),
            summary={"valid": 0, "invalid": 1, "errors": 0, "skipped": 0},
        )
    )
    kyverno = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm,
        output_root=tmp_path / "out",
        kubeconform=kubeconform,
        kyverno=kyverno,
    )

    result = runner.run(
        [
            _cfg(
                _row(),
                tmp_path / "chart",
                enabled_validators=frozenset({"schema"}),
            )
        ]
    )

    policy = result.rows[0].phases["policy"]
    assert result.rows[0].phases["schema"].status == "FAIL"
    assert policy.status == "SKIP"
    assert policy.detail == "disabled by chart-lifecycle"
    assert policy.skip_cause == "validator_disabled"
    assert kyverno.calls == []
    assert result.outcome() is Outcome.FAILED


def test_policy_runs_after_passing_schema(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    policy_paths = [tmp_path / "policies"]
    (tmp_path / "policies").mkdir()
    result = runner.run([_cfg(_row(), tmp_path / "chart", policy_paths=policy_paths)])

    assert len(ky.calls) == 1
    assert ky.calls[0]["policy_paths"] == policy_paths
    row_result = result.rows[0]
    assert row_result.phases["policy"].status == "PASS"
    assert result.outcome() is Outcome.SUCCESS


def test_policy_failure_yields_exit_one(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(
        report=KyvernoReport(
            results=(
                PolicyResult(
                    policy="require-non-root",
                    rule="containers-must-run-as-non-root",
                    resource_kind="Deployment",
                    resource_name="bad",
                    resource_namespace="default",
                    status="fail",
                    message="validation error: runAsNonRoot",
                ),
            ),
            summary={"pass": 0, "fail": 1},
        )
    )
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run([_cfg(_row(), tmp_path / "chart", policy_paths=[Path("/p")])])

    policy_phase = result.rows[0].phases["policy"]
    assert policy_phase.status == "FAIL"
    assert policy_phase.error_type is None
    assert "require-non-root/containers-must-run-as-non-root" in (policy_phase.detail or "")
    assert "Deployment/bad" in (policy_phase.detail or "")
    assert result.outcome() is Outcome.FAILED


def test_runner_does_not_fail_fast_across_rows(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(
        KubeconformReport(
            resources=(
                ResourceResult(
                    filename="/r/x.yaml",
                    kind="Deployment",
                    name="bad",
                    status="invalid",
                    msg="boom",
                ),
            ),
            summary={"valid": 0, "invalid": 1, "errors": 0, "skipped": 0},
        )
    )
    runner = ManifestValidationRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    rows = [_row("a"), _row("b")]
    configs = [_cfg(r, tmp_path / "chart") for r in rows]
    result = runner.run(configs)

    assert len(result.rows) == 2
    assert len(kc.calls) == 2
    assert all(rr.phases["schema"].status == "FAIL" for rr in result.rows)


@pytest.mark.parametrize("workers", [1, 3])
def test_unexpected_row_crash_isolated_with_serial_parallel_parity(
    tmp_path: Path,
    workers: int,
) -> None:
    class _ExplodingHelm(_StubHelm):
        def template(self, release, chart_ref, **kwargs):  # type: ignore[override]
            if release == "bad":
                raise RuntimeError("kaboom")
            return super().template(release, chart_ref, **kwargs)

    runner = ManifestValidationRunner(
        helm=_ExplodingHelm(succeed=True),
        output_root=tmp_path / "out",
        kubeconform=_StubKubeconform(_ok_report()),
        max_workers=workers,
    )

    result = runner.run(
        [
            _cfg(_row("bad"), tmp_path / "bad"),
            _cfg(_row("good"), tmp_path / "good"),
        ]
    )

    by_chart = {row.row.chart: row for row in result.rows}
    assert by_chart["bad"].phases["render"].status == "FAIL"
    assert by_chart["bad"].phases["render"].error_type == "tool"
    assert "kaboom" in (by_chart["bad"].phases["render"].detail or "")
    assert by_chart["good"].phases["render"].status == "PASS"


@pytest.mark.parametrize("workers", [1, 3])
def test_dependency_prefetch_failure_isolated_by_chart(
    tmp_path: Path,
    workers: int,
) -> None:
    class _PrefetchFailureHelm(_StubHelm):
        def dependency_update(self, chart_path: Path, *, timeout: float | None = None) -> None:
            super().dependency_update(chart_path, timeout=timeout)
            if chart_path.name == "bad":
                raise RuntimeError("registry unavailable")

    runner = ManifestValidationRunner(
        helm=_PrefetchFailureHelm(succeed=True),
        output_root=tmp_path / "out",
        kubeconform=_StubKubeconform(_ok_report()),
        max_workers=workers,
    )

    result = runner.run(
        [
            _cfg(_row("bad"), tmp_path / "bad"),
            _cfg(_row("good"), tmp_path / "good"),
        ]
    )

    by_chart = {row.row.chart: row for row in result.rows}
    bad = by_chart["bad"].phases["render"]
    assert bad.status == "FAIL"
    assert bad.error_type == "tool"
    assert "dependency prefetch failed" in (bad.detail or "")
    assert "registry unavailable" in (bad.detail or "")
    assert by_chart["good"].phases["render"].status == "PASS"


def test_fail_fast_stops_later_rows_and_marks_them_not_run(tmp_path: Path) -> None:
    class _ExplodingHelm(_StubHelm):
        def template(self, release, chart_ref, **kwargs):  # type: ignore[override]
            if release == "bad":
                raise RuntimeError("kaboom")
            return super().template(release, chart_ref, **kwargs)

    helm = _ExplodingHelm(succeed=True)
    runner = ManifestValidationRunner(
        helm=helm,
        output_root=tmp_path / "out",
        kubeconform=_StubKubeconform(_ok_report()),
        max_workers=4,
    )

    result = runner.run(
        [
            _cfg(_row("bad"), tmp_path / "bad"),
            _cfg(_row("later"), tmp_path / "later"),
        ],
        fail_fast=True,
    )

    by_chart = {row.row.chart: row for row in result.rows}
    assert by_chart["bad"].phases["render"].status == "FAIL"
    assert {phase.status for phase in by_chart["later"].phases.values()} == {"NOT_RUN"}
    assert [call["release"] for call in helm.calls] == []


@pytest.mark.parametrize("workers", [1, 3])
def test_every_phase_result_has_exactly_one_terminal_event(
    tmp_path: Path,
    workers: int,
) -> None:
    events: list[tuple[str, str, str]] = []
    runner = ManifestValidationRunner(
        helm=_StubHelm(succeed=True),
        output_root=tmp_path / "out",
        kubeconform=_StubKubeconform(_ok_report()),
        max_workers=workers,
        on_event=lambda row, phase, status, _elapsed: events.append((row.chart, phase, status)),
    )

    result = runner.run(
        [
            _cfg(_row("a"), tmp_path / "a"),
            _cfg(_row("b"), tmp_path / "b"),
        ],
        enabled_phases=frozenset({"render"}),
    )

    terminal = [event for event in events if event[2] != "running"]
    expected = [
        (row.row.chart, phase.phase, phase.status)
        for row in result.rows
        for phase in row.phases.values()
    ]
    assert sorted(terminal) == sorted(expected)
    assert len(terminal) == len(set(terminal)) == 6


def test_phases_subset_marks_disabled_as_not_run(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run(
        [_cfg(_row(), tmp_path / "chart", policy_paths=[tmp_path / "p"])],
        enabled_phases=frozenset({"render", "schema"}),
    )

    row_result = result.rows[0]
    assert row_result.phases["render"].status == "PASS"
    assert row_result.phases["schema"].status == "PASS"
    assert row_result.phases["policy"].status == "NOT_RUN"
    assert ky.calls == []
    assert result.outcome() is Outcome.SUCCESS


def test_phases_subset_excluding_render_still_renders(tmp_path: Path) -> None:
    """A subset that leaves out `render` must still render.

    Schema and policy read the rendered tree, so "disable render" cannot
    mean "skip render" — the runner docstring states this, but nothing
    covered the case: the existing subset test keeps render enabled.
    Without this, the runner's dead "downgrade later phases to SKIP when
    render was NOT_RUN" branch looked load-bearing.
    """
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run(
        [_cfg(_row(), tmp_path / "chart", policy_paths=[tmp_path / "p"])],
        enabled_phases=frozenset({"schema"}),
    )

    row_result = result.rows[0]
    # Render ran and PASSed even though the caller did not ask for it.
    assert row_result.phases["render"].status == "PASS"
    assert len(helm.calls) == 1
    assert row_result.phases["schema"].status == "PASS"
    assert kc.calls != []
    assert row_result.phases["policy"].status == "NOT_RUN"
    assert ky.calls == []
    assert result.outcome() is Outcome.SUCCESS


def test_chart_disabled_kubeconform_skips_it_without_blocking_policy(
    tmp_path: Path,
) -> None:
    helm = _StubHelm(succeed=True)
    kubeconform = _StubKubeconform(_ok_report())
    kyverno = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm,
        output_root=tmp_path / "out",
        kubeconform=kubeconform,
        kyverno=kyverno,
    )

    result = runner.run(
        [
            _cfg(
                _row(),
                tmp_path / "chart",
                policy_paths=[tmp_path / "policies"],
                enabled_validators=frozenset({"policy"}),
            )
        ]
    )

    phases = result.rows[0].phases
    assert phases["render"].status == "PASS"
    assert phases["schema"].status == "SKIP"
    assert phases["schema"].detail == "disabled by chart-lifecycle"
    assert phases["policy"].status == "PASS"
    assert kubeconform.calls == []
    assert len(kyverno.calls) == 1


def test_chart_disabled_policy_is_a_visible_skip_and_never_invokes_kyverno(
    tmp_path: Path,
) -> None:
    helm = _StubHelm(succeed=True)
    kyverno = _StubKyverno(report=_kyverno_pass())
    runner = ManifestValidationRunner(
        helm=helm,
        output_root=tmp_path / "out",
        kubeconform=_StubKubeconform(_ok_report()),
        kyverno=kyverno,
    )

    result = runner.run(
        [
            _cfg(
                _row(),
                tmp_path / "chart",
                policy_paths=[tmp_path / "policies"],
                enabled_validators=frozenset({"schema"}),
            )
        ]
    )

    policy = result.rows[0].phases["policy"]
    assert policy.status == "SKIP"
    assert policy.detail == "disabled by chart-lifecycle"
    assert kyverno.calls == []


def test_phases_subset_multi_row_batch(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    runner = ManifestValidationRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    configs = [
        _cfg(_row("a"), tmp_path / "chart-a"),
        _cfg(_row("b"), tmp_path / "chart-b"),
        _cfg(_row("c"), tmp_path / "chart-c"),
    ]
    result = runner.run(configs)

    assert len(result.rows) == 3
    assert all(rr.phases["render"].status == "PASS" for rr in result.rows)
    assert all(rr.phases["schema"].status == "PASS" for rr in result.rows)
    assert result.outcome() is Outcome.SUCCESS


def test_parallel_run_returns_all_rows_with_events(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    events: list[tuple[str, str, str, bool]] = []

    def on_event(row, phase, status, elapsed_s):
        events.append((row.chart, phase, status, elapsed_s is not None))

    runner = ManifestValidationRunner(
        helm=helm,
        output_root=tmp_path / "out",
        kubeconform=kc,
        max_workers=4,
        on_event=on_event,
    )
    configs = [_cfg(_row(f"chart-{i}"), tmp_path / f"chart-{i}") for i in range(6)]
    result = runner.run(configs)

    assert len(result.rows) == 6
    # Deterministic sort by (chart, env) regardless of completion order.
    assert [r.row.chart for r in result.rows] == sorted(r.row.chart for r in result.rows)
    # 3 phases (render, schema, policy) x 6 rows x 2 events (running + end)
    # = 36. Policy SKIPs on `no policies discovered`, but the runner still
    # times it and emits both events.
    assert len(events) == 36
    # Every end-event carries an elapsed measurement.
    end_events = [e for e in events if e[2] != "running"]
    assert all(e[3] for e in end_events)


def test_parallel_run_isolates_worker_crash_into_row_failure(tmp_path: Path) -> None:
    boom = RuntimeError("kaboom")

    class _ExplodingHelm(_StubHelm):
        def template(self, release, chart_ref, **kwargs):  # type: ignore[override]
            if release == "bad":
                raise boom
            return super().template(release, chart_ref, **kwargs)

    helm = _ExplodingHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    runner = ManifestValidationRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, max_workers=2
    )

    rows = [_row("good"), _row("bad")]
    configs = [_cfg(r, tmp_path / r.chart) for r in rows]
    result = runner.run(configs)

    # Both rows present; the crash converts to a tool-error FAIL render row.
    assert len(result.rows) == 2
    by_chart = {r.row.chart: r for r in result.rows}
    assert by_chart["bad"].phases["render"].status == "FAIL"
    assert by_chart["bad"].phases["render"].error_type == "tool"
    # Phase fns ARE called by _run_row, not _crash_row, so an in-phase
    # ExternalCommandError surfaces here. But this test uses a bare
    # RuntimeError, which the phase fn re-raises, escaping to the
    # worker. Verify the crash text bubbled into the detail.
    detail = by_chart["bad"].phases["render"].detail or ""
    assert "kaboom" in detail or "worker crashed" in detail
    # The good row still passes.
    assert by_chart["good"].phases["render"].status == "PASS"
    # Crash row also short-circuits schema/policy to SKIP.
    assert by_chart["bad"].phases["schema"].status == "SKIP"
    assert by_chart["bad"].phases["policy"].status == "SKIP"


def test_serial_path_when_max_workers_one(tmp_path: Path) -> None:
    # Sentinel: max_workers=1 must NOT use a ThreadPoolExecutor (the
    # original execution shape). We assert by patching the import site;
    # a call to ThreadPoolExecutor in the serial path would surface here.
    import chart_manager.services.manifest_validation.runner as runner_mod

    calls = []
    real_pool = runner_mod.ThreadPoolExecutor

    class _TrackingPool(real_pool):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            calls.append("pool")
            super().__init__(*a, **kw)

    runner_mod.ThreadPoolExecutor = _TrackingPool  # type: ignore[assignment]
    try:
        helm = _StubHelm(succeed=True)
        kc = _StubKubeconform(_ok_report())
        runner = ManifestValidationRunner(
            helm=helm, output_root=tmp_path / "out", kubeconform=kc, max_workers=1
        )
        runner.run([_cfg(_row(), tmp_path / "c")])
    finally:
        runner_mod.ThreadPoolExecutor = real_pool  # type: ignore[assignment]
    assert calls == []


def test_schema_inputs_threaded_into_kubeconform(tmp_path: Path) -> None:
    captured: dict = {}

    class _CapturingKubeconform(_StubKubeconform):
        def validate(
            self,
            manifests_dir,
            *,
            kubernetes_version=None,
            schema_locations=None,
            skip_kinds=None,
            strict=True,
            extra_args=None,
        ):
            captured["kubernetes_version"] = kubernetes_version
            captured["schema_locations"] = schema_locations
            return super().validate(manifests_dir)

    kc = _CapturingKubeconform(_ok_report())
    helm = _StubHelm(succeed=True)
    runner = ManifestValidationRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    runner.run(
        [
            _cfg(
                _row(),
                tmp_path / "chart",
                kubernetes_version="1.31.2",
                schema_locations=["/local"],
            )
        ]
    )

    assert captured["kubernetes_version"] == "1.31.2"
    assert captured["schema_locations"] == ["/local"]
