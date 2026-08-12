/* Pure unit tests for the sync engine (flatten/inflate/diff/merge). */
const Sync = require('../app/sync.js');
let pass = 0, fail = 0;
function ck(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { console.log('  PASS: ' + name); pass++; }
  else { console.log('  FAIL: ' + name + '\n    got:  ' + g + '\n    want: ' + w); fail++; }
}
const CTX = { orgId: 'org-1', uid: 'user-1' };

// ---- flatten / inflate round-trip ----
const lead = {
  id: 'L1', type: 'commercial', name: 'HOA on Main', contact: 'Jane', phone: '555', email: 'j@x.com',
  address: '1 Main', value: '5000', source: 'Referral', propertyType: 'HOA', stage: 'quoted',
  confidence: 'hot', notes: 'big job', services: ['house', 'roof'],
  comms: [
    { id: 'C1', method: 'call', date: '2026-08-01', notes: 'left vm', outcome: 'novm', ts: 1000 },
    { id: 'C2', method: 'email', date: '2026-08-02', notes: 'sent quote', outcome: 'sent', ts: 2000 }
  ],
  followUp: { date: '2026-08-10', note: 'circle back' },
  reclean: 6, lostReason: '', sample: false, created: 1000, updated: 5000
};
const flat = Sync.flatten(lead, CTX);
ck('flatten stamps org_id', flat.leadRow.org_id, 'org-1');
ck('flatten maps propertyType->property_type', flat.leadRow.property_type, 'HOA');
ck('flatten splits followUp into columns', [flat.leadRow.followup_date, flat.leadRow.followup_note], ['2026-08-10', 'circle back']);
ck('flatten coerces value to number', flat.leadRow.value, 5000);
ck('flatten produces 2 comm rows w/ lead_id', flat.commRows.map(c => c.lead_id), ['L1', 'L1']);
ck('flatten stamps comm org_id', flat.commRows[0].org_id, 'org-1');

const back = Sync.inflate(flat.leadRow, flat.commRows);
ck('round-trip name', back.name, 'HOA on Main');
ck('round-trip propertyType', back.propertyType, 'HOA');
ck('round-trip followUp', back.followUp, { date: '2026-08-10', note: 'circle back' });
ck('round-trip services', back.services, ['house', 'roof']);
ck('round-trip comms count', back.comms.length, 2);
ck('round-trip comm outcome', back.comms[0].outcome, 'novm');
ck('inflate sorts comms by ts', back.comms.map(c => c.id), ['C1', 'C2']);

// ---- diff: new lead ----
let d = Sync.diff([lead], [], CTX);
ck('diff new lead -> 1 lead upsert', d.leadUpserts.length, 1);
ck('diff new lead -> 2 comm upserts', d.commUpserts.length, 2);
ck('diff new lead -> no deletes', [d.leadDeletes.length, d.commDeletes.length], [0, 0]);

// ---- diff: no change ----
d = Sync.diff([lead], [lead], CTX);
ck('diff identical -> empty', d.empty, true);

// ---- diff: changed field ----
let lead2 = JSON.parse(JSON.stringify(lead)); lead2.stage = 'won'; lead2.updated = 6000;
d = Sync.diff([lead2], [lead], CTX);
ck('diff stage change -> 1 lead upsert', d.leadUpserts.length, 1);
ck('diff stage change -> 0 comm upserts', d.commUpserts.length, 0);

// ---- diff: added comm ----
let lead3 = JSON.parse(JSON.stringify(lead));
lead3.comms.push({ id: 'C3', method: 'text', date: '2026-08-03', notes: 'replied', outcome: 'reply', ts: 3000 });
d = Sync.diff([lead3], [lead], CTX);
ck('diff added comm -> 1 comm upsert', d.commUpserts.length, 1);
ck('diff added comm -> lead unchanged (0 lead upsert)', d.leadUpserts.length, 0);

// ---- diff: deleted comm ----
let lead4 = JSON.parse(JSON.stringify(lead)); lead4.comms = [lead.comms[0]];
d = Sync.diff([lead4], [lead], CTX);
ck('diff deleted comm -> 1 comm delete', d.commDeletes, ['C2']);

// ---- diff: deleted lead ----
d = Sync.diff([], [lead], CTX);
ck('diff deleted lead -> 1 lead delete', d.leadDeletes, ['L1']);
ck('diff deleted lead -> no orphan comm deletes (cascade)', d.commDeletes.length, 0);

// ---- diff: audit-only churn (same content, new updated_by/ts) must NOT write ----
let lead5 = JSON.parse(JSON.stringify(lead));
d = Sync.diff([lead5], [lead], { orgId: 'org-1', uid: 'user-2' });
ck('diff audit-only change -> empty (no needless write)', d.empty, true);

// ---- merge last-write-wins ----
let localL = { id: 'L1', name: 'local', updated: 100 };
let serverL = { id: 'L1', name: 'server', updated: 200 };
ck('merge server newer wins', Sync.mergeByUpdated([localL], [serverL]).find(x => x.id === 'L1').name, 'server');
ck('merge local newer wins', Sync.mergeByUpdated([{ id: 'L1', name: 'local', updated: 300 }], [serverL]).find(x => x.id === 'L1').name, 'local');
ck('merge brings in server-only lead', Sync.mergeByUpdated([], [{ id: 'L9', name: 's', updated: 1 }]).length, 1);

console.log('\nSYNC RESULT: ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail);
