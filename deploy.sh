#!/bin/sh
kubectl apply -f deployments/agonbar
kubectl apply -f deployments/aya
kubectl apply -f deployments/immich
kubectl apply -f deployments/lamg
kubectl apply -f deployments/matrix
kubectl apply -f deployments/piracy
kubectl apply -f deployments/yavoo

# kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.0/cert-manager.yaml
# kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.27.1/controller.yaml