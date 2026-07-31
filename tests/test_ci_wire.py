"""The GitHub Actions cluster-test matrix contract, tested without a surface.

`tests/test_ci_matrix_cli.py` asserts the same bytes through the CLI. This
module asserts them one layer down, so a second surface that never goes
through Typer is covered by the same contract, and so a shape regression
points at `services/ci_wire.py` instead of at a command.
"""

from __future__ import annotations

import json
from pathlib import Path

from chart_manager.services.ci import MatrixSelection, _select_cluster_tests
from chart_manager.services.ci_wire import cluster_test_matrix_to_dict
from chart_manager.services.lifecycle import ClusterTestImpact
from chart_manager.services.lifecycle.impact import ImpactReason, ImpactReasonCode


class _Source:
    """A matrix source that records which selector the dispatch chose."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def cluster_test_matrix(self, base: str) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("diff", base))
        return (ClusterTestImpact("consumer", "full", ()),)

    def all_cluster_test_matrix(self) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("all", None))
        return (ClusterTestImpact("source", "minimal", ()),)

    def explicit_cluster_test_matrix(
        self,
        charts: list[str],
    ) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("explicit", charts))
        return tuple(ClusterTestImpact(c, "minimal", ()) for c in charts)


# --- the payload shape ----------------------------------------------------


def test_payload_is_the_github_matrix_include_shape() -> None:
    entries = (
        ClusterTestImpact("consumer", "full", ()),
        ClusterTestImpact("source", "minimal", ()),
    )

    assert cluster_test_matrix_to_dict(entries) == {
        "include": [
            {"chart": "consumer", "profile": "full"},
            {"chart": "source", "profile": "minimal"},
        ]
    }


def test_payload_drops_selection_reasons() -> None:
    """`reasons` would become a `matrix.reasons` dimension on every job."""
    entry = ClusterTestImpact(
        "consumer",
        "full",
        (
            ImpactReason(
                code=ImpactReasonCode.HELM_DEPENDENT,
                changed_file=Path("charts/source/Chart.yaml"),
                detail="source changed",
            ),
        ),
    )

    (rendered,) = cluster_test_matrix_to_dict([entry])["include"]

    assert rendered == {"chart": "consumer", "profile": "full"}
    assert "reasons" in entry.to_dict()


def test_empty_selection_is_an_empty_include_not_a_missing_key() -> None:
    """`strategy.matrix` needs the key present; a missing one is a workflow error."""
    payload = cluster_test_matrix_to_dict(())

    assert payload == {"include": []}
    assert json.dumps(payload, separators=(",", ":"), sort_keys=True) == '{"include":[]}'


def test_entry_order_is_preserved() -> None:
    """Selection order is the service's; the wire must not re-sort it."""
    entries = [
        ClusterTestImpact("zeta", "minimal", ()),
        ClusterTestImpact("alpha", "minimal", ()),
    ]

    assert [e["chart"] for e in cluster_test_matrix_to_dict(entries)["include"]] == [
        "zeta",
        "alpha",
    ]


# --- the selection dispatch ----------------------------------------------
#
# Exercised through the private `_select_cluster_tests` rather than
# `CiService.matrix`, its only caller: `CiService.__init__` wires a chart
# repository, a cluster-test catalog, an impact service and Git against a
# real root, none of which the precedence rules depend on. The method is a
# one-line delegation, and `tests/test_ci_matrix_cli.py` covers the path a
# surface actually takes.


def test_default_selection_diffs_against_base() -> None:
    source = _Source()

    entries = _select_cluster_tests(source, MatrixSelection(base="merge-base"))

    assert source.calls == [("diff", "merge-base")]
    assert [e.chart for e in entries] == ["consumer"]


def test_all_charts_selects_every_enabled_chart() -> None:
    source = _Source()

    _select_cluster_tests(source, MatrixSelection(all_charts=True))

    assert source.calls == [("all", None)]


def test_explicit_charts_are_passed_through_as_a_list_in_request_order() -> None:
    source = _Source()

    _select_cluster_tests(source, MatrixSelection(charts=("beta", "alpha")))

    assert source.calls == [("explicit", ["beta", "alpha"])]


def test_all_charts_wins_over_explicit_charts() -> None:
    """Documented precedence. Rejecting the combination is a surface concern."""
    source = _Source()

    _select_cluster_tests(
        source,
        MatrixSelection(all_charts=True, charts=("beta",)),
    )

    assert source.calls == [("all", None)]
