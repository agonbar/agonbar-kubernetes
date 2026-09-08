#!/usr/bin/env bash
# Sample pg_stat_activity on the nas02 Postgres and report the high-water mark
# per role, so "is max_connections big enough" is answered with a measurement
# instead of a guess.
#
# Postgres keeps no peak-connection counter, so the only way to size the limit
# is to sample. Run it over a busy window (an arr mass-search, an import burst).
#
# The instance is LAN-only: the sampling pod must land on a node with wired-LAN
# access to 192.168.0.29. orange-pi5 can, ovh02 cannot.
#
#   ./nas02-pg-connection-watermark.sh              # 5 min at 5s
#   DURATION=3600 INTERVAL=15 ./nas02-pg-connection-watermark.sh
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-lamg}"
NS="${NS:-piracy}"
NODE="${NODE:-orange-pi5}"
PGHOST="${PGHOST:-192.168.0.29}"
PGUSER="${PGUSER:-piracy_admin}"
DURATION="${DURATION:-300}"
INTERVAL="${INTERVAL:-5}"
OUT="${OUT:-/tmp/nas02-pg-samples-$(date +%Y%m%d-%H%M%S).tsv}"

if [[ -z "${PGPASSWORD:-}" ]]; then
    echo "PGPASSWORD is not set. Take the piracy_admin password from the vault" >&2
    echo "note architecture/piracy-postgres-credentials.md and export it." >&2
    exit 2
fi

# One long-lived pod for the whole run: a pod per sample costs more scheduling
# time than the sample interval.
POD="pgwatermark-$RANDOM"
cleanup() { kubectl --context "$KUBE_CONTEXT" -n "$NS" delete pod "$POD" --now >/dev/null 2>&1 || true; }
trap cleanup EXIT

kubectl --context "$KUBE_CONTEXT" -n "$NS" run "$POD" \
    --image=postgres:17-alpine --restart=Never \
    --overrides="{\"spec\":{\"nodeSelector\":{\"kubernetes.io/hostname\":\"$NODE\"}}}" \
    --env="PGPASSWORD=$PGPASSWORD" \
    --command -- sleep "$((DURATION + 120))" >/dev/null

kubectl --context "$KUBE_CONTEXT" -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=120s >/dev/null

limit=$(kubectl --context "$KUBE_CONTEXT" -n "$NS" exec "$POD" -- \
    psql -h "$PGHOST" -U "$PGUSER" -d postgres -At \
    -c "select setting from pg_settings where name='max_connections'")
echo "max_connections=$limit  sampling ${DURATION}s every ${INTERVAL}s -> $OUT"

deadline=$(( $(date +%s) + DURATION ))
while [[ $(date +%s) -lt $deadline ]]; do
    ts=$(date +%s)
    kubectl --context "$KUBE_CONTEXT" -n "$NS" exec "$POD" -- \
        psql -h "$PGHOST" -U "$PGUSER" -d postgres -At -F$'\t' \
        -c "select '$ts', usename, count(*) from pg_stat_activity
            where backend_type = 'client backend' group by 1, 2" >> "$OUT" 2>/dev/null || true
    sleep "$INTERVAL"
done

echo
echo "== peak concurrent connections per role =="
awk -F'\t' '{ if ($3 > peak[$2]) peak[$2] = $3 }
     END { for (r in peak) printf "  %-16s %s\n", r, peak[r] }' "$OUT" | sort -k2 -rn

echo
echo "== peak total across all roles (worst single sample) =="
awk -F'\t' '{ total[$1] += $3 }
     END { for (t in total) if (total[t] > max) { max = total[t]; at = t } }
     END { printf "  %s of %s at %s\n", max, "'"$limit"'", strftime("%H:%M:%S", at) }' "$OUT"

echo
echo "Samples kept in $OUT"
