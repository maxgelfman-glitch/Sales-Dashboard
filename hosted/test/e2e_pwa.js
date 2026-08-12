/* E2E: PWA offline app-shell + migration import from the free app's JSON.
 *  - Signs in, waits for the service worker to cache the shell.
 *  - Reloads fully OFFLINE and asserts the app shell still boots from cache
 *    and shows locally-mirrored leads (no network at all).
 *  - Imports a free-app export file and asserts those leads reach Supabase.
 */
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const { execSync } = require('child_process');
const fs = require('fs');
const APP = 'http://127.0.0.1:4100/index.html';
const GW = 'http://127.0.0.1:4001';
const QS = '?supabase_url=' + encodeURIComponent(GW) + '&supabase_key=testanonkey';
let pass = 0, fail = 0;
function ck(n, c) { if (c) { console.log('  PASS: ' + n); pass++; } else { console.log('  FAIL: ' + n); fail++; } }
function psql(sql) { return execSync(`PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d powerwash -qAt -c "${sql}"`, { encoding: 'utf8' }).trim(); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitDB(sql, want, tries) { for (let i = 0; i < (tries || 20); i++) { if (psql(sql) === String(want)) return true; await sleep(300); } return false; }
async function signIn(ctx, email) {
  const page = await ctx.newPage();
  page.on('pageerror', e => console.log('  [pageerror] ' + e.message));
  await page.goto(APP + QS, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#hb-toggle', { timeout: 10000 });
  await page.click('#hb-toggle'); await page.fill('#hb-email', email); await page.fill('#hb-pw', 'x'); await page.click('#hb-go');
  await page.waitForSelector('[data-act="add"]', { timeout: 10000 });
  return page;
}
async function addLead(page, name) {
  await page.click('[data-act="add"]'); await page.waitForSelector('#f-name'); await page.fill('#f-name', name);
  await page.click('.modal-foot [data-savelead]'); await sleep(250);
  const c = await page.$('.so-head [data-close]'); if (c) { await c.click().catch(() => {}); await sleep(120); }
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  try {
    // ---- offline app-shell ----
    const ctx = await browser.newContext();
    const page = await signIn(ctx, 'owner@a.co');
    await addLead(page, 'Shell Cache Lead');
    await waitDB("select count(*) from leads where name='Shell Cache Lead'", 1);
    // wait for the service worker to activate and cache the shell
    await page.waitForFunction(() => navigator.serviceWorker && navigator.serviceWorker.controller, { timeout: 10000 }).catch(() => {});
    const swActive = await page.evaluate(() => !!(navigator.serviceWorker && navigator.serviceWorker.controller));
    ck('service worker is controlling the page', swActive);
    // wait until the shell is actually in the cache (deterministic, not a sleep)
    const cached = await page.waitForFunction(async () => {
      const c = await caches.open('pwcrm-shell-v1');
      return !!(await c.match('./index.html', { ignoreSearch: true }));
    }, { timeout: 10000 }).then(() => true).catch(() => false);
    ck('app shell is cached for offline use', cached);

    // go fully offline and reload — the app must still boot from cache
    await ctx.setOffline(true);
    await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
    const booted = await page.waitForSelector('[data-act="add"]', { timeout: 10000 }).then(() => true).catch(() => false);
    ck('app shell boots with NO network (offline reload)', booted);
    await page.click('[data-nav="contacts"]').catch(() => {});
    await sleep(400);
    ck('offline shell shows mirrored lead', (await page.content()).indexOf('Shell Cache Lead') !== -1);
    await ctx.setOffline(false);

    // ---- migration import from the free app's JSON ----
    const exportFile = '/tmp/freeapp_export.json';
    fs.writeFileSync(exportFile, JSON.stringify({ v: 1, leads: [
      { id: 'labc123', type: 'residential', name: 'Imported Homeowner', phone: '555-0100', value: 750, stage: 'quoted', source: 'Yard Sign', services: ['house'], comms: [{ id: 'cx1', method: 'call', date: '2026-08-01', notes: 'called', outcome: '', ts: Date.now() }], followUp: { date: '2026-08-20', note: 'follow up' } },
      { id: 'ldef456', type: 'commercial', name: 'Imported Plaza LLC', value: 5200, stage: 'new', source: 'Referral', services: ['concrete'], comms: [] }
    ] }));
    await page.click('[data-act="account"]');
    await page.waitForSelector('[data-act="hb-import"]', { timeout: 5000 });
    // set the file on the hidden input the click will create
    page.on('filechooser', async fc => { await fc.setFiles(exportFile); });
    await page.click('[data-act="hb-import"]');
    ck('imported free-app leads reach Supabase', await waitDB("select count(*) from leads where name in ('Imported Homeowner','Imported Plaza LLC')", 2, 30));
    ck('imported lead keeps its comm', await waitDB("select count(*) from comms c join leads l on l.id=c.lead_id where l.name='Imported Homeowner'", 1, 20));
    ck('imported ids were re-issued as uuids', psql("select count(*) from leads where id::text='labc123'") === '0');
    ck('imported lead attributed to importer (owner A)', psql("select p.email from leads l join profiles p on p.id=l.created_by where l.name='Imported Homeowner'") === 'owner@a.co');

  } catch (e) { console.log('  EXCEPTION: ' + (e && e.stack || e)); fail++; }
  finally { await browser.close(); }
  console.log('\nPWA/IMPORT RESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
