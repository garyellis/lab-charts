"""Contract tests for the upgrade wire projections.

These exercise `services/upgrader/wire.py` directly, with no Typer and no
CliRunner in sight -- which is the point of the module existing. A REST
handler or a Slack app calls exactly what these tests call.
"""

from __future__ import annotations

import json
from pathlib import Path

from chart_manager.services.upgrader import FinalizeResult, UpgradeResult
from chart_manager.services.upgrader.wire import (
    SCHEMA_VERSION,
    finalize_to_dict,
    upgrade_to_dict,
)

#: Every key both projections must emit, at every version of the contract.
_CONTRACT_KEYS = {
    "schema_version",
    "repository",
    "base",
    "chart",
    "path",
    "current_wrapper_version",
    "proposed_wrapper_version",
    "branch",
    "outcome",
    "pull_request",
    "diagnostics",
}


def _upgrade_result(**overrides: object) -> UpgradeResult:
    fields: dict[str, object] = {
        "chart": "loki",
        "chart_path": Path("charts/loki"),
        "current_version": "1.2.3",
        "proposed_version": "1.2.4",
        "branch": "renovate/loki",
        "group": "chart-manager:loki",
        "outcome": "pr_open",
        "diagnostics": ("registry lookup retried",),
        "pr_url": "https://example.test/pull/7",
        "pr_number": 7,
        "repository": "owner/repository",
        "base": "main",
    }
    fields.update(overrides)
    return UpgradeResult(**fields)  # type: ignore[arg-type]


def _finalize_result(**overrides: object) -> FinalizeResult:
    fields: dict[str, object] = {
        "chart": "loki",
        "previous_version": "1.2.3",
        "version": "2.0.0",
        "bump": "major",
        "changed": True,
    }
    fields.update(overrides)
    return FinalizeResult(**fields)  # type: ignore[arg-type]


def test_upgrade_projection_is_the_full_contract() -> None:
    assert upgrade_to_dict(_upgrade_result()) == {
        "schema_version": SCHEMA_VERSION,
        "repository": "owner/repository",
        "base": "main",
        "chart": "loki",
        "path": "charts/loki",
        "current_wrapper_version": "1.2.3",
        "proposed_wrapper_version": "1.2.4",
        "branch": "renovate/loki",
        "outcome": "pr_open",
        "pull_request": {"url": "https://example.test/pull/7", "number": 7},
        "diagnostics": ["registry lookup retried"],
    }


def test_finalize_projection_is_the_full_contract() -> None:
    assert finalize_to_dict(_finalize_result(), chart_path=Path("charts/loki")) == {
        "schema_version": SCHEMA_VERSION,
        "repository": None,
        "base": None,
        "chart": "loki",
        "path": "charts/loki",
        "current_wrapper_version": "1.2.3",
        "proposed_wrapper_version": "2.0.0",
        "branch": None,
        "outcome": "updated",
        "pull_request": None,
        "diagnostics": [],
    }


def test_both_projections_emit_identical_key_sets() -> None:
    """One contract, two producers.

    This is the invariant that replaced the CLI's `_first(...)` key-fallback
    chain: a consumer must never have to probe for which spelling of the
    wrapper version a given result object happened to use.
    """
    upgrade_keys = set(upgrade_to_dict(_upgrade_result()))
    finalize_keys = set(finalize_to_dict(_finalize_result(), chart_path=Path("charts/loki")))

    assert upgrade_keys == finalize_keys == _CONTRACT_KEYS


def test_finalize_maps_previous_version_onto_the_current_wrapper_version_key() -> None:
    """`previous_version`/`version` are the same pair `UpgradeResult` calls
    `current_version`/`proposed_version` -- a naming difference, not a
    semantic one."""
    payload = finalize_to_dict(
        _finalize_result(previous_version="9.9.9", version="10.0.0"),
        chart_path=Path("charts/loki"),
    )

    assert payload["current_wrapper_version"] == "9.9.9"
    assert payload["proposed_wrapper_version"] == "10.0.0"


def test_finalize_outcome_is_derived_from_changed() -> None:
    unchanged = finalize_to_dict(
        _finalize_result(changed=False, version="1.2.3", bump=None),
        chart_path=Path("charts/loki"),
    )
    changed = finalize_to_dict(_finalize_result(changed=True), chart_path=Path("charts/loki"))

    assert unchanged["outcome"] == "unchanged"
    assert changed["outcome"] == "updated"
    # An unchanged finalize still reports a version -- unlike an upgrade with
    # nothing to propose, which reports null. `outcome` is the discriminator.
    assert unchanged["proposed_wrapper_version"] == "1.2.3"


def test_upgrade_with_nothing_to_propose_nulls_the_proposal_and_the_pr() -> None:
    payload = upgrade_to_dict(
        _upgrade_result(
            proposed_version=None,
            branch=None,
            outcome="no_update",
            diagnostics=(),
            pr_url=None,
            pr_number=None,
            repository=None,
            base=None,
        )
    )

    assert payload["proposed_wrapper_version"] is None
    assert payload["pull_request"] is None
    assert payload["diagnostics"] == []
    assert payload["outcome"] == "no_update"


def test_partial_pull_request_coordinates_still_produce_an_object() -> None:
    """Reporting `"pull_request": null` for a run that opened one would be a lie."""
    assert upgrade_to_dict(_upgrade_result(pr_number=None))["pull_request"] == {
        "url": "https://example.test/pull/7",
        "number": None,
    }
    assert upgrade_to_dict(_upgrade_result(pr_url=None))["pull_request"] == {
        "url": None,
        "number": 7,
    }


def test_projections_are_json_encodable_without_a_custom_encoder() -> None:
    """Paths and tuples must already be normalized; a surface gets plain data."""
    for payload in (
        upgrade_to_dict(_upgrade_result()),
        finalize_to_dict(_finalize_result(), chart_path=Path("charts/loki")),
    ):
        assert json.loads(json.dumps(payload)) == payload
        assert isinstance(payload["path"], str)
        assert isinstance(payload["diagnostics"], list)


def test_absolute_chart_paths_are_posix_normalized() -> None:
    payload = finalize_to_dict(_finalize_result(), chart_path=Path("/repo/charts/loki"))

    assert payload["path"] == "/repo/charts/loki"


def test_schema_version_lives_in_the_service_layer() -> None:
    """The declared version is the service's to own -- see design commitment 5."""
    assert upgrade_to_dict(_upgrade_result())["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION == 1
