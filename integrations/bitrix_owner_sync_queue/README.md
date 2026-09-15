# Bitrix founder-package queue

The bound Google Apps Script stores only contact IDs and processing state in the
`QUEUE` sheet. It does not store names, company data, leads, or credentials.

## Script properties

Set these in Apps Script **Project Settings → Script properties**:

- `QUEUE_API_KEY`: a random secret of at least 32 characters.

Run `setupQueue` once from the editor and approve spreadsheet access. It records
the bound spreadsheet ID and validates the headers.

Deploy as **Web app**, execute as the script owner, access **Anyone**. Copy the
`/exec` URL. Do not publish the spreadsheet itself.

## Vercel variables

- `GOOGLE_QUEUE_URL`: the Apps Script `/exec` URL.
- `GOOGLE_QUEUE_KEY`: the same `QUEUE_API_KEY`.

Redeploy Vercel after adding the variables.

## GitHub Actions secrets

- `GOOGLE_QUEUE_URL`: the same Apps Script `/exec` URL.
- `GOOGLE_QUEUE_KEY`: the same `QUEUE_API_KEY`.

Workflow 31A is normally dispatched by Vercel only for the first queued event.
The scheduled run every ten minutes recovers a missed dispatch or expired claim.
One run claims up to 500 contact IDs, loads CRM once for that batch, processes
each independent package, acknowledges successful versions, and then drains any
events that arrived while it was running.

Failed items enter `RETRY` for ten minutes. A new Bitrix event for the same
contact makes it immediately pending. A completion for an older version cannot
erase a newer event.
