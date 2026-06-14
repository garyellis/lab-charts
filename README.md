# chart-manager

Helm wrapper charts and local/CI testing tools for lab observability deployments.

## Tooling

This repo uses `mise` to pin local tool dependencies:

```bash
mise trust
mise install
mise run setup
```

Managed tools are Python, `uv`, Helm, kubectl, and kind.

Common tasks:

```bash
mise run test
mise run charts
mise run deps -- alloy --profile minimal
mise run kind-test -- alloy --profile minimal
```

## CLI

The Python CLI is available through `uv`:

```bash
uv run chart-manager charts list
uv run chart-manager deps plan alloy --profile minimal
uv run chart-manager kind test alloy --profile minimal
```
