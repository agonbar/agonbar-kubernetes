#!/usr/bin/env bash
# Import an Abanca CSV export (web banking "Movimientos" download) into the
# "Abanca" account in Sure.
#
# Overlapping exports are safe: Sure's TransactionImport matches rows against
# existing entries by date + amount + currency and updates them instead of
# inserting. The date column is "Fecha ctble" on purpose, because it is the
# booking date Enable Banking uses too, so a later bank sync claims these rows
# rather than duplicating them.
#
# Publishes only if every row validates, then re-syncs the account on its own:
# the Abanca Enable Banking item has been failing, and a family sync aborts
# before it reaches this account's balance recalculation.
#
#   scripts/sure-import-abanca-csv.sh ~/Downloads/<export>.csv
set -euo pipefail

CSV="${1:?usage: sure-import-abanca-csv.sh <abanca-export.csv>}"
[[ -r "$CSV" ]] || { echo "cannot read $CSV" >&2; exit 1; }

read -r -d '' RUBY <<'EOF' || true
csv = STDIN.read
fam = Family.first
acct = fam.accounts.find_by!(name: "Abanca")
before = acct.entries.where(entryable_type: "Transaction").count
imp = fam.imports.create!(type: "TransactionImport", account: acct, raw_file_str: csv, col_sep: ";",
  date_col_label: "Fecha ctble", amount_col_label: "Importe", name_col_label: "Concepto",
  notes_col_label: "Concepto ampliado", date_format: "%d-%m-%Y", number_format: "1.234,56",
  signage_convention: "inflows_positive", amount_type_strategy: "signed_amount")
imp.generate_rows_from_csv
invalid = imp.reload.rows.reject(&:valid?)
puts "rows=#{imp.rows.count} invalid=#{invalid.size}"
if invalid.any?
  invalid.first(5).each { |r| puts "  #{r.date} #{r.amount} #{r.errors.full_messages.join(', ')}" }
  imp.destroy!
  abort "not published"
end
imp.publish
acct.sync_later
t0 = Time.now
sleep 3
sleep 3 while acct.syncs.where(status: %w[pending syncing]).exists? && Time.now - t0 < 240
tx = acct.reload.entries.where(entryable_type: "Transaction")
puts "import=#{imp.reload.status} new_transactions=#{tx.count - before} total=#{tx.count} " \
     "range=#{tx.minimum(:date)}..#{tx.maximum(:date)} balance=#{acct.balance}"
EOF

kubectl --context lamg -n sure exec -i deploy/sure-web -c web -- \
  bin/rails runner "$RUBY" < "$CSV" 2>&1 | grep -v -E "SKYLIGHT|OmniAuth|SSO providers|Enqueued|Sidekiq"
