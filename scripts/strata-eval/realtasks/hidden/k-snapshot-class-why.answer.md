Reference answer (self-test only).

backup.yaml sets a global SNAPSHOT_CLASS="truenas-iscsi-ssd" and uses it for every
VolumeSnapshot and for the temp PVC. The PVCs that live on truenas-iscsi-ssd-lan get a
snapshot from the other driver: it reports readyToUse and the job's own temp PVC binds
(both drivers reach the same pool), but a PVC of class truenas-iscsi-ssd-lan will not take
a dataSource made by a different driver, so a real restore stays "not bound".
