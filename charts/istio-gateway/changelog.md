## 0.1.3

- Make OpenStack Service annotations opt-in through `values-openstack.yaml`.
- Move OpenStack HA edge placement into the same opt-in overlay so an empty
  affinity map is actually empty after Helm values merging.
- Keep the base annotation map empty so non-OpenStack consumers can select an
  upstream `loadBalancerClass` without inheriting Octavia metadata.

## 0.1.2

- Allow consumers to disable the starter Gateway and self-signed CA resources.
