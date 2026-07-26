#!/usr/bin/env bash
# Generates an X509 client cert for the external dev `tokkenno`, signed by the
# cluster CA via the k3s CertificateSigningRequest API, and writes a ready-to-
# hand-over kubeconfig. Run against the k3s cluster (context `lamg`) with your
# admin credentials.
#
#   ./make-kubeconfig.sh
#
# Output: ./kubeconfig-tokkenno.yaml  (give this file to the dev)
# The private key never leaves this machine except inside that kubeconfig.
set -euo pipefail

USER="tokkenno"
GROUP="tenant-tokkenno"
NS="tokkenno"
CONTEXT="lamg"
SERVER="https://80.241.214.254:6443"
EXPIRATION_SECONDS="31536000"   # 1 year
WORKDIR="$(mktemp -d)"
OUT="$(dirname "$0")/kubeconfig-${USER}.yaml"

trap 'rm -rf "$WORKDIR"' EXIT

echo ">> generating key + CSR for CN=${USER}, O=${GROUP}"
openssl genrsa -out "$WORKDIR/${USER}.key" 2048 2>/dev/null
openssl req -new -key "$WORKDIR/${USER}.key" \
  -subj "/CN=${USER}/O=${GROUP}" -out "$WORKDIR/${USER}.csr"

CSR_B64="$(base64 -w0 < "$WORKDIR/${USER}.csr")"

echo ">> submitting CertificateSigningRequest"
kubectl --context "$CONTEXT" apply -f - <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: ${USER}
spec:
  request: ${CSR_B64}
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: ${EXPIRATION_SECONDS}
  usages:
    - client auth
EOF

echo ">> approving CSR"
kubectl --context "$CONTEXT" certificate approve "$USER"

echo ">> waiting for signed certificate"
for _ in $(seq 1 30); do
  CERT="$(kubectl --context "$CONTEXT" get csr "$USER" -o jsonpath='{.status.certificate}' 2>/dev/null || true)"
  [ -n "$CERT" ] && break
  sleep 1
done
[ -n "$CERT" ] || { echo "!! certificate not issued"; exit 1; }
echo "$CERT" | base64 -d > "$WORKDIR/${USER}.crt"

# Clean up the CSR object (cert is already extracted).
kubectl --context "$CONTEXT" delete csr "$USER" >/dev/null 2>&1 || true

echo ">> extracting cluster CA"
CA_B64="$(kubectl --context "$CONTEXT" config view --raw --minify \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"

echo ">> writing $OUT"
kubectl config --kubeconfig "$OUT" set-cluster k3s-tokkenno \
  --server="$SERVER" >/dev/null
# inject CA (embedded) directly
python3 - "$OUT" "$CA_B64" <<'PY' 2>/dev/null || \
  kubectl config --kubeconfig "$OUT" set-cluster k3s-tokkenno \
    --certificate-authority=<(echo "$CA_B64" | base64 -d) --embed-certs=true >/dev/null
import sys, yaml
path, ca = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(path))
d['clusters'][0]['cluster']['certificate-authority-data'] = ca
yaml.safe_dump(d, open(path, 'w'))
PY

kubectl config --kubeconfig "$OUT" set-credentials "$USER" \
  --client-certificate="$WORKDIR/${USER}.crt" \
  --client-key="$WORKDIR/${USER}.key" --embed-certs=true >/dev/null
kubectl config --kubeconfig "$OUT" set-context "$USER" \
  --cluster=k3s-tokkenno --user="$USER" --namespace="$NS" >/dev/null
kubectl config --kubeconfig "$OUT" use-context "$USER" >/dev/null

echo
echo "Done. Hand over: $OUT"
echo "Test as the dev:  kubectl --kubeconfig $OUT get pods"
echo "Should be denied: kubectl --kubeconfig $OUT get ns   (list)  /  get nodes"
