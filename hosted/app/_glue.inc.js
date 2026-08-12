/* ============================================================================
   HOSTED GLUE  (injected inside the app IIFE by build_hosted.py)
   Turns the single-device localStorage app into a multi-user, synced,
   offline-capable client on top of Supabase. Has direct access to the app's
   internals: state, render, normalizeLead, toast, openModal, closeModal, esc,
   confirmModal, settings, loadSettings, checkReminders, I (icons).
   Relies on globals: supabase (vendored), Sync, DB, CONFIG.
   ============================================================================ */
var HB = {
  ready: false, entered: false, orgId: null, uid: null, role: 'owner',
  email: '', orgName: 'My Business', members: {}, client: null,
  online: (typeof navigator !== 'undefined') ? navigator.onLine : true,
  syncing: false, lastErr: null, _t: null, _pollTimer: null,

  // Debounced persist: mirror immediately, push the delta shortly after.
  persist: function () {
    if (!HB.ready) return;
    // stamp attribution on any freshly-created records
    (state.leads || []).forEach(function (l) {
      if (!l.createdBy) l.createdBy = HB.uid;
      l.updatedBy = HB.uid;
      (l.comms || []).forEach(function (c) { if (!c.createdBy) c.createdBy = HB.uid; });
    });
    HB._dirty = true;
    DB.saveLocal(state.leads);
    if (HB._t) clearTimeout(HB._t);
    HB._t = setTimeout(HB.flushNow, 300);
  },
  flushNow: function () {
    if (!HB.ready) return;
    HB.syncing = true; HB.paintStatus();
    DB.flush(state.leads).then(function (r) {
      HB.syncing = false;
      if (r && r.ok) { HB._dirty = false; HB.lastErr = null; }   // pushed or nothing to push
      else HB.lastErr = (r && r.reason) || null;                  // offline/error → stays dirty
      HB.online = DB.isOnline();
      HB.paintStatus();
    });
  },
  // Push pending writes; a no-op unless something is actually dirty + online.
  heartbeat: function () { if (HB.ready && HB.online && HB._dirty && !HB.syncing) HB.flushNow(); },

  paintStatus: function () {
    var pill = document.getElementById('hb-sync');
    if (pill) {
      var txt, cls;
      if (!HB.online) { txt = 'Offline'; cls = 'off'; }
      else if (HB.syncing) { txt = 'Saving…'; cls = 'busy'; }
      else if (HB.lastErr) { txt = 'Retrying'; cls = 'err'; }
      else { txt = 'Synced'; cls = 'ok'; }
      pill.textContent = txt;
      pill.className = 'hb-pill ' + cls;
    }
    var lbl = document.getElementById('hb-acct-label');
    if (lbl) lbl.textContent = HB.email ? (HB.email.split('@')[0]) : 'Account';
  }
};

/* ---------- UUIDs (Supabase primary keys are uuid) ---------- */
function hbUUID() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
    var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8); return v.toString(16);
  });
}

