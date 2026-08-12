/* End-to-end test of the HOSTED app in a real browser (Chromium), driving the
 * actual UI against the real Supabase gateway -> PostgREST -> Postgres stack
 * with RLS enforced. Proves: auth, org bootstrap, create/persist/sync across
 * reloads, multi-user org sharing, role-gated delete, tenant isolation, and
 * offline write-then-sync. DB assertions read Postgres directly via psql.
 */
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const { execSync } = require('child_process');

const APP = 'http://127.0.0.1:4100/index.html';
const GW = 'http://127.0.0.1:4001';
const QS = '?supabase_url=' + encodeURIComponent(GW) + '&supabase_key=testanonkey';

let pass = 0, fail = 0;
function ck(name, cond) { if (cond) { console.log('  PASS: ' + name); pass++; } else { console.log('  FAIL: ' + name); fail++; } }
function psql(sql) {
  return execSync(`PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d powerwash -qAt -c "${sql}"`, { encoding: 'utf8' }).trim();
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitDB(sql, want, tries) {
  for (let i = 0; i < (tries || 20); i++) { if (psql(sql) === String(want)) return true; await sleep(300); }
  return false;
}

async function signIn(ctx, email) {
  const page = await ctx.newPage();
  page.on('pageerror', e => console.log('  [pageerror ' + email + '] ' + e.message));
  await page.goto(APP + QS, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#hb-toggle', { timeout: 10000 });
  await page.click('#hb-toggle');                 // switch to password mode
  await page.fill('#hb-email', email);
  await page.fill('#hb-pw', 'testpass');
  await page.click('#hb-go');
  await page.waitForSelector('[data-act="add"]', { timeout: 10000 }); // app shell loaded
  return page;
}
async function gotoContacts(page) {
  await page.click('[data-nav="contacts"]').catch(() => {});
  await sleep(150);
}
async function addLead(page, name) {
  await page.click('[data-act="add"]');
  await page.waitForSelector('#f-name');
  await page.fill('#f-name', name);
  await page.click('.modal-foot [data-savelead]');
  await sleep(250);
  // saveLead opens the new lead's side panel; close it so the next action is unobstructed
  const close = await page.$('.so-head [data-close]');
  if (close) { await close.click().catch(() => {}); await sleep(120); }
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  try {
    // ========== Owner A: sign in, create, persist ==========
    const ctxA = await browser.newContext();
    let ownerA = await signIn(ctxA, 'owner@a.co');
    ck('owner A signs in and app loads', await ownerA.$('[data-act="add"]') !== null);
    ck('owner A profile exists in DB', psql("select count(*) from profiles where email='owner@a.co'") === '1');

    await addLead(ownerA, 'Maple Estate HOA');
    ck('lead written to Supabase (owner A)', await waitDB("select count(*) from leads where name='Maple Estate HOA'", 1));
    ck('lead stamped with org A', psql("select o.name from leads l join orgs o on o.id=l.org_id where l.name='Maple Estate HOA'") === 'A Co');
    ck('lead attributed to owner A', psql("select p.email from leads l join profiles p on p.id=l.created_by where l.name='Maple Estate HOA'") === 'owner@a.co');

    // ========== Persistence across reload (pulled from server) ==========
    await ownerA.reload({ waitUntil: 'domcontentloaded' });
    await ownerA.waitForSelector('[data-act="add"]', { timeout: 10000 });
    await sleep(1200); // allow pull
    await gotoContacts(ownerA);
    ck('lead survives reload (server pull)', (await ownerA.content()).indexOf('Maple Estate HOA') !== -1);

    // ========== Agent A: same org sees it; cannot delete ==========
    const ctxAg = await browser.newContext();
    let agentA = await signIn(ctxAg, 'agent@a.co');
    await sleep(1200);
    await gotoContacts(agentA);
    ck('agent A (same org) sees the lead', (await agentA.content()).indexOf('Maple Estate HOA') !== -1);
    ck('agent A role is agent in DB', psql("select role from profiles where email='agent@a.co'") === 'agent');
    // open the lead and try to delete
    await agentA.click('[data-open]');
    await agentA.waitForSelector('[data-del]', { timeout: 5000 });
    await agentA.click('[data-del]');
    await sleep(1200);
    ck('agent A delete BLOCKED (lead still in DB)', psql("select count(*) from leads where name='Maple Estate HOA'") === '1');

    // ========== Owner B: tenant isolation ==========
    const ctxB = await browser.newContext();
    let ownerB = await signIn(ctxB, 'owner@b.co');
    await sleep(1200);
    await gotoContacts(ownerB);
    ck('owner B does NOT see org A lead (tenant isolation)', (await ownerB.content()).indexOf('Maple Estate HOA') === -1);
    // owner B creates their own lead
    await addLead(ownerB, 'Rival Job Xyz');
    ck('owner B lead written under org B', await waitDB("select count(*) from leads l join orgs o on o.id=l.org_id where l.name='Rival Job Xyz' and o.name='B Co'", 1));
    ck('org A still isolated from B lead', psql("select count(*) from leads l join orgs o on o.id=l.org_id where o.name='A Co' and l.name='Rival Job Xyz'") === '0');

    // ========== Owner A: delete works ==========
    await ownerA.bringToFront();
    await gotoContacts(ownerA);
    await ownerA.click('[data-open]');
    await ownerA.waitForSelector('[data-del]', { timeout: 5000 });
    await ownerA.click('[data-del]');
    ck('owner A delete removes lead from DB', await waitDB("select count(*) from leads where name='Maple Estate HOA'", 0));

    // ========== Offline write, then sync on reconnect ==========
    await addLead(ownerA, 'Online Lead One');
    await waitDB("select count(*) from leads where name='Online Lead One'", 1);
    await ctxA.setOffline(true);
    await ownerA.evaluate(() => window.dispatchEvent(new Event('offline')));
    await addLead(ownerA, 'Offline Lead Two');
    await sleep(1000);
    ck('offline lead NOT yet on server', psql("select count(*) from leads where name='Offline Lead Two'") === '0');
    await ctxA.setOffline(false);
    await ownerA.evaluate(() => window.dispatchEvent(new Event('online')));
    ck('offline lead syncs after reconnect', await waitDB("select count(*) from leads where name='Offline Lead Two'", 1, 30));

    // ========== Invite flow: owner A invites a new VA email ==========
    await ownerA.evaluate(() => window.__hbInviteTest && 0);
    // drive via account modal
    await ownerA.click('[data-act="account"]');
    await ownerA.waitForSelector('[data-act="hb-invite"]', { timeout: 5000 });
    await ownerA.fill('#hb-invite-email', 'newva@a.co');
    await ownerA.click('[data-act="hb-invite"]');
    ck('invite row created for new VA', await waitDB("select count(*) from org_invites where email='newva@a.co'", 1));
    // register the invited user in auth (as Supabase would on their first sign-in), then sign them in
    psql("insert into auth.users(id,email) values ('88888888-0000-0000-0000-0000000000aa','newva@a.co') on conflict do nothing");
    const ctxVA = await browser.newContext();
    let va = await signIn(ctxVA, 'newva@a.co');
    await sleep(800);
    ck('invited VA joins org A as agent', await waitDB("select role from profiles where email='newva@a.co'", 'agent'));
    ck('invited VA lands in org A', psql("select o.name from profiles p join orgs o on o.id=p.org_id where p.email='newva@a.co'") === 'A Co');

  } catch (e) {
    console.log('  EXCEPTION: ' + (e && e.stack || e));
    fail++;
  } finally {
    await browser.close();
  }
  console.log('\nE2E RESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
