#!/usr/bin/env bash
# Why isn't Tdarr transcoding? One-shot answer instead of five kubectl execs.
#
#   ./tdarr-status.sh
#
# Checks, in the order they have actually bitten:
#   - /app/server read-only (iSCSI ext4 journal abort; fix: repair-iscsi-volume.sh)
#   - prioritiseHealthChecks: a GLOBAL gate, not an ordering. While even one health
#     check is queued the server hands out zero transcodes, even to nodes that have
#     no health-check slot at all. Seen 2026-09-18: 1460 queued, 3 nodes idle.
#   - nodes with a 0 worker limit (Tdarr gives every new or renamed node 0 of
#     everything; kubernetes-00/10 lost their health-check slot that way)
#   - tdarr-node pods the DaemonSet wants but the server doesn't see
set -euo pipefail
K="kubectl --context ${CTX:-lamg} -n piracy"
POD=$($K get pods -l app=tdarr -o jsonpath='{.items[0].metadata.name}')

$K exec "$POD" -c tdarr -- sh -c 'touch /app/server/.rw 2>/dev/null && rm /app/server/.rw && echo "server fs      RW" || echo "server fs      READ-ONLY  <-- scripts/repair-iscsi-volume.sh piracy deployment/tdarr-deployment tdarr-server"'

$K exec -i "$POD" -c tdarr -- node - <<'JS'
const H = { 'Content-Type': 'application/json', 'x-api-key': process.env.apiKey };
const api = (path, body) => fetch('http://localhost:8265/api/v2/' + path,
  body ? { method: 'POST', headers: H, body: JSON.stringify({ data: body }) } : { headers: H }).then(r => r.json());
const count = t => api('client/status-tables',
  { start: 0, pageSize: 1, filters: [], sorts: [], opts: { table: t } }).then(r => r.totalCount);
(async () => {
  const [tq, hq, g, nodes, libs] = await Promise.all([
    count('table1'), count('table4'),
    api('cruddb', { collection: 'SettingsGlobalJSONDB', mode: 'getById', docID: 'globalsettings' }),
    api('get-nodes'),
    api('cruddb', { collection: 'LibrarySettingsJSONDB', mode: 'getAll' }),
  ]);
  const now = new Date();
  const slot = ['Sun','Mon','Tue','Wed','Thur','Fri','Sat'][now.getDay()] + ':' +
    String(now.getHours()).padStart(2, '0') + '-' + String((now.getHours() + 1) % 24).padStart(2, '0');
  console.log('queues         transcode=' + tq + ' healthcheck=' + hq);
  console.log('gate           prioritiseHealthChecks=' + g.prioritiseHealthChecks +
    (g.prioritiseHealthChecks && hq > 0 ? '  <-- BLOCKING all transcodes until healthcheck=0' : ''));
  console.log('paused         pauseAllNodes=' + g.pauseAllNodes + ' ignoreSchedules=' + g.ignoreSchedules);
  for (const l of libs) {
    const on = (l.schedule || []).find(s => s._id === slot);
    console.log('library        ' + l._id + ' slot ' + slot + ' ' + (on && on.checked ? 'IN schedule' : 'OUT of schedule'));
  }
  let idle = 0;
  for (const n of Object.values(nodes)) {
    const w = Object.values(n.workers || {}).map(x => x.workerType);
    const L = n.workerLimits;
    const tcFree = L.transcodecpu + L.transcodegpu - w.filter(t => t.startsWith('transcode')).length;
    if (tcFree > 0) idle += tcFree;
    const zero = Object.entries(L).filter(([k, v]) => k.endsWith('cpu') && v === 0).map(([k]) => k);
    console.log('node           ' + n.nodeName.padEnd(18) + ' tc=' + L.transcodecpu + ' hc=' + L.healthcheckcpu +
      ' paused=' + n.nodePaused + ' running=[' + (w.join(',') || '-') + ']' +
      (zero.length ? '  <-- ' + zero.join(',') + '=0' : ''));
  }
  if (tq > 0 && idle > 0) console.log('VERDICT        ' + idle + ' transcode slot(s) idle with ' + tq + ' queued');
  else console.log('VERDICT        ok');
})().catch(e => { console.error(e); process.exit(1); });
JS

DESIRED=$($K get ds tdarr-node -o jsonpath='{.status.desiredNumberScheduled}')
READY=$($K get ds tdarr-node -o jsonpath='{.status.numberReady}')
echo "daemonset      tdarr-node ready=$READY desired=$DESIRED"
kubectl --context "${CTX:-lamg}" get nodes -l workload=media --no-headers | awk '$2 !~ /^Ready/ {print "media node     " $1 " " $2 "  <-- no tdarr-node here"}'
# Pressure taints evict the pod while the node still reports Ready (work-vm-00 disk-pressure, 2026-09-25).
kubectl --context "${CTX:-lamg}" get nodes -l workload=media -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.taints[*].key}{"\n"}{end}' \
  | awk '{for (i = 2; i <= NF; i++) if ($i ~ /pressure$/) print "media node     " $1 " " $i "  <-- tdarr-node evicted"}'
