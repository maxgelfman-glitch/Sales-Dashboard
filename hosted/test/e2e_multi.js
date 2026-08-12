/* Multi-device pressure test: two signed-in devices in the SAME org, proving
 * the cross-device guarantees a synced CRM lives or dies on:
 *   - create propagates device1 -> device2
 *   - edit propagates with last-write-wins (device2 -> device1)
 *   - a logged contact (comm) propagates
 *   - a delete on one device propagates to the other (no resurrection)
 *   - switching accounts on a shared device leaks no data across orgs
 * device1 = owner@a.co, device2 = agent@a.co (same org). Pull is triggered the
 * way the app does it in real life: a window 'focus' event.
 */
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const APP = 'http://127.0.0.1:4100/index.html';
const GW = 'http://127.0.0.1:4001';
const QS = '?supabase_url=' + encodeURIComponent(GW) + '&supabase_key=testanonkey';
let pass = 0, fail = 0;
function ck(n, c) { if (c) { console.log('  PASS: ' + n); pass++; } else { console.log('  FAIL: ' + n); fail++; } }
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function signIn(ctx, email) {
  const page = await ctx.newPage();
  page.on('pageerror', e => console.log('  [pageerror ' + email + '] ' + e.message));
  await page.goto(APP + QS, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#hb-toggle', { timeout: 10000 });
  await page.click('#hb-toggle'); await page.fill('#hb-email', email); await page.fill('#hb-pw', 'x'); await page.click('#hb-go');
  await page.waitForSelector('[data-act="add"]', { timeout: 10000 });
  await sleep(800);
  return page;
}
async function pull(page) { await page.evaluate(() => window.dispatchEvent(new Event('focus'))); await sleep(1600); }
async function contacts(page) { await page.click('[data-nav="contacts"]').catch(() => {}); await sleep(250); }
async function closeOverlay(page) { const c = await page.$('.so-head [data-close]'); if (c) { await c.click().catch(() => {}); await sleep(150); } }
async function addLead(page, name) {
  await page.click('[data-act="add"]'); await page.waitForSelector('#f-name'); await page.fill('#f-name', name);
  await page.click('.modal-foot [data-savelead]'); await sleep(300); await closeOverlay(page);
}
async function openLead(page) { await contacts(page); await page.click('[data-open]'); await page.waitForSelector('.so-title', { timeout: 5000 }); }

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  try {
    const d1 = await signIn(await browser.newContext(), 'owner@a.co');   // device 1 (owner)
    const d2 = await signIn(await browser.newContext(), 'agent@a.co');   // device 2 (agent, same org)

    // 1) create propagates d1 -> d2
    await addLead(d1, 'SyncLead');
    await pull(d2); await contacts(d2);
    ck('create propagates owner -> agent device', (await d2.content()).indexOf('SyncLead') !== -1);

    // 2) edit (value) on d2 propagates to d1 (last-write-wins)
    await openLead(d2);
    await d2.click('[data-edit]'); await d2.waitForSelector('#f-value');
    await d2.fill('#f-value', '8888'); await d2.click('.modal-foot [data-savelead]'); await sleep(400); await closeOverlay(d2);
    await pull(d1); await openLead(d1);
    ck('edit propagates agent -> owner (LWW value=8,888)', (await d1.content()).indexOf('8,888') !== -1);
    await closeOverlay(d1);

    // 3) a logged contact propagates d1 -> d2
    await openLead(d1);
    await d1.click('[data-log]'); await d1.waitForSelector('#c-notes');
    await d1.fill('#c-notes', 'Called and left a message'); await d1.click('[data-savecomm]'); await sleep(400); await closeOverlay(d1);
    await pull(d2); await openLead(d2);
    ck('logged contact propagates owner -> agent', (await d2.content()).indexOf('Called and left a message') !== -1);
    await closeOverlay(d2);

    // 4) delete on d1 (owner) propagates to d2 — no resurrection
    await openLead(d1); await d1.click('[data-del]'); await sleep(600);
    await pull(d2); await contacts(d2);
    ck('delete propagates owner -> agent (no resurrection)', (await d2.content()).indexOf('SyncLead') === -1);
    // and it stays gone after a second pull
    await pull(d2); await contacts(d2);
    ck('deleted lead stays gone after re-pull', (await d2.content()).indexOf('SyncLead') === -1);

    // 5) account switch on a shared device leaks nothing across orgs
    await addLead(d1, 'OrgAOnlyLead'); await sleep(600);
    // reuse d2's context: sign out, sign in as owner of a DIFFERENT org
    const d2ctx = d2.context();
    await d2.click('[data-act="account"]'); await d2.waitForSelector('[data-act="hb-signout"]');
    await d2.click('[data-act="hb-signout"]'); await sleep(600);
    await d2.waitForSelector('#hb-toggle', { timeout: 8000 });
    await d2.click('#hb-toggle'); await d2.fill('#hb-email', 'owner@b.co'); await d2.fill('#hb-pw', 'x'); await d2.click('#hb-go');
    await d2.waitForSelector('[data-act="add"]', { timeout: 10000 }); await sleep(1200); await contacts(d2);
    ck('account switch shows no other-org leads', (await d2.content()).indexOf('OrgAOnlyLead') === -1 && (await d2.content()).indexOf('SyncLead') === -1);

  } catch (e) { console.log('  EXCEPTION: ' + (e && e.stack || e)); fail++; }
  finally { await browser.close(); }
  console.log('\nMULTI-DEVICE RESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