/* ---------- Auth gate UI ---------- */
function hbGateHTML(mode, msg) {
  var m = msg ? '<div class="hb-msg">' + esc(msg) + '</div>' : '';
  return '<div class="hb-auth"><div class="hb-card">' +
    '<div class="hb-logo">' + I.drop + '</div>' +
    '<h1>PowerWash CRM</h1>' +
    '<p class="hb-sub">Sign in to your account. Your leads sync across every device and back up automatically.</p>' +
    m +
    '<form id="hb-form" autocomplete="on">' +
      '<label class="hb-l">Email</label>' +
      '<input id="hb-email" type="email" inputmode="email" autocomplete="username" placeholder="you@example.com" />' +
      '<div id="hb-pwwrap" style="display:none">' +
        '<label class="hb-l">Password</label>' +
        '<input id="hb-pw" type="password" autocomplete="current-password" placeholder="Your password" />' +
      '</div>' +
      '<button type="submit" class="hb-btn" id="hb-go">Email me a magic link</button>' +
    '</form>' +
    '<button class="hb-link" id="hb-toggle">Use a password instead</button>' +
    '<div class="hb-foot">First time here? Just enter your email — we\'ll set up your account automatically.</div>' +
  '</div></div>';
}
function hbRenderGate(msg) {
  var app = document.getElementById('app');
  app.innerHTML = hbGateHTML('magic', msg);
  var usePw = false;
  var form = document.getElementById('hb-form');
  document.getElementById('hb-toggle').addEventListener('click', function () {
    usePw = !usePw;
    document.getElementById('hb-pwwrap').style.display = usePw ? 'block' : 'none';
    document.getElementById('hb-go').textContent = usePw ? 'Sign in' : 'Email me a magic link';
    this.textContent = usePw ? 'Use a magic link instead' : 'Use a password instead';
  });
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var email = (document.getElementById('hb-email').value || '').trim().toLowerCase();
    if (!email || email.indexOf('@') < 0) { toast('Enter a valid email', true); return; }
    var go = document.getElementById('hb-go'); go.disabled = true;
    if (usePw) {
      var pw = document.getElementById('hb-pw').value;
      HB.client.auth.signInWithPassword({ email: email, password: pw }).then(function (r) {
        go.disabled = false;
        if (r.error) { toast(r.error.message || 'Sign-in failed', true); }
      });
    } else {
      HB.client.auth.signInWithOtp({ email: email, options: { emailRedirectTo: location.href.split('#')[0].split('?')[0] } }).then(function (r) {
        go.disabled = false;
        if (r.error) { toast(r.error.message || 'Could not send link', true); }
        else hbRenderGate('Check your email — we sent a sign-in link to ' + email + '. Open it on this device.');
      });
    }
  });
}

/* ---------- Identity + entering the app ---------- */
function hbIdentityKey() { return 'pwcrm-identity:' + HB.uid; }
function hbReadIdentity() {
  try { return JSON.parse(localStorage.getItem(hbIdentityKey()) || 'null'); } catch (e) { return null; }
}
function hbApplyIdentity(id) {
  HB.orgId = id.orgId; HB.role = id.role || 'owner';
  HB.members = id.members || {}; HB.orgName = id.orgName || 'My Business';
}
// Resolve org/role/teammates. Online: via RPC (creates org / accepts invite),
// then cache it. Offline or transient failure: fall back to the cached identity
// so the app still boots and works from the local mirror.
function hbResolveIdentity() {
  var cached = hbReadIdentity();
  if (!HB.online && cached && cached.uid === HB.uid) { hbApplyIdentity(cached); return Promise.resolve(); }
  return HB.client.rpc('accept_invite_or_bootstrap', { p_name: 'My Business' }).then(function (r) {
    if (r.error) throw r.error;
    HB.orgId = r.data;
    return Promise.all([
      HB.client.from('profiles').select('*'),
      HB.client.from('orgs').select('*').eq('id', HB.orgId).maybeSingle()
    ]);
  }).then(function (rr) {
    var profs = rr[0].data || [];
    HB.members = {};
    profs.forEach(function (p) { HB.members[p.id] = p.name || p.email || 'Teammate'; });
    var me = profs.filter(function (p) { return p.id === HB.uid; })[0];
    HB.role = (me && me.role) || 'owner';
    if (rr[1].data && rr[1].data.name) HB.orgName = rr[1].data.name;
    try {
      localStorage.setItem(hbIdentityKey(), JSON.stringify({
        uid: HB.uid, orgId: HB.orgId, role: HB.role, members: HB.members, orgName: HB.orgName
      }));
    } catch (e) {}
  }).catch(function (e) {
    if (cached && cached.uid === HB.uid) { hbApplyIdentity(cached); return; } // degraded → use cache
    throw e;
  });
}

function hbEnterApp(session) {
  if (HB.entered) return;
  HB.entered = true;
  HB.online = (typeof navigator !== 'undefined') ? navigator.onLine : true;
  HB.uid = session.user.id;
  HB.email = session.user.email || '';
  loadSettings();
  hbInitSettingsDefaults();
  hbResolveIdentity().then(function () {
    return DB.boot(HB.client, { orgId: HB.orgId, uid: HB.uid, role: HB.role });
  }).then(function () {
    return DB.loadLocal();
  }).then(function (mirror) {
    if (Array.isArray(mirror)) state.leads = mirror.map(hbNorm);
    else state.leads = [];
    HB.ready = true;
    render();
    HB.paintStatus();
    checkReminders(false);
    if (!HB._pollTimer) {
      setInterval(function () { checkReminders(false); }, 60 * 60 * 1000);
      HB._pollTimer = setInterval(hbPull, 30000);
      setInterval(HB.heartbeat, 4000); // guarantees pending writes converge after reconnect
    }
    hbBindSync();
    return hbPull(); // reconcile with server
  }).catch(function (e) {
    var app = document.getElementById('app');
    app.innerHTML = hbGateHTML('magic', 'Could not load your account: ' + ((e && e.message) || e) + '. Please try again.');
    HB.entered = false;
    hbRenderGate('Could not load your account. Sign in to retry.');
  });
}

