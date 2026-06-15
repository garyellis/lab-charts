"""Validate pipeline: render -> schema -> policy.

Architectural contract:
    Pure phase functions over a rendered dir; the runner orchestrates.
    The CLI is the only env-aware layer (paths, run-ids, cleanup, exit).

Phases must not perform IO beyond what the runner gives them. The runner
must not interpret phase results beyond aggregation. This split is what
lets us unit-test phases without subprocess mocking pyramids and lets the
runner stay reusable from both `validate render` and the future
`validate run` command.
"""
