#!/bin/sh
# Regression test for the snapshot retention path in backup.yaml.
#
# Bug it guards against: backup_pvc() used to `return` early when a snapshot
# never became ready or the temp PVC never bound, skipping retention. A PVC
# that failed every night (e.g. it had been deleted) then leaked snapshots
# forever -- transmission-config accumulated 15 for a PVC gone for months.
#
# Usage: ./test-retention.sh   (exits non-zero on failure, no cluster needed)
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# extract the backup.sh block from the ConfigMap (indented 4 under "backup.sh: |")
awk '
  /^  backup\.sh: \|/ { grab=1; next }
  grab && /^  [a-zA-Z0-9_.-]+: / { grab=0 }
  grab { sub(/^    /, ""); print }
' "$DIR/backup.yaml" > "$TMP/full.sh"
[ -s "$TMP/full.sh" ] || { echo "FAIL: could not extract backup.sh"; exit 1; }
sh -n "$TMP/full.sh" || { echo "FAIL: backup.sh has a syntax error"; exit 1; }

# keep only the config vars and function defs, drop the backup_pvc call list
awk '/^echo "={10,}"/{exit} {print}' "$TMP/full.sh" > "$TMP/lib.sh"

# stub kubectl: log every call, fail wherever FAIL_AT says, and report 9 snapshots
mkdir -p "$TMP/bin"
cat > "$TMP/bin/kubectl" <<'STUB'
#!/bin/sh
echo "kubectl $*" >> "$CALLS"
case "$FAIL_AT" in
  snapshot) case "$*" in *"wait volumesnapshot"*) exit 1;; esac ;;
  pvc)      case "$*" in *"wait pvc"*) exit 1;; esac ;;
  rsync)    case "$*" in *"wait job"*) exit 1;; esac ;;
esac
case "$*" in *"get volumesnapshots"*) echo "s1 s2 s3 s4 s5 s6 s7 s8 s9";; esac
exit 0
STUB
chmod +x "$TMP/bin/kubectl"

RC=0
for CASE in snapshot pvc rsync none; do
  CALLS="$TMP/calls.log"; : > "$CALLS"
  OUT=$(PATH="$TMP/bin:$PATH" FAIL_AT="$CASE" CALLS="$CALLS" \
        sh -c ". $TMP/lib.sh; backup_pvc testns testpvc" 2>&1) || true
  # retention keeps the last 7 of 9, so s1 and s2 must be deleted
  PRUNED=$(grep -c "delete volumesnapshot s[12] " "$CALLS" || true)
  if echo "$OUT" | grep -q "Enforcing snapshot retention" && [ "$PRUNED" -eq 2 ]; then
    echo "ok   - retention runs (failure point: $CASE)"
  else
    echo "FAIL - retention skipped (failure point: $CASE, pruned=$PRUNED)"
    RC=1
  fi
done
[ $RC -eq 0 ] && echo "PASS: retention runs on every exit path"
exit $RC
