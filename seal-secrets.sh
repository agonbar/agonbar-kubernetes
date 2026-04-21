#!/bin/sh
kubeseal --context lamg -f ./system/argocd/secrets.yaml -o yaml --scope cluster-wide > ./system/argocd/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/agonbar/secrets.yaml -o yaml --scope cluster-wide > ./deployments/agonbar/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/aya/secrets.yaml -o yaml --scope cluster-wide > ./deployments/aya/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/dawarich/secrets.yaml -o yaml --scope cluster-wide > ./deployments/dawarich/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/games/secrets.yaml -o yaml --scope cluster-wide > ./deployments/games/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/immich/secrets.yaml -o yaml --scope cluster-wide > ./deployments/immich/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/lamg/secrets.yaml -o yaml --scope cluster-wide > ./deployments/lamg/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/yavoo/secrets.yaml -o yaml --scope cluster-wide > ./deployments/yavoo/sealedsecrets.yaml
kubeseal --context lamg -f ./deployments/piracy/secrets.yaml -o yaml --scope cluster-wide > ./deployments/piracy/sealedsecrets.yaml
kubeseal --context lamg -f ./system/democratic-csi/secrets.yaml -o yaml --scope cluster-wide > ./system/democratic-csi/sealedsecrets.yaml