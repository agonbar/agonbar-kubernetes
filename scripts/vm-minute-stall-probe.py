#!/usr/bin/env python3
# Times a fixed CPU workload in wall-clock terms, bucketed by second of the
# minute, to tell hypervisor CPU theft from in-guest contention on a VPS.
# Same work, longer wall time = the vCPU was taken away. ovh02 reports 0 %
# steal, yet on 2026-10-20 this showed the work 2.7x slower at :02 and freezes
# up to 213 ms at :00-:03 of every minute, with 12 vCPUs mostly idle.
#
# Usage: scp scripts/vm-minute-stall-probe.py ovh02:/tmp/ && ssh ovh02 'nice -n -5 python3 /tmp/vm-minute-stall-probe.py; rm /tmp/vm-minute-stall-probe.py'
import time, collections, statistics
def work():
    x = 0
    for i in range(60000): x += i * i
    return x
for _ in range(20): work()
by = collections.defaultdict(list)
end = time.time() + 185
while time.time() < end:
    t0 = time.perf_counter(); work(); dt = (time.perf_counter() - t0) * 1000
    by[int(time.time()) % 60].append(dt); time.sleep(0.03)
allv = [v for l in by.values() for v in l]; med = statistics.median(allv)
print('median %.2f ms per unit of work' % med)
for s in sorted(by):
    l = by[s]; slow = sum(1 for v in l if v > 1.5 * med)
    if s < 8 or s in (28, 29, 30, 31) or slow: print('  :%02d  median %.2f  max %6.2f  slow(>1.5x) %d/%d' % (s, statistics.median(l), max(l), slow, len(l)))
