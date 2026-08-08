"""Repository-wide guards for the authored ChartLifecycle contract."""

from __future__ import annotations

import yaml

from chart_manager.api.lifecycle.v1alpha1 import LIFECYCLE_API_VERSION, LIFECYCLE_KIND
from chart_manager.domain.lifecycle_policy import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    cluster_test_status,
    load_chart_lifecycle,
    validation_status,
)

from .conftest import REPO_ROOT

#: Charts that author `clusterTest` as disabled. An offline kind sandbox
#: cannot produce a truthful signal for these, so the invariant below is
#: relaxed for exactly the names listed here -- and only here, so that
#: skipping cluster tests stays a deliberate, reviewed act instead of
#: something a new chart can quietly drift into.
CLUSTER_TEST_OPT_OUTS = {
    # Two reasons, both properties of the live environment rather than of the
    # chart: the OpenStack Designate webhook authenticates to Keystone during
    # process startup, and nothing installs the DNSEndpoint CRD that this
    # chart's `sources: [crd]` depends on. edge-w acceptance is where both
    # exist, so that is the honest place for the signal.
    "external-dns",
}


def test_every_production_chart_has_one_valid_enabled_config() -> None:
    chart_dirs = sorted(path.parent for path in (REPO_ROOT / "charts").glob("*/Chart.yaml"))

    assert len(chart_dirs) == 29
    # An opt-out naming a chart that no longer exists would silently weaken
    # nothing, but it would still be a lie about the repository.
    assert CLUSTER_TEST_OPT_OUTS.issubset(chart_dir.name for chart_dir in chart_dirs)
    for chart_dir in chart_dirs:
        config_path = chart_dir / LIFECYCLE_FILENAME
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert list(document) == ["apiVersion", "kind", "metadata", "spec"], chart_dir.name
        assert document["apiVersion"] == LIFECYCLE_API_VERSION, chart_dir.name
        assert document["kind"] == LIFECYCLE_KIND, chart_dir.name
        assert document["metadata"] == {"name": chart_dir.name}, chart_dir.name
        assert list(document["spec"]) == [
            "enabled",
            "validation",
            "clusterTest",
        ], chart_dir.name

        lifecycle = load_chart_lifecycle(config_path)
        assert lifecycle.spec.enabled, chart_dir.name
        assert validation_status(lifecycle) is CapabilityStatus.ENABLED, chart_dir.name
        # Asserted as an exact status, not merely "not enabled": an opt-out
        # for a chart that later gains a real cluster test fails here too, so
        # the list above cannot go stale in the permissive direction either.
        expected_cluster_test = (
            CapabilityStatus.DISABLED
            if chart_dir.name in CLUSTER_TEST_OPT_OUTS
            else CapabilityStatus.ENABLED
        )
        assert cluster_test_status(lifecycle) is expected_cluster_test, chart_dir.name


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
