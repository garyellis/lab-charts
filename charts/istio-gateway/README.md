# istio-gateway

Wrapper around the upstream Istio gateway chart.

`values.yaml` is provider-neutral. OpenStack deployments add
`values-openstack.yaml` to retain the Octavia annotations, two replicas, and
edge-node placement used before 0.1.3:

```shell
helm upgrade --install istio-gateway . \
  --namespace istio-ingress \
  --values values.yaml \
  --values values-openstack.yaml
```

Other providers set the upstream gateway Service values directly. For example,
a Cilium Node IPAM LoadBalancer can use:

```yaml
gateway:
  replicaCount: 1
  affinity: {}
  tolerations: []
  service:
    type: LoadBalancer
    annotations: {}
    loadBalancerClass: io.cilium/node
```
