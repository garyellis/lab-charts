# Grafana Dashboards Chart

A Helm chart that dynamically creates ConfigMaps from dashboard JSON files for Grafana sidecar discovery.

## How It Works

1. Place any Grafana dashboard JSON file in the `dashboards/` directory
2. The chart automatically creates a ConfigMap for each `.json` file
3. ConfigMaps are labeled with `grafana_dashboard: "1"`
4. Grafana's sidecar container discovers and loads them automatically

## Usage

### Adding a New Dashboard

Simply add a `.json` file to `dashboards/`:

```bash
cp my-new-dashboard.json dashboards/
helm upgrade grafana-dashboards . -n observability
```

The dashboard will be automatically discovered without needing to modify any templates.

### Dashboard Naming Convention

- File: `gpu-monitoring.json` → ConfigMap: `grafana-dashboard-gpu-monitoring`
- File: `network-overview.json` → ConfigMap: `grafana-dashboard-network-overview`

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `sidecar.label` | Label key for Grafana sidecar discovery | `grafana_dashboard` |
| `sidecar.labelValue` | Label value for discovery | `"1"` |
| `dashboardFolder` | Optional folder name in Grafana UI | `""` (default) |
| `additionalLabels` | Extra labels to apply to all ConfigMaps | `{}` |

## Current Dashboards

- **gpu-monitoring.json** - GPU power and utilization metrics
- **network-drop.json** - Network packet drop analysis
- **network-overview.json** - Network traffic overview
- **slo.json** - Service Level Objectives

## Requirements

- Grafana chart must have sidecar enabled with matching label configuration
- See `values/grafana.yaml` for sidecar settings
