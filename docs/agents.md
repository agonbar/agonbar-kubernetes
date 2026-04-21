# Agent Rules for This Repository

Rules and conventions that AI agents must follow when working with this Kubernetes GitOps repository.

## Deployment Strategy: Recreate for PVC-bound Deployments

**CRITICAL: Any Deployment that uses a PersistentVolumeClaim (PVC) MUST use `strategy: type: Recreate`.**

### Why

All iSCSI/block storage PVCs in this cluster use `ReadWriteOnce` (RWO) access mode (`truenas-iscsi-ssd` storage class). RWO volumes can only be mounted on one node at a time.

With the default `RollingUpdate` strategy, Kubernetes tries to start the new pod *before* killing the old one. If the new pod lands on a different node, it cannot mount the volume because the old pod still holds it. This causes a **Multi-Attach error** deadlock — the new pod waits for the volume, the old pod waits to be replaced, and the deployment is stuck forever.

`Recreate` strategy stops the old pod first, releases the volume, then starts the new pod. This guarantees the volume is always available.

### Rule

When creating or modifying a Deployment manifest:

1. Check if the Deployment mounts any PVC volumes
2. If yes, add `strategy: type: Recreate` under `spec:`
3. If no PVC is used, `RollingUpdate` (the default) is fine

### Example

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  strategy:
    type: Recreate       # <-- REQUIRED when using PVCs
  replicas: 1
  selector:
    matchLabels:
      app: my-app
  template:
    spec:
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: my-app-data    # <-- PVC = must use Recreate
```

### What happens if you forget

The deployment will work fine *until* a pod needs to reschedule to a different node (node failure, drain, resource pressure). Then it deadlocks with Multi-Attach errors and requires manual intervention (force-deleting pods and clearing VolumeAttachments).
