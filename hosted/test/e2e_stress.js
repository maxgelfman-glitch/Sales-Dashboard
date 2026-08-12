/* Stress / edge: bulk sync of many leads, rapid consecutive edits (debounce
 * must coalesce without losing the final value), and a smoke test that the
 * untouched FREE app still loads and seeds. */
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const { execSync } = require('child_process');
const fs = require('fs');
const APP = 'http://127.0.0.1:4100/index.html';
const FREE = 'http://127.0.0.1:4100/free.html';
const GW = 'http://127.0.0.1:4001';
const QS = '?supabase_url=' + encodeURIComponent(GW) + '&supabase_key=t';
let pass = 0, fail = 0;
function ck(n, c) { if (c) { console.log('  PASS: ' + n); pass++; } else { console.log('  FAIL: ' + n); fail++; } }
const psql = s => execSync(`PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d powerwash -qAt -c "${s}"`, { encoding: 'utf8' }).trim();
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitDB(sql, want, tries) { for (let i = 0; i < (tries || 40); i++) { if (psql(sql) === String(want)) return true; await sleep(300); } return false; }
async function signIn(ctx, email) {
  const p = await ctx.newPage();
  p.on('pageerror', e => console.log('  [pageerror] ' + e.message));
  await p.goto(APP + QS, { waitUntil: 'domcontentloaded' });
  await p.waitForSelector('#hb-toggle'); await p.click('#hb-toggle');
  await p.fill('#hb-email', email); await p.fill('#hb-pw', 'x'); await p.click('#hb-go');
  await p.waitForSelector('[data-act="add"]', { timeout: 10000 }); await sleep(600);
  return p;
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  try {
    const ctx = await browser.newContext();
    const page = await signIn(ctx, 'owner@a.co');

    // ---- bulk: import 60 leads at once ----
    const N = 60;
    const leads = [];
    for (let i = 1; i <= N; i++) leads.push({ id: 'lx' + i, type: i % 2 ? 'residential' : 'commercial', name: 'Bulk Lead ' + i, value: i * 10, stage: ['new', 'contacted', 'quoted', 'job', 'won'][i % 5], source: 'Referral', services: ['house'], comms: i % 3 === 0 ? [{ id: 'cx' + i, method: 'call', date: '2026-08-01', notes: 'n', outcome: '', ts: Date.now() }] : [] });
    const f = '/tmp/bulk60.json'; fs.writeFileSync(f, JSON.stringify({ v: 1, leads }));
    page.on('filechooser', async fc => { await fc.setFiles(f); });
    await page.click('[data-act="account"]'); await page.waitForSelector('[data-act="hb-import"]');
    await page.click('[data-act="hb-import"]');
    ck('all 60 bulk leads synced to server', await waitDB("select count(*) from leads where name like 'Bulk Lead %'", 60, 60));
    ck('bulk comms synced too', await waitDB("select count(*) from comms", 20, 40)); // every 3rd lead has 1 comm => 20
    ck('no data corruption: distinct names == 60', psql("select count(distinct name) from leads where name like 'Bulk Lead %'") === '60');

    // ---- rapid consecutive edits to one lead: final value must win ----
    await page.click('[data-nav="contacts"]').catch(() => {}); await sleep(300);
    await page.click('[data-open]'); await page.waitForSelector('.so-title');
    let finalVal = 0;
    for (let k = 1; k <= 8; k++) {
      await page.click('[data-edit]'); await page.waitForSelector('#f-value');
      finalVal = 1000 + k;
      await page.fill('#f-value', String(finalVal));
      await page.click('.modal-foot [data-savelead]'); await sleep(90); // faster than the 300ms debounce
    }
    const editedName = await page.$eval('.so-title', el => el.textContent.trim());
    ck('rapid edits: final value persists on server', await waitDB(`select value::int from leads where name='${editedName.replace(/'/g, "''")}'`, finalVal, 40));

    // ---- FREE app smoke test (must be untouched & working) ----
    fs.copyFileSync('/home/user/Sales-Dashboard/index.html', '/home/user/Sales-Dashboard/hosted/app/free.html');
    const fctx = await browser.newContext();
    const fp = await fctx.newPage();
    fp.on('pageerror', e => console.log('  [free pageerror] ' + e.message));
    await fp.goto(FREE, { waitUntil: 'domcontentloaded' });
    await fp.waitForSelector('[data-act="add"]', { timeout: 8000 });
    await sleep(500);
    const freeContent = await fp.content();
    ck('free app loads and seeds sample data', freeContent.indexOf('Riverside Plaza HOA') !== -1);
    ck('free app uses its own localStorage (no Supabase gate)', (await fp.$('#hb-toggle')) === null);
    fs.unlinkSync('/home/user/Sales-Dashboard/hosted/app/free.html');

  } catch (e) { console.log('  EXCEPTION: ' + (e && e.stack || e)); fail++; }
  finally { await browser.close(); }
  console.log('\nSTRESS RESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
