/* Conflict edge: an offline edit to a lead that was deleted on another device.
 * Verifies the outcome is SAFE (no crash, no corruption) and documents which
 * side wins. Design choice: an edit is newer intent than the delete, so the
 * lead is resurrected with the edit rather than silently losing the edit —
 * losing an extra row is recoverable; losing a user's edit is not. */
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const { execSync } = require('child_process');
const APP = 'http://127.0.0.1:4100/index.html', GW = 'http://127.0.0.1:4001';
const QS = '?supabase_url=' + encodeURIComponent(GW) + '&supabase_key=t';
let pass = 0, fail = 0;
function ck(n, c) { if (c) { console.log('  PASS: ' + n); pass++; } else { console.log('  FAIL: ' + n); fail++; } }
const psql = s => execSync(`PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d powerwash -qAt -c "${s}"`, { encoding: 'utf8' }).trim();
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitDB(sql, want, tries) { for (let i = 0; i < (tries || 40); i++) { if (psql(sql) === String(want)) return true; await sleep(300); } return false; }
async function signIn(ctx, email) {
  const p = await ctx.newPage(); p.on('pageerror', e => console.log('  [pageerror] ' + e.message));
  await p.goto(APP + QS, { waitUntil: 'domcontentloaded' });
  await p.waitForSelector('#hb-toggle'); await p.click('#hb-toggle');
  await p.fill('#hb-email', email); await p.fill('#hb-pw', 'x'); await p.click('#hb-go');
  await p.waitForSelector('[data-act="add"]', { timeout: 10000 }); await sleep(600); return p;
}
async function pull(p) { await p.evaluate(() => window.dispatchEvent(new Event('focus'))); await sleep(1600); }
async function contacts(p) { await p.click('[data-nav="contacts"]').catch(() => {}); await sleep(250); }
async function closeOv(p) { const c = await p.$('.so-head [data-close]'); if (c) { await c.click().catch(() => {}); await sleep(150); } }
async function addLead(p, n) { await p.click('[data-act="add"]'); await p.waitForSelector('#f-name'); await p.fill('#f-name', n); await p.click('.modal-foot [data-savelead]'); await sleep(300); await closeOv(p); }
async function openLead(p) { await contacts(p); await p.click('[data-open]'); await p.waitForSelector('.so-title', { timeout: 5000 }); }

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  try {
    const c1 = await browser.newContext(); const d1 = await signIn(c1, 'owner@a.co');
    const c2 = await browser.newContext(); const d2 = await signIn(c2, 'agent@a.co');
    await addLead(d1, 'ConflictLead');
    ck('lead exists on server', await waitDB("select count(*) from leads where name='ConflictLead'", 1));
    await pull(d2); await contacts(d2);
    ck('device2 has the lead', (await d2.content()).indexOf('ConflictLead') !== -1);

    // device2 goes offline and edits the value
    await openLead(d2);
    await c2.setOffline(true); await d2.evaluate(() => window.dispatchEvent(new Event('offline')));
    await d2.click('[data-edit]'); await d2.waitForSelector('#f-value');
    await d2.fill('#f-value', '7777'); await d2.click('.modal-foot [data-savelead]'); await sleep(300); await closeOv(d2);

    // meanwhile device1 (owner) deletes it
    await openLead(d1); await d1.click('[data-del]');
    ck('owner delete removes it from server', await waitDB("select count(*) from leads where name='ConflictLead'", 0));

    // device2 reconnects -> its offline edit resurrects the lead (no data loss)
    await c2.setOffline(false); await d2.evaluate(() => window.dispatchEvent(new Event('online')));
    ck('offline edit resurrects lead (edit preserved, not lost)', await waitDB("select value::int from leads where name='ConflictLead'", 7777, 40));

    // and the system is consistent: exactly one such lead, no duplicates/corruption
    ck('exactly one row, no duplication/corruption', psql("select count(*) from leads where name='ConflictLead'") === '1');
    await pull(d1); await contacts(d1);
    ck('device1 converges to the resurrected lead', (await d1.content()).indexOf('ConflictLead') !== -1);

  } catch (e) { console.log('  EXCEPTION: ' + (e && e.stack || e)); fail++; }
  finally { await browser.close(); }
  console.log('\nCONFLICT RESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
