from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


def run_node(source, *args):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for webhook/journal checks")
    subprocess.run([node, "--input-type=module", "-e", source, *map(str, args)], check=True, capture_output=True, text=True)


def test_owner_journal_keeps_first_transition_and_storage_retry():
    run_node(r'''
import fs from 'node:fs'; import vm from 'node:vm'; import assert from 'node:assert/strict';
const saved = new Map(), rows = []; let fail = false;
const sheet = {getLastRow: () => rows.length + 1,
 getRange: (r,c,n,w) => ({setValues: values => {if(fail) throw Error('storage'); rows.push(...values);}, getValues: () => rows.slice(r-2,r-2+n)})};
const context = {console, PropertiesService:{getScriptProperties:()=>({getProperty:k=>saved.get(k),setProperty:(k,v)=>saved.set(k,v)})}};
vm.createContext(context); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.ensureSheet_ = () => sheet; context.workbook_ = () => ({});
assert.equal(context.ownerEvent_({entity:'lead',entity_id:3,owner_id:4,event_ts:100}).seq,1);
fail = true; assert.throws(()=>context.ownerEvent_({entity:'lead',entity_id:3,owner_id:13,event_ts:101}));
assert.equal(saved.get('OWNER_EVENT_LAST_lead_3'),'4');
fail = false; assert.equal(context.ownerEvent_({entity:'lead',entity_id:3,owner_id:13,event_ts:101}).seq,2);
assert.equal(context.ownerEvent_({entity:'lead',entity_id:3,owner_id:14,event_ts:102}).seq,3);
assert.equal(context.ownerEvent_({entity:'lead',entity_id:3,owner_id:14,event_ts:103}).reason,'owner_unchanged');
const result = context.ownerEvents_({after:0,limit:2});
assert.deepEqual(Array.from(result.events, x=>x.owner_id),[4,13]);
assert.equal(result.events[1].previous_owner_id,4); assert.equal(result.has_more,true);
const tail = context.ownerEvents_({after:2,limit:2}); assert.equal(tail.events[0].owner_id,14);
assert.equal(context.ownerEvents_({after:9}).error,'owner_event_journal_reset');
''', ROOT / "integrations/bitrix_owner_sync_queue/Code.gs")


def test_webhook_records_lead_and_company_events_and_preserves_contact_queue():
    run_node(r'''
import {pathToFileURL} from 'node:url'; import assert from 'node:assert/strict';
process.env.BITRIX_WEBHOOK_KEY='secret'; process.env.BITRIX_ALLOWED_DOMAIN='box.example.test';
process.env.GOOGLE_QUEUE_KEY='x'.repeat(32); process.env.GOOGLE_QUEUE_URL='https://script.google.com/macros/s/test/exec';
const handler = (await import(pathToFileURL(process.argv[1]))).default;
for (const [event,entity] of [['ONCRMLEADUPDATE','lead'],['ONCRMCOMPANYUPDATE','company'],['ONCRMCONTACTUPDATE','contact']]) {
 const calls=[];
 globalThis.fetch = async (url, options) => {
   if (String(url).includes('crm.')) return {ok:true,json:async()=>({result:{ID:'5',ASSIGNED_BY_ID:'13'}})};
   calls.push(JSON.parse(options.body)); return {ok:true,json:async()=>({ok:true,queued:true,version:1,seq:1})};
 };
 const response = {setHeader(){},status(n){this.statusCode=n;return this;},json(v){this.value=v;return this;}};
 await handler({method:'POST',url:'https://app.test/api/bitrix/event?key=secret',body:{event,ts:'1001',data:{FIELDS:{ID:'5'}},auth:{domain:'box.example.test',client_endpoint:'https://box.example.test/rest/',access_token:'test'}}},response);
 assert.equal(response.statusCode,202); assert.equal(calls[0].action,'owner_event');
 assert.equal(calls[0].entity,entity); assert.equal(calls[0].owner_id,'13');
 assert.equal(calls.length,entity==='contact'?2:1);
 if(entity==='contact') assert.equal(calls[1].action,'enqueue');
}
''', ROOT / "integrations/bitrix_owner_sync_webhook/api/bitrix/event.js")


def test_installer_subscribes_to_all_packet_elements():
    source = (ROOT / "integrations/bitrix_owner_sync_webhook/api/bitrix/install.js").read_text()
    for kind in ("CONTACT", "COMPANY", "LEAD"):
        for operation in ("ADD", "UPDATE"):
            assert f'"ONCRM{kind}{operation}"' in source
