# Migración de NFS a iSCSI con democratic-csi

## Contexto

Las aplicaciones *arr (Radarr, Sonarr, etc.) usan bases de datos SQLite que se corrompen frecuentemente sobre NFS debido a problemas con file locking. La solución es migrar los volúmenes de `/config` a iSCSI (block storage).

## Tecnologías

- **TrueNAS SCALE** (basado en Linux/Debian)
- **democratic-csi**: Driver CSI para Kubernetes que soporta NFS e iSCSI con TrueNAS
- **iSCSI**: Protocolo de block storage, ideal para bases de datos

## Qué migrar y qué no

| Tipo de volumen | Protocolo | Razón |
|-----------------|-----------|-------|
| `/config` (SQLite, DBs) | **iSCSI** | Evita corrupción de SQLite |
| `/downloads`, `/movies`, `/music` | **NFS** | Media compartida, archivos grandes |

### Apps candidatas para migración a iSCSI:

- [ ] Radarr (`/mnt/SSD/docker/radarr/config`)
- [ ] Sonarr (`/mnt/SSD/docker/sonarr/config`)
- [ ] Lidarr (`/mnt/SSD/docker/lidarr/config`)
- [ ] Prowlarr (`/mnt/SSD/docker/prowlarr/config`)
- [ ] Bazarr (`/mnt/SSD/docker/bazarr/config`)
- [ ] Readarr (si existe)
- [ ] Cualquier otra app con SQLite

### Apps que se beneficiarían también:

- [ ] PostgreSQL (Immich, Dawarich)
- [ ] Redis
- [ ] Paperless

## Preparación en TrueNAS SCALE

### 1. Habilitar y obtener API Key

1. Ir a **System → General → GUI → Settings**
2. Verificar que la API está accesible
3. Crear API Key: Usuario (arriba derecha) → **API Keys** → **Add**
4. Guardar la API Key en un lugar seguro

### 2. Configurar iSCSI

1. Ir a **Shares → iSCSI**
2. Verificar que el servicio está **habilitado**
3. Crear un **Portal**:
   - IP: `192.168.0.29` (IP del TrueNAS)
   - Puerto: `3260`
4. Crear un **Initiator Group**:
   - Puede dejarse abierto o limitar a IPs de los nodos de Kubernetes

### 3. Crear dataset para Kubernetes

Crear la siguiente estructura:

```
/mnt/SSD/kubernetes/
├── iscsi/    # democratic-csi creará zvols aquí
└── nfs/      # Para PVCs NFS dinámicos (opcional)
```

En TrueNAS:
1. **Datasets → Add Dataset**
2. Nombre: `kubernetes`
3. Dentro de `kubernetes`, crear `iscsi` y opcionalmente `nfs`

## Instalación de democratic-csi

### 1. Añadir repositorio Helm

```bash
helm repo add democratic-csi https://democratic-csi.github.io/charts/
helm repo update
```

### 2. Crear namespace

```bash
kubectl create namespace democratic-csi
```

### 3. Crear secret con credenciales

```yaml
# system/democratic-csi/secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: truenas-api
  namespace: democratic-csi
type: Opaque
stringData:
  api-key: "TU_API_KEY_AQUI"
```

### 4. Values para iSCSI

```yaml
# system/democratic-csi/values-iscsi.yaml
csiDriver:
  name: "org.democratic-csi.iscsi"

storageClasses:
  - name: truenas-iscsi
    defaultClass: false
    reclaimPolicy: Retain
    volumeBindingMode: Immediate
    allowVolumeExpansion: true
    parameters:
      fsType: ext4

driver:
  config:
    driver: freenas-api-iscsi
    instance_id: ""
    httpConnection:
      protocol: https
      host: 192.168.0.29
      port: 443
      apiKey: TU_API_KEY_AQUI
      allowInsecure: true  # Si usas certificado self-signed
    zfs:
      datasetParentName: SSD/kubernetes/iscsi
      detachedSnapshotsDatasetParentName: SSD/kubernetes/iscsi-snapshots
      zvolCompression: "lz4"
      zvolDedup: ""
      zvolEnableReservation: false
      zvolBlocksize: "16K"
    iscsi:
      targetPortal: "192.168.0.29:3260"
      targetPortals: []
      interface: ""
      namePrefix: "csi-"
      nameSuffix: ""
      targetGroups:
        - targetGroupPortalGroup: 1
          targetGroupInitiatorGroup: 1
          targetGroupAuthType: None
      extentInsecureTpc: true
      extentXenCompat: false
      extentDisablePhysicalBlocksize: true
      extentBlocksize: 512
      extentRpm: "SSD"
      extentAvailThreshold: 0
```

### 5. Instalar con Helm

```bash
helm upgrade --install democratic-csi-iscsi democratic-csi/democratic-csi \
  --namespace democratic-csi \
  --values system/democratic-csi/values-iscsi.yaml
```

### 6. Verificar instalación

```bash
kubectl get pods -n democratic-csi
kubectl get sc
kubectl get csidrivers
```

## Migración de una app (ejemplo: Radarr)

### Paso 1: Crear PVC

```yaml
# deployments/piracy/radarr-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: radarr-config
  namespace: piracy
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: truenas-iscsi
  resources:
    requests:
      storage: 5Gi  # Ajustar según necesidad
```

