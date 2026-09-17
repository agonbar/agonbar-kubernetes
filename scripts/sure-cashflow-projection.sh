#!/usr/bin/env bash
# Project Sure's liquid balance to a date, to answer "will I have enough on X?".
#
# Adds up: today's balance on non-liability accounts, the transactions already
# dated in the future (a tax bill, a known payment), the active recurring
# transactions that fall in the window, and the average discretionary spend of
# the last complete months (total spend minus what the recurring ones explain).
# Transfers between own accounts are excluded, so moving money to savings does
# not read as spending.
#
#   scripts/sure-cashflow-projection.sh 2026-11-05
set -euo pipefail
TARGET="${1:?usage: sure-cashflow-projection.sh <YYYY-MM-DD>}"

read -r -d '' RUBY <<'EOF' || true
target = Date.parse(ENV["TARGET"])
fam = Family.first
today = Date.current
days = (target - today).to_i
abort "la fecha ya paso" if days <= 0

# Top-ups between own accounts that Sure did not pair as transfers (the other
# leg is outside the 90 days the bank hands over) would read as income.
NOISE = /top-up|traspaso|transferencia interna/i

def month_flows(fam, from, to)
  rows = fam.transactions.joins(:entry).where.not(kind: Transaction::TRANSFER_KINDS)
            .where("entries.date >= ? AND entries.date <= ?", from, to)
            .pluck(Arel.sql("entries.amount"), Arel.sql("entries.name"))
  inc = rows.select { |a, n| a.to_f.negative? && n.to_s !~ NOISE }.sum { |a, _| -a.to_f }
  exp = rows.select { |a, _| a.to_f.positive? }.sum { |a, _| a.to_f }
  [ inc, exp ]
end

months = (1..3).map { |i| (today << i).beginning_of_month }
flows = months.map { |m| month_flows(fam, m, m.end_of_month) }
median = ->(xs) { s = xs.sort; s.size.odd? ? s[s.size / 2] : (s[s.size / 2 - 1] + s[s.size / 2]) / 2.0 }
inc_m = median.call(flows.map(&:first))
exp_m = median.call(flows.map(&:last))

liquid = fam.accounts.reject { |a| a.classification == "liability" }
savings, current = liquid.partition { |a| a.name =~ /remunerada|ahorro/i }
cash = current.sum { |a| a.balance.to_f }

puts "hoy #{today} -> #{target} (#{days} dias)"
current.each { |a| puts format("  %-20s %10.2f", a.name, a.balance.to_f) }
puts format("  %-20s %10.2f", "corriente", cash)
savings.each { |a| puts format("  %-20s %10.2f  (ahorro)", a.name, a.balance.to_f) }
puts "
meses completos:"
months.zip(flows).each { |m, (i, e)| puts format("  %s ingresos=%9.2f gastos=%9.2f neto=%9.2f", m.strftime("%Y-%m"), i, e, i - e) }
puts format("  mediana        ingresos=%9.2f gastos=%9.2f neto=%9.2f/mes", inc_m, exp_m, inc_m - exp_m)

known = 0.0
puts "
ya apuntado entre hoy y la fecha:"
fam.transactions.joins(:entry).where.not(kind: Transaction::TRANSFER_KINDS)
   .where("entries.date > ? AND entries.date <= ?", today, target)
   .order(Arel.sql("entries.date")).each do |t|
  known += t.entry.amount.to_f
  puts format("  %s %10.2f  %s", t.entry.date, -t.entry.amount.to_f, t.entry.name.to_s[0, 34])
end
puts "  (nada)" if known.zero?

run_rate = (inc_m - exp_m) * days / 30.0
projected = cash + run_rate - known
puts format("
PROYECCION corriente: %.2f %+.2f (ritmo %.2f/mes) %+.2f (apuntado) = %.2f",
            cash, run_rate, inc_m - exp_m, -known, projected)
if projected.negative?
  puts format("FALTAN %.2f -> hay %.2f en ahorro", -projected, savings.sum { |a| a.balance.to_f })
else
  puts format("LLEGAS con %.2f de margen", projected)
end
EOF

kubectl --context lamg -n sure exec -i deploy/sure-web -c web -- \
  env TARGET="$TARGET" bin/rails runner "$RUBY" 2>&1 | grep -v -E "SKYLIGHT|OmniAuth|SSO providers|ProviderLoader"
