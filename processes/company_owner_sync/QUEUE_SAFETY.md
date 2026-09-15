# Sequential founder-package synchronization

Workflows 31 and 31A share `founder-package-owner-writes`, with
`queue: max` and `cancel-in-progress: false`. GitHub allows 100 pending
runs in this group. Overflow, manual cancellation and timeouts are not recovered
automatically. This is a bounded execution queue, not a durable event journal.

The source owner is refreshed after the CRM scan and checked before each write.
Target owners changed since the plan are not overwritten. Each write is read
back; a failure stops remaining writes for that package, reports errors and exits
nonzero. A fresh rerun skips already-correct records. No automatic rollback is
attempted because it could overwrite a subsequent user change.

## Deployment and acceptance

1. Do not overlap deployment with an old running transfer. Wait for old runs;
   already-queued runs may still reference the old workflow/code.
2. First run 31A manually in dry_run on the new revision for a known contact.
3. On approved test records, submit three different contacts simultaneously;
   confirm one running transfer and the others pending, with no replacement.
4. Apply once and inspect the report and CRM. Repeat: no additional writes.
5. Change the source owner while processing: the run must report failure rather
   than claim complete success. Rerun with fresh CRM data to reconcile.
6. Check failed/cancelled runs explicitly; there is no background retry service.

## Remaining boundaries

- External users and other workflows are not locked by this group. Other writers
  must be audited before enabling overlapping bulk operations.
- Bitrix updates are not transactional. A race between read and write remains;
  stopping on error does not undo an already-written record.
- Duplicate founder contacts with conflicting owners still use existing source
  selection rules. Simultaneous edits to different duplicates require manual
  resolution; this patch does not invent a new ownership priority.
- Full FIO matching, package membership, and skipped ambiguous companies retain
  existing behavior. Deals are not part of this script's package.
- Local tests cover Python logic with a fake CRM, not live Windows runner or
  Bitrix behavior. Live acceptance is still required.