```bash
kubectl apply -f deployments/piracy/radarr-pvc.yaml
```

### Paso 2: Parar la app

```bash
kubectl scale deployment radarr-deployment -n piracy --replicas=0
```

### Paso 3: Copiar datos con pod temporal

```yaml
# temp-migration-pod.yaml
apiVersion: v1
kind: Pod
metadata:
  name: radarr-migration
  namespace: piracy
spec:
  containers:
    - name: migration
      image: alpine:latest
      command: ["sleep", "infinity"]
      volumeMounts:
        - name: source-nfs
          mountPath: /source
        - name: dest-iscsi
          mountPath: /dest
  volumes:
    - name: source-nfs
      nfs:
        server: 192.168.0.29
        path: /mnt/SSD/docker/radarr/config
    - name: dest-iscsi
      persistentVolumeClaim:
        claimName: radarr-config
  restartPolicy: Never
```

```bash
kubectl apply -f temp-migration-pod.yaml
kubectl exec -it radarr-migration -n piracy -- sh

# Dentro del pod:
apk add rsync
rsync -av --progress /source/ /dest/
exit

kubectl delete pod radarr-migration -n piracy
```

### Paso 4: Actualizar deployment

Cambiar de volumen NFS inline a PVC:

```yaml
# ANTES (NFS inline):
volumes:
  - name: config
    nfs:
      server: 192.168.0.29
      path: /mnt/SSD/docker/radarr/config

# DESPUÉS (PVC iSCSI):
volumes:
  - name: config
    persistentVolumeClaim:
      claimName: radarr-config
```

### Paso 5: Arrancar la app

```bash
kubectl apply -f deployments/piracy/radarr.yml
kubectl scale deployment radarr-deployment -n piracy --replicas=1
```

### Paso 6: Verificar

```bash
kubectl get pods -n piracy
kubectl logs -f deployment/radarr-deployment -n piracy
```

Acceder a la app y verificar que funciona correctamente.

## Script de migración (opcional)

```bash
#!/bin/bash
# migrate-to-iscsi.sh

APP=$1
NAMESPACE=$2
NFS_PATH=$3
PVC_SIZE=$4

if [ -z "$APP" ] || [ -z "$NAMESPACE" ] || [ -z "$NFS_PATH" ] || [ -z "$PVC_SIZE" ]; then
    echo "Uso: $0 <app> <namespace> <nfs_path> <pvc_size>"
    echo "Ejemplo: $0 radarr piracy /mnt/SSD/docker/radarr/config 5Gi"
    exit 1
fi

echo "=== Migrando $APP a iSCSI ==="

# Crear PVC
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${APP}-config
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: truenas-iscsi
  resources:
    requests:
      storage: ${PVC_SIZE}
EOF

echo "Esperando a que el PVC esté bound..."
kubectl wait --for=condition=Bound pvc/${APP}-config -n ${NAMESPACE} --timeout=60s

# Escalar a 0
echo "Parando ${APP}..."
kubectl scale deployment ${APP}-deployment -n ${NAMESPACE} --replicas=0
sleep 5

# Crear pod de migración
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${APP}-migration
  namespace: ${NAMESPACE}
spec:
  containers:
    - name: migration
      image: alpine:latest
      command: ["sh", "-c", "apk add rsync && rsync -av /source/ /dest/ && echo DONE && sleep 30"]
      volumeMounts:
        - name: source-nfs
          mountPath: /source
        - name: dest-iscsi
          mountPath: /dest
  volumes:
    - name: source-nfs
      nfs:
        server: 192.168.0.29
        path: ${NFS_PATH}
    - name: dest-iscsi
      persistentVolumeClaim:
        claimName: ${APP}-config
  restartPolicy: Never
EOF

echo "Esperando migración..."
kubectl wait --for=condition=Ready pod/${APP}-migration -n ${NAMESPACE} --timeout=120s
kubectl logs -f ${APP}-migration -n ${NAMESPACE}

# Limpiar pod
kubectl delete pod ${APP}-migration -n ${NAMESPACE}

echo ""
echo "=== Migración de datos completada ==="
echo "SIGUIENTE PASO: Actualizar el deployment de ${APP} para usar el PVC ${APP}-config"
echo ""
```

## Rollback

Si algo sale mal:

1. Escalar a 0: `kubectl scale deployment <app>-deployment -n <namespace> --replicas=0`
2. Revertir el deployment al volumen NFS original
3. Aplicar: `kubectl apply -f deployments/...`
4. (Opcional) Borrar el PVC: `kubectl delete pvc <app>-config -n <namespace>`

## Notas importantes

- **Backups**: Las apps *arr tienen backups automáticos en `/config/Backups/`. Verificar que estén antes de migrar.
- **iSCSI es RWO**: Solo un pod puede montar el volumen a la vez (normal para estas apps).
- **Tamaño de PVC**: Empezar con 5Gi para configs pequeños, aumentar si es necesario.
- **Monitorización**: Después de migrar, monitorizar durante unos días para verificar que no hay corrupción.

## Referencias

- [democratic-csi GitHub](https://github.com/democratic-csi/democratic-csi)
- [democratic-csi Helm Chart](https://github.com/democratic-csi/charts)
- [TrueNAS SCALE iSCSI](https://www.truenas.com/docs/scale/scaletutorials/shares/iscsi/)
