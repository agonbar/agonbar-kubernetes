#!/bin/sh
# kubeseal --recovery-unseal only processes the first document in a multi-document YAML.
# This function splits the file, unseals each document individually, and concatenates results.
unseal() {
    input="$1"
    output="$2"
    tmpdir=$(mktemp -d)
    csplit --quiet --prefix="${tmpdir}/doc_" --suffix-format='%03d.yaml' "$input" '/^---$/' '{*}' 2>/dev/null
    > "$output"
    for f in "${tmpdir}"/doc_*.yaml; do
        # skip empty / separator-only documents
        if grep -q 'kind: SealedSecret' "$f" 2>/dev/null; then
            kubeseal --context lamg -f "$f" --recovery-unseal --recovery-private-key sealed-secrets-key.yaml -o yaml >> "$output"
        fi
    done
    rm -rf "$tmpdir"
}

if [ ! -f sealed-secrets-key.yaml ]; then
    kubectl get secret -n kube-system --context lamg -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key.yaml
fi

unseal ./deployments/agonbar/sealedsecrets.yaml  ./deployments/agonbar/secrets.yaml
unseal ./deployments/aya/sealedsecrets.yaml       ./deployments/aya/secrets.yaml
unseal ./deployments/dawarich/sealedsecrets.yaml  ./deployments/dawarich/secrets.yaml
unseal ./deployments/games/sealedsecrets.yaml    ./deployments/games/secrets.yaml
unseal ./deployments/immich/sealedsecrets.yaml    ./deployments/immich/secrets.yaml
unseal ./deployments/lamg/sealedsecrets.yaml      ./deployments/lamg/secrets.yaml
unseal ./deployments/yavoo/sealedsecrets.yaml     ./deployments/yavoo/secrets.yaml
unseal ./deployments/piracy/sealedsecrets.yaml    ./deployments/piracy/secrets.yaml
