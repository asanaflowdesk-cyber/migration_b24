from pathlib import Path
import shutil
import subprocess

import pytest


def test_queue_storage_failure_does_not_consume_owner_change():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the Apps Script fault injection test")
    path = Path(__file__).resolve().parents[3] / "integrations" / "bitrix_owner_sync_queue" / "Code.gs"
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const saved = new Map(); let fail = true, writes = 0;
const context = {PropertiesService: {getScriptProperties: () => ({getProperty: k => saved.get(k), setProperty: (k,v) => saved.set(k,v), deleteProperty: k => saved.delete(k)})}};
vm.createContext(context); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.sheet_ = () => {if (fail) throw Error('storage_failure'); return {};};
context.rows_ = () => []; context.writeRows_ = () => writes++;
context.normalizeStates_ = () => {}; context.dispatchPending_ = () => false;
assert.throws(() => context.enqueue_({contact_id: 837, owner_id: 69}));
assert.equal(saved.has('OWNER_SYNC_LAST_837'), false);
fail = false; assert.equal(context.enqueue_({contact_id: 837, owner_id: 69}).queued, true);
assert.equal(writes, 1);
assert.equal(context.enqueue_({contact_id: 837, owner_id: 69}).reason, 'owner_unchanged');
'''
    subprocess.run([node, "-e", script, str(path)], check=True, capture_output=True, text=True)
