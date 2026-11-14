#!/usr/bin/env bash
# Construye ghcr.io/agonbar/strata:<commit> con BuildKit DENTRO de kubernetes-10 y la sube.
# El Dockerfile de Strata compila el motor con código nativo para la CPU que construye
# (Zen 4 con AVX-512 en ese nodo), así que no vale construirla en otra máquina.
# Contexto: el clon del nodo, /var/lib/strata/strata, en el commit que se quiera.
#
# Uso: scripts/strata-build-image.sh [tag]   (por defecto, el commit corto del clon)
# Necesita ~/.docker/config.json con un login a ghcr.io con write:packages.
# Gotcha: FROM nvidia/cuda:... sin registry falla con resolución de nombres cortos;
# el initContainer lo reescribe a docker.io/nvidia/cuda:... (Dockerfile 0.1.x) y los
# build-arg CUDA_IMAGE/RUNTIME_BASE hacen lo mismo con el de la v0.2.x (FROM ${CUDA_IMAGE}).
# Con Strata sirviendo, al nodo le quedan unos 7 GiB asignables y ~17 GiB libres: la Job pide
# 4Gi con tope de 12Gi y compila con JOBS hilos (4 por defecto), no con nproc (16), para que un
# nvcc de más acabe con la Job y no con el pod de Strata.
set -euo pipefail
CTX=lamg NS=strata-build NODE=kubernetes-10 SRC=/var/lib/strata/strata JOBS="${JOBS:-4}"
TAG="${1:-$(ssh "agonbar@$NODE" "git -C $SRC rev-parse --short=8 HEAD")}"
COMMIT=$(ssh "agonbar@$NODE" "git -C $SRC rev-parse HEAD")   # package_terminal.py de la v0.2.x exige los 40 dígitos
IMAGE="ghcr.io/agonbar/strata:$TAG"
k() { kubectl --context "$CTX" "$@"; }

k create namespace "$NS" --dry-run=client -o yaml | k apply -f -
# Solo la entrada de ghcr.io del config local, sin pasar por disco ni pantalla.
jq '{auths: {"ghcr.io": .auths["ghcr.io"]}}' ~/.docker/config.json |
  k -n "$NS" create secret generic ghcr-push --from-file=config.json=/dev/stdin \
    --dry-run=client -o yaml | k apply -f -
k -n "$NS" delete job strata-build --ignore-not-found --wait

k apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: strata-build
  namespace: $NS
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: $NODE
      initContainers:
        - name: context
          image: busybox:1.37
          command: ["sh", "-c", "cp -a /src/. /ctx/ && sed -i -e 's|^FROM nvidia/cuda:|FROM docker.io/nvidia/cuda:|' -e 's|-j \"\$(nproc)\"|-j $JOBS|' /ctx/Dockerfile && grep -E '^FROM|-j ' /ctx/Dockerfile"]
          volumeMounts:
            - {name: src, mountPath: /src, readOnly: true}
            - {name: ctx, mountPath: /ctx}
      containers:
        - name: buildkit
          image: moby/buildkit:v0.34.0
          command: ["buildctl-daemonless.sh"]
          args:
            - build
            - --frontend=dockerfile.v0
            - --local=context=/ctx
            - --local=dockerfile=/ctx
            - --opt=build-arg:CUDA_ARCHITECTURES=86
            - --opt=build-arg:CUDA_IMAGE=docker.io/nvidia/cuda:13.0.0-devel-ubuntu24.04
            - --opt=build-arg:RUNTIME_BASE=docker.io/library/ubuntu:24.04
            - --opt=build-arg:STRATA_COMMIT=$COMMIT
            - --output=type=image,name=$IMAGE,push=true
          env:
            - {name: DOCKER_CONFIG, value: /docker}
          securityContext:
            privileged: true
          resources:
            requests: {cpu: "8", memory: 4Gi}
            limits: {memory: 12Gi}
          volumeMounts:
            - {name: ctx, mountPath: /ctx}
            - {name: buildkit, mountPath: /var/lib/buildkit}
            - {name: docker, mountPath: /docker, readOnly: true}
      volumes:
        - {name: src, hostPath: {path: $SRC, type: Directory}}
        - {name: ctx, emptyDir: {}}
        - {name: buildkit, emptyDir: {}}
        - {name: docker, secret: {secretName: ghcr-push}}
EOF
echo "Job lanzado: $IMAGE"
echo "Seguir:  kubectl --context $CTX -n $NS logs -f job/strata-build | grep -E '^#[0-9]+ (DONE|ERROR)|error|pushing'"
echo "Limpiar: kubectl --context $CTX delete namespace $NS"
