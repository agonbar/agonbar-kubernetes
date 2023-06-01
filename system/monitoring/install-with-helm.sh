# /bin/bash
# https://www.opsramp.com/guides/prometheus-monitoring/prometheus-operator/
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install --namespace monitoring -f values.yaml prom-operator-01 prometheus-community/kube-prometheus-stack
kubectl --namespace monitoring get pods
kubectl --namespace monitoring get svc


helm upgrade --namespace monitoring -f values.yaml prom-operator-01 prometheus-community/kube-prometheus-stack