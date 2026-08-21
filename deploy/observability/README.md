# Capacity and reconciliation alerts

Render the template only with thresholds approved by the retained staging load/soak report. The
metric producer or platform adapter must preserve the exact metric names and replica aggregation
semantics in this file. Loading the rule without a live scrape target is not evidence.

Every critical alert is an unconditional canary abort. Route it to the recorded on-call owner and
rollback commander, retain the rule revision and Alertmanager delivery test, and verify queue and
lag alerts clear only after the durable facts drain. Historical reconciliation is read-only; an
alert never authorizes a backfill or deletion.
