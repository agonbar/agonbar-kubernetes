#!/bin/sh
kubectl apply -f deployments/agonbar
kubectl apply -f deployments/aya
kubectl apply -f deployments/immich
kubectl apply -f deployments/lamg
kubectl apply -f deployments/matrix
kubectl apply -f deployments/piracy
kubectl apply -f deployments/yavoo

# kubectl apply -k system/cert-manager --server-side --force-conflicts
# kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.27.1/controller.yaml