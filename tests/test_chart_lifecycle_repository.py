"""Repository-wide guards for the authored ChartLifecycle contract."""

from __future__ import annotations

from chart_manager.api.v1alpha1.chart_lifecycle import CHART_LIFECYCLE_KIND
from chart_manager.api.v1alpha1.common import API_VERSION
from chart_manager.plumbing.yaml_files import parse_yaml
from chart_manager.shared.charts.lifecycle import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    chart_test_status,
    load_chart_lifecycle,
    validation_status,
)

from .conftest import REPO_ROOT

#: Charts that author `chartTest` as disabled. An offline kind sandbox
#: cannot produce a truthful signal for these, so the invariant below is
#: relaxed for exactly the names listed here -- and only here, so that
#: skipping chart tests stays a deliberate, reviewed act instead of
#: something a new chart can quietly drift into.
CHART_TEST_OPT_OUTS = {
    # The preview Cosmos DB emulator can remain unready for more than twenty
    # minutes on hosted runners, so its live test is quarantined while static
    # render, schema, and policy validation remain enabled.
    "cosmosdb-emulator",
    # Two reasons, both properties of the live environment rather than of the
    # chart: the OpenStack Designate webhook authenticates to Keystone during
    # process startup, and nothing installs the DNSEndpoint CRD that this
    # chart's `sources: [crd]` depends on. edge-w acceptance is where both
    # exist, so that is the honest place for the signal.
    "external-dns",
}


def test_every_production_chart_has_one_valid_enabled_config() -> None:
    charts_root = REPO_ROOT / "charts"
    chart_dirs = {path.parent for path in charts_root.glob("*/Chart.yaml")}
    lifecycle_dirs = {path.parent for path in charts_root.glob(f"*/{LIFECYCLE_FILENAME}")}

    assert chart_dirs == lifecycle_dirs
    # An opt-out naming a chart that no longer exists would silently weaken
    # nothing, but it would still be a lie about the repository.
    assert CHART_TEST_OPT_OUTS.issubset(chart_dir.name for chart_dir in chart_dirs)
    for chart_dir in sorted(chart_dirs):
        config_path = chart_dir / LIFECYCLE_FILENAME
        document = parse_yaml(config_path.read_text(encoding="utf-8"))
        assert list(document) == ["apiVersion", "kind", "metadata", "spec"], chart_dir.name
        assert document["apiVersion"] == API_VERSION, chart_dir.name
        assert document["kind"] == CHART_LIFECYCLE_KIND, chart_dir.name
        assert document["metadata"] == {"name": chart_dir.name}, chart_dir.name
        assert list(document["spec"]) == [
            "enabled",
            "validation",
            "chartTest",
        ], chart_dir.name

        lifecycle = load_chart_lifecycle(config_path)
        assert lifecycle.spec.enabled, chart_dir.name
        assert validation_status(lifecycle) is CapabilityStatus.ENABLED, chart_dir.name
        # Asserted as an exact status, not merely "not enabled": an opt-out
        # for a chart that later gains a real chart test fails here too, so
        # the list above cannot go stale in the permissive direction either.
        expected_chart_test = (
            CapabilityStatus.DISABLED
            if chart_dir.name in CHART_TEST_OPT_OUTS
            else CapabilityStatus.ENABLED
        )
        assert chart_test_status(lifecycle) is expected_chart_test, chart_dir.name


def test_no_helmignore_excludes_chart_lifecycle_configuration() -> None:
    offenders = []
    for ignore_path in (REPO_ROOT / "charts").glob("*/.helmignore"):
        entries = {
            line.strip()
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if LIFECYCLE_FILENAME in entries:
            offenders.append(ignore_path.relative_to(REPO_ROOT))

    assert offenders == []
