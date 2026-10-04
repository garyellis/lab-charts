"""The GitHub Actions chart-test matrix contract, tested without a surface.

`tests/test_ci_matrix_cli.py` asserts the same bytes through the CLI. This
module asserts them one layer down, so a second surface that never goes
through Typer is covered by the same contract, and so a shape regression
points at `services/ci_wire.py` instead of at a command.
"""

from __future__ import annotations

import json
from pathlib import Path

from chart_manager.commands import test
from chart_manager.services.ci_wire import cluster_test_matrix_to_dict
from chart_manager.services.lifecycle import LifecycleImpact, impact_to_dict

# --- the payload shape ----------------------------------------------------


def test_payload_is_the_github_matrix_include_shape() -> None:
    entries = (
        test.SelectedTest("consumer", "full", ()),
        test.SelectedTest("source", "minimal", ()),
    )

    assert cluster_test_matrix_to_dict(entries) == {
        "include": [
            {"chart": "consumer", "profile": "full"},
            {"chart": "source", "profile": "minimal"},
        ]
    }


def test_payload_drops_selection_reasons() -> None:
    """`reasons` would become a `matrix.reasons` dimension on every job."""
    entry = test.SelectedTest(
        "consumer",
        "full",
        (
            test.Reason(
                code=test.ReasonCode.CHART_CHANGE,
                changed_file=Path("charts/source/Chart.yaml"),
                detail="source changed",
            ),
        ),
    )

    (rendered,) = cluster_test_matrix_to_dict([entry])["include"]

    assert rendered == {"chart": "consumer", "profile": "full"}
    # The impact document keeps what the matrix drops -- same entry, two
    # audiences, which is the whole reason this projection is separate.
    document = impact_to_dict(
        LifecycleImpact(changed_files=(), validation=(), cluster_tests=(entry,))
    )
    assert "reasons" in document["cluster_test_matrix"][0]


def test_empty_selection_is_an_empty_include_not_a_missing_key() -> None:
    """`strategy.matrix` needs the key present; a missing one is a workflow error."""
    payload = cluster_test_matrix_to_dict(())

    assert payload == {"include": []}
    assert json.dumps(payload, separators=(",", ":"), sort_keys=True) == '{"include":[]}'


def test_entry_order_is_preserved() -> None:
    """Selection order is the service's; the wire must not re-sort it."""
    entries = [
        test.SelectedTest("zeta", "minimal", ()),
        test.SelectedTest("alpha", "minimal", ()),
    ]

    assert [e["chart"] for e in cluster_test_matrix_to_dict(entries)["include"]] == [
        "zeta",
        "alpha",
    ]