function hbNorm(l) {
  var n = normalizeLead(l);
  n.createdBy = l.createdBy || l.created_by || null;
  n.updatedBy = l.updatedBy || l.updated_by || null;
  return n;
}

// Pull server state, merge (last-write-wins), repaint.
function hbPull() {
  if (!HB.ready || !HB.online) return Promise.resolve();
  return DB.pullMerge(state.leads).then(function (merged) {
    state.leads = merged.map(hbNorm);
    DB.saveLocal(state.leads);
    render();
    HB.paintStatus();
  }).catch(function () { /* offline / transient — mirror still holds state */ });
}

function hbBindSync() {
  // repaint + reconcile whenever we regain focus or connectivity
  if (HB._bound) return; HB._bound = true;
  window.addEventListener('focus', hbPull);
  window.addEventListener('online', function () { HB.online = true; HB.paintStatus(); HB.flushNow(); hbPull(); });
  window.addEventListener('offline', function () { HB.online = false; HB.paintStatus(); });
  // best-effort realtime (works against real Supabase; harmless if unavailable)
  try {
    HB.client.channel('org-' + HB.orgId)
      .on('postgres_changes', { event: '*', schema: 'public', table: 'leads' }, function () { hbPullDebounced(); })
      .on('postgres_changes', { event: '*', schema: 'public', table: 'comms' }, function () { hbPullDebounced(); })
      .subscribe();
  } catch (e) {}
}
var _pullDeb = null;
function hbPullDebounced() { if (_pullDeb) clearTimeout(_pullDeb); _pullDeb = setTimeout(hbPull, 800); }

function hbInitSettingsDefaults() {
  if (!settings.templates) settings.templates = [];
  if (!settings.customServices) settings.customServices = [];
  if (!settings.customSources) settings.customSources = [];
  if (!settings.customPropertyTypes) settings.customPropertyTypes = [];
  if (settings.autoCadence === undefined) settings.autoCadence = true;
  if (!settings.tone) settings.tone = 'friendly';
}

/* ---------- Account / team modal ---------- */
// Returns a builder function (the app's openModal expects a function, matching
// leadModal/settingsModal), so the panel re-renders with fresh data each paint.
function hbAccountModal() { return function () {
  var teammates = Object.keys(HB.members).map(function (id) {
    var meTag = id === HB.uid ? ' <span class="hb-tag">you</span>' : '';
    return '<div class="hb-member">' + esc(HB.members[id]) + meTag + '</div>';
  }).join('');
  var inviteRow = HB.role === 'owner'
    ? '<div class="set-row"><div class="sr-txt"><div class="t">Invite a teammate</div><div class="d">They\'ll sign in with their own email and can add & edit leads. Only you (owner) can delete.</div></div></div>' +
      '<div class="field" style="display:flex;gap:8px"><input id="hb-invite-email" type="email" placeholder="teammate@email.com" style="flex:1"/><button class="btn btn-primary btn-sm" data-act="hb-invite">Invite</button></div>'
    : '';
  var roleLine = HB.role === 'owner' ? 'Owner — full access' : 'Agent — can add & edit, cannot delete';
  return '<div class="modal" role="dialog" aria-modal="true" aria-label="Account">' +
    '<div class="modal-head"><h2>Account</h2><button class="icon-btn x" data-close="1" aria-label="Close">' + I.x + '</button></div>' +
    '<div class="modal-body">' +
      '<div class="set-row"><div class="sr-txt"><div class="t">' + esc(HB.email) + '</div><div class="d">' + esc(roleLine) + '</div></div>' +
        '<span class="hb-pill ' + (HB.online ? 'ok' : 'off') + '">' + (HB.online ? 'Synced' : 'Offline') + '</span></div>' +
      '<div class="set-row"><div class="sr-txt"><div class="t">Your team</div><div class="d">' + (teammates || 'Just you for now.') + '</div></div></div>' +
      inviteRow +
      '<div class="set-row"><div class="sr-txt"><div class="t">Bring in leads from the free app</div><div class="d">Exported a backup file from the browser-only version? Import it here to load those leads into your account.</div></div><button class="btn btn-ghost btn-sm" data-act="hb-import">Import</button></div>' +
      '<div class="set-row"><div class="sr-txt"><div class="t">Export / back up</div><div class="d">Download all your leads as a file.</div></div><button class="btn btn-ghost btn-sm" data-act="export">Export</button></div>' +
      '<div class="set-row"><div class="sr-txt"><div class="t">Sign out</div><div class="d">You\'ll need your email to sign back in.</div></div><button class="btn btn-ghost btn-sm" data-act="hb-signout">Sign out</button></div>' +
    '</div></div>';
}; }

