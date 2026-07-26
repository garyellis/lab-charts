"""Validate pipeline: render -> schema -> policy.

Architectural contract, by owner:

    `phases.py`    — pure functions over a rendered dir. No sequencing, no
                     env awareness; they do no IO beyond what the runner
                     hands them.
    `runner.py`    — sequences the phases per row and fans rows out across
                     workers. Does not interpret phase results beyond
                     aggregating them into a `RunResult`.
    `worklist.py`  — which (chart, env) rows exist, and what each one runs
                     with (chart path, values, policy dirs).
    `app.py`       — `ValidateApp`: the env-aware layer. Changed-file
                     resolution, render-dir paths, run ids, artifact
                     retention/cleanup, worker counts, helm bindings.
    `wire.py`      — machine-readable projections (JSON, markdown).

The CLI owns exactly one thing this package does not: terminating the
process. Everything else it used to own — paths, run ids, cleanup — moved
into `ValidateApp` so that a REST worker or Slack handler drives the same
pipeline without reimplementing them.

This split is what lets us unit-test phases without subprocess mocking
pyramids, and lets the runner stay reusable from both `validate render`
and `validate run`.
"""
