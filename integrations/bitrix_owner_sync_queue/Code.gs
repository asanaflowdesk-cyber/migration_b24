const QUEUE_SHEET = 'QUEUE';
const HEADERS = ['contact_id', 'version', 'state', 'updated_at', 'claim_id', 'claimed_version', 'claimed_at', 'attempts', 'last_error', 'completed_at'];
const LEASE_MS = 30 * 60 * 1000;
const RETRY_MS = 10 * 60 * 1000;

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function sheet_() {
  const id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!id) throw new Error('SPREADSHEET_ID is not configured');
  const sheet = SpreadsheetApp.openById(id).getSheetByName(QUEUE_SHEET);
  if (!sheet) throw new Error('QUEUE sheet is missing');
  return sheet;
}

function authorized_(body) {
  const expected = PropertiesService.getScriptProperties().getProperty('QUEUE_API_KEY') || '';
  return expected.length >= 32 && String(body.key || '') === expected;
}

function rows_(sheet) {
  const last = sheet.getLastRow();
  return last < 2 ? [] : sheet.getRange(2, 1, last - 1, HEADERS.length).getValues();
}

function iso_() { return new Date().toISOString(); }

function expired_(value, ageMs) {
  const time = new Date(value || 0).getTime();
  return !Number.isFinite(time) || Date.now() - time >= ageMs;
}

function normalizeStates_(rows) {
  rows.forEach(row => {
    if (row[2] === 'CLAIMED' && expired_(row[6], LEASE_MS)) {
      row[2] = 'PENDING';
      row[4] = '';
      row[5] = '';
      row[6] = '';
      row[8] = 'claim_lease_expired';
    } else if (row[2] === 'RETRY' && expired_(row[6], RETRY_MS)) {
      row[2] = 'PENDING';
      row[4] = '';
      row[5] = '';
      row[6] = '';
    }
  });
}

function doPost(e) {
  let body = {};
  try {
    body = JSON.parse((e.postData && e.postData.contents) || '{}');
    if (!authorized_(body)) return json_({ok: false, error: 'unauthorized'});
    const lock = LockService.getScriptLock();
    lock.waitLock(20000);
    try {
      if (body.action === 'enqueue') return json_(enqueue_(body));
      if (body.action === 'claim') return json_(claim_(body));
      if (body.action === 'complete') return json_(complete_(body));
      if (body.action === 'release_dispatch') return json_(releaseDispatch_());
      return json_({ok: false, error: 'unknown_action'});
    } finally {
      lock.releaseLock();
    }
  } catch (error) {
    console.error(error && error.stack ? error.stack : error);
    return json_({ok: false, error: 'internal_error'});
  }
}

function enqueue_(body) {
  const contactId = Number(body.contact_id);
  if (!Number.isSafeInteger(contactId) || contactId <= 0) return {ok: false, error: 'invalid_contact_id'};
  const sheet = sheet_();
  const rows = rows_(sheet);
  normalizeStates_(rows);
  const now = iso_();
  let index = rows.findIndex(row => Number(row[0]) === contactId);
  if (index < 0) {
    rows.push([contactId, 1, 'PENDING', now, '', '', '', 0, '', '']);
    index = rows.length - 1;
  } else {
    const row = rows[index];
    row[1] = Number(row[1] || 0) + 1;
    row[3] = now;
    row[8] = '';
    row[9] = '';
    if (row[2] !== 'CLAIMED') row[2] = 'PENDING';
  }
  writeRows_(sheet, rows);
  const props = PropertiesService.getScriptProperties();
  const active = rows.some(row => row[2] === 'CLAIMED');
  const dispatchPending = props.getProperty('DISPATCH_PENDING') === '1';
  const dispatch = !active && !dispatchPending;
  if (dispatch) props.setProperty('DISPATCH_PENDING', '1');
  return {ok: true, dispatch: dispatch, contact_id: contactId, version: rows[index][1]};
}

function claim_(body) {
  const limit = Math.min(Math.max(Number(body.limit) || 500, 1), 500);
  const workerId = String(body.worker_id || '').slice(0, 100);
  if (!workerId) return {ok: false, error: 'missing_worker_id'};
  const sheet = sheet_();
  const rows = rows_(sheet);
  normalizeStates_(rows);
  if (rows.some(row => row[2] === 'CLAIMED')) {
    writeRows_(sheet, rows);
    return {ok: true, busy: true, items: []};
  }
  const pending = [];
  rows.forEach((row, index) => { if (row[2] === 'PENDING' && pending.length < limit) pending.push(index); });
  PropertiesService.getScriptProperties().deleteProperty('DISPATCH_PENDING');
  if (!pending.length) {
    writeRows_(sheet, rows);
    return {ok: true, items: []};
  }
  const claimId = workerId + '-' + Utilities.getUuid();
  const now = iso_();
  const items = pending.map(index => {
    const row = rows[index];
    row[2] = 'CLAIMED';
    row[4] = claimId;
    row[5] = Number(row[1]);
    row[6] = now;
    row[7] = Number(row[7] || 0) + 1;
    return {contact_id: Number(row[0]), version: Number(row[5]), updated_at: String(row[3])};
  });
  writeRows_(sheet, rows);
  return {ok: true, claim_id: claimId, items: items};
}

function complete_(body) {
  const claimId = String(body.claim_id || '');
  const results = Array.isArray(body.results) ? body.results : [];
  if (!claimId || !results.length) return {ok: false, error: 'invalid_completion'};
  const byKey = {};
  results.forEach(result => { byKey[String(result.contact_id) + ':' + String(result.version)] = result; });
  const sheet = sheet_();
  const rows = rows_(sheet);
  const now = iso_();
  let completed = 0;
  rows.forEach(row => {
    if (row[2] !== 'CLAIMED' || String(row[4]) !== claimId) return;
    const result = byKey[String(row[0]) + ':' + String(row[5])];
    if (!result) return;
    const hasNewerVersion = Number(row[1]) !== Number(row[5]);
    if (result.success && !hasNewerVersion) {
      row[2] = 'DONE';
      row[8] = '';
      row[9] = now;
    } else if (result.success && hasNewerVersion) {
      row[2] = 'PENDING';
      row[8] = '';
    } else {
      row[2] = 'RETRY';
      row[8] = String(result.error || 'sync_failed').slice(0, 250);
    }
    row[4] = '';
    row[5] = '';
    completed += 1;
  });
  writeRows_(sheet, rows);
  return {ok: true, completed: completed, pending: rows.filter(row => row[2] === 'PENDING').length};
}

function releaseDispatch_() {
  PropertiesService.getScriptProperties().deleteProperty('DISPATCH_PENDING');
  return {ok: true};
}

function writeRows_(sheet, rows) {
  if (rows.length) sheet.getRange(2, 1, rows.length, HEADERS.length).setValues(rows);
}

function setupQueue() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) throw new Error('Open this function from the bound spreadsheet project');
  PropertiesService.getScriptProperties().setProperty('SPREADSHEET_ID', spreadsheet.getId());
  const sheet = spreadsheet.getSheetByName(QUEUE_SHEET);
  if (!sheet) throw new Error('QUEUE sheet is missing');
  const actual = sheet.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  if (JSON.stringify(actual) !== JSON.stringify(HEADERS)) throw new Error('QUEUE headers do not match');
}
