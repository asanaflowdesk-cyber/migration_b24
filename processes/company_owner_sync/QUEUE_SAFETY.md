# Founder package synchronization

Owner events use the durable Google Apps Script queue. The persistent worker
drains it before periodic full CRM recovery. A busy claim or a batch limit
does not count as a drained queue.

The authority is a lead-linked director/founder contact, never DATE_MODIFY.
An unlinked duplicate cannot drive a package when a linked contact exists.
A completed event's source is persisted outside runtime. Without event history,
conflicting linked owners are skipped rather than guessed.

Recovery updates only ASSIGNED_BY_ID on package contacts, companies and leads.
Source ownership is refreshed and writes are read back. A changing source is
retried within bounded stabilization rounds. Failed writes leave residuals for
the next pass. Repeating a successful pass produces no additional writes.

Workflows 31 and 31A share founder-package-owner-writes, queue: max and
cancel-in-progress: false. This serializes Action runs and deployments, not
the detached worker or external writers. Bitrix writes are not transactional;
a later conflicting write is corrected on a subsequent recovery pass.

Push deployments run regression tests before restarting and wait for a fresh
recovery report. Tests cover lost events, duplicate contacts, source changes,
unverified writes, preserved stages, restart state and failed queue writes.
Live deployment logs still require inspection.

Apps Script must be separately published as a new web-app version. Recovery
works with the existing queue, but does not make its old enqueue path atomic.
Ambiguous FIO or company membership is reported. Deals are outside this scope.
