## 0.1.0

- Vendor aquasecurity/trivy-operator chart 0.36.0 (Trivy Operator 0.34.0,
  Trivy 0.74.0).
- Vulnerability scanning in client/server mode (built-in Trivy server), at
  most two concurrent scan jobs with a 30m timeout; config audit and RBAC
  assessment in-process. Exposed-secret, infra-assessment, compliance and
  SBOM generation off.
- Per-image severity metrics plus per-CVE metrics bounded to High/Critical by
  the ServiceMonitor (`release: alloy`).
- Chart-owned helm test proving a Pod's image gets a VulnerabilityReport and
  appears in the operator's metrics.
