# lab-charts

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
uv run lab-charts charts list
uv run lab-charts deps plan alloy --profile minimal
uv run lab-charts kind test alloy --profile minimal
```
