# Testing the Grafana Dashboards Chart

## Pre-deployment Testing

### 1. Validate Chart Structure
```bash
helm lint ./grafana-dashboards
```

### 2. Render Templates (Dry-run)
```bash
# See all generated ConfigMaps
helm template grafana-dashboards ./grafana-dashboards --namespace observability

# Count how many dashboards will be created
helm template grafana-dashboards ./grafana-dashboards --namespace observability | grep "kind: ConfigMap" | wc -l
```

### 3. Verify Sidecar Labels
```bash
# Check that grafana_dashboard label is present
helm template grafana-dashboards ./grafana-dashboards --namespace observability | grep "grafana_dashboard:"
```

## Deployment Testing

### 1. Deploy via Helmfile (Recommended)
```bash
# Deploy just the dashboards
helmfile -l app=grafana-dashboards sync

# Or deploy all observability
helmfile -l tier=observability sync
```

### 2. Verify ConfigMaps Created
```bash
# List all dashboard ConfigMaps
kubectl get configmap -n observability -l grafana_dashboard=1

# Inspect a specific dashboard
kubectl get configmap grafana-dashboard-gpu-monitoring -n observability -o yaml
```

### 3. Check Grafana Sidecar Logs
```bash
# Watch sidecar discover dashboards
kubectl logs -n observability -l app.kubernetes.io/name=grafana -c grafana-sc-dashboard -f
```

You should see logs like:
```
INFO: Discovered configmap observability/grafana-dashboard-gpu-monitoring
INFO: Reloading dashboards...
```

### 4. Verify in Grafana UI
1. Port-forward to Grafana:
   ```bash
   kubectl port-forward -n observability svc/grafana 3000:80
   ```
2. Open http://localhost:3000
3. Navigate to Dashboards → Browse
4. Your dashboards should appear in the default folder

## Adding New Dashboards

### 1. Export from Grafana
- In Grafana UI: Dashboard Settings → JSON Model → Copy

### 2. Add to Chart
```bash
# Save to file
cat > grafana-dashboards/dashboards/my-new-dashboard.json <<'EOF'
{
  "title": "My New Dashboard",
  "uid": "my-new-dashboard",
  ...
}
EOF
```

### 3. Deploy Update
```bash
helmfile -l app=grafana-dashboards sync
```

The sidecar will automatically detect and load the new dashboard within ~60 seconds.

## Troubleshooting

### Dashboard Not Appearing

1. Check ConfigMap exists:
   ```bash
   kubectl get cm -n observability -l grafana_dashboard=1
   ```

2. Verify label matches Grafana sidecar config:
   ```bash
   # Your grafana.yaml should have:
   # sidecar.dashboards.label: grafana_dashboard
   kubectl get deployment grafana -n observability -o yaml | grep -A 5 LABEL
   ```

3. Check sidecar logs for errors:
   ```bash
   kubectl logs -n observability deployment/grafana -c grafana-sc-dashboard --tail=50
   ```

### Invalid JSON

If a dashboard has syntax errors:
```bash
# Validate JSON
cat grafana-dashboards/dashboards/my-dashboard.json | jq .
```

### Permission Issues

The sidecar searches `ALL` namespaces. If it's not finding ConfigMaps:
```bash
# Check sidecar RBAC
kubectl auth can-i list configmaps --as=system:serviceaccount:observability:grafana -n observability
```