function hbInvite() {
  var el = document.getElementById('hb-invite-email');
  var email = (el && el.value || '').trim().toLowerCase();
  if (!email || email.indexOf('@') < 0) { toast('Enter a valid email', true); return; }
  HB.client.rpc('invite_teammate', { p_email: email, p_role: 'agent' }).then(function (r) {
    if (r.error) { toast(r.error.message || 'Could not invite', true); return; }
    toast('Invite sent to ' + email + '. They sign in with that email to join.');
    hbResolveIdentity().then(function () { closeModal(); openModal(hbAccountModal()); });
  });
}

function hbSignOut() {
  HB.client.auth.signOut().then(function () {
    HB.ready = false; HB.entered = false; state.leads = [];
    if (DB._reset) DB._reset();
    closeModal();
    hbRenderGate('Signed out. Enter your email to sign back in.');
  });
}

/* ---------- Import from the free app's JSON export ---------- */
function hbImportFree() {
  var inp = document.createElement('input');
  inp.type = 'file'; inp.accept = '.json,application/json';
  inp.addEventListener('change', function () {
    var f = inp.files && inp.files[0]; if (!f) return;
    var rd = new FileReader();
    rd.onload = function () {
      var data; try { data = JSON.parse(rd.result); } catch (e) { toast('That file isn\'t a valid backup', true); return; }
      var incoming = Array.isArray(data) ? data : (data && data.leads) || [];
      if (!incoming.length) { toast('No leads found in that file', true); return; }
      var added = 0;
      incoming.forEach(function (raw) {
        var l = hbNorm(raw);
        l.id = hbUUID();                       // free-app ids aren't uuids
        l.sample = false;
        l.createdBy = HB.uid;
        (l.comms || []).forEach(function (c) { c.id = hbUUID(); c.createdBy = HB.uid; });
        state.leads.push(l); added++;
      });
      save(); closeModal(); render();
      toast('Imported ' + added + ' lead' + (added === 1 ? '' : 's') + ' into your account');
    };
    rd.readAsText(f);
  });
  inp.click();
}

/* ---------- Hosted boot ---------- */
function boot() {
  try { var th = localStorage.getItem(THEME_KEY); if (th) document.documentElement.setAttribute('data-theme', th); } catch (e) {}
  if (!window.CONFIG || !CONFIG.configured()) {
    document.getElementById('app').innerHTML =
      '<div class="hb-auth"><div class="hb-card"><div class="hb-logo">' + I.drop + '</div>' +
      '<h1>Almost there</h1><p class="hb-sub">This app needs to be connected to your Supabase project. ' +
      'Open <code>config.js</code> and paste in your Project URL and anon key, then reload. ' +
      'Step-by-step instructions are in <code>DEPLOY.md</code>.</p></div></div>';
    return;
  }
  HB.client = supabase.createClient(CONFIG.url, CONFIG.anonKey, {
    auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true, storageKey: 'pwcrm-auth' }
  });
  HB.client.auth.getSession().then(function (res) {
    var s = res.data.session;
    if (s) hbEnterApp(s); else hbRenderGate();
  });
  HB.client.auth.onAuthStateChange(function (evt, s) {
    if (s && !HB.entered) hbEnterApp(s);
    else if (!s && HB.entered) { HB.entered = false; HB.ready = false; hbRenderGate(); }
  });
  // register the service worker for offline/installable
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(function () {});
  }
}
