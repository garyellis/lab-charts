"""Chart-domain models, loading boundaries and policy.

`domain/` is a peer of `api/` and `integrations/`, one rank below `services/`:
it interprets the authored contracts in `chart_manager.api` plus the schemas
this project does not own (`Chart.yaml`, `Chart.lock`) and answers questions
about them -- what is enabled, which profile was meant, what installs first,
whether a dependency is stale. It may import `api/`, `plumbing/` and
`settings`, and nothing above it; `chart_manager.services` is banned from here
by TID251 so the direction stays enforced rather than merely intended.
"""
