from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
EVENT_JS = ROOT / "integrations" / "bitrix_owner_sync_webhook" / "api" / "bitrix" / "event.js"
QUEUE_GS = ROOT / "integrations" / "bitrix_owner_sync_queue" / "Code.gs"


def test_event_retries_github_dispatch_and_records_failure():
    source = EVENT_JS.read_text(encoding="utf-8")
    assert "const GITHUB_DISPATCH_ATTEMPTS = 3" in source
    assert "GITHUB_RETRYABLE_STATUSES" in source
    assert "async function dispatchGithub" in source
    assert 'queueRequest("dispatch_error"' in source
    assert "github_dispatch_not_configured" in source
    assert "dispatch_attempts" in source


def test_queue_keeps_failed_dispatch_pending_and_visible():
    source = QUEUE_GS.read_text(encoding="utf-8")
    assert "body.action === 'dispatch_error'" in source
    start = source.index("function dispatchError_(body)")
    end = source.index("function releaseDispatch_()", start)
    block = source[start:end]
    assert "DISPATCH_ERROR " in block
    assert "deleteProperty('DISPATCH_PENDING')" in block
    assert "row[2] = 'DISPATCH_ERROR'" not in block
    assert "row[2] = 'MANUAL_REVIEW'" not in block


def test_dispatch_lock_expires_instead_of_blocking_queue_forever():
    source = QUEUE_GS.read_text(encoding="utf-8")
    assert "const DISPATCH_LEASE_MS = 30 * 1000" in source
    assert "function dispatchPending_(props)" in source
    assert "Date.now() - startedAt >= DISPATCH_LEASE_MS" in source
    assert "props.deleteProperty('DISPATCH_PENDING')" in source
    assert "setProperty('DISPATCH_PENDING', String(Date.now()))" in source
    assert "setProperty('DISPATCH_PENDING', '1')" not in source
