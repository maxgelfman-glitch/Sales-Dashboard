/* PowerWash CRM — browser data layer.
 *
 * Bridges the app's in-memory nested leads to Supabase, with an offline-first
 * IndexedDB mirror and a self-healing push model:
 *
 *   - The mirror always holds the current desired state + the last snapshot
 *     that was confirmed written to the server.
 *   - Each push diffs current vs snapshot (Sync.diff) and sends only the delta.
 *     On success, snapshot := current. If offline or a call fails, snapshot
 *     stays behind, so the NEXT push re-diffs and catches everything up.
 *     This is idempotent and needs no fragile per-op queue.
 *   - Pull merges server rows into local by last-write-wins on `updated`.
 *
 * Depends on: window.Sync (sync.js), window.supabase (vendored UMD).
 */
(function (root) {
  'use strict';
  var Sync = root.Sync;

  // ---- tiny IndexedDB key/value wrapper --------------------------------
  var DB_NAME = 'pwcrm', STORE = 'kv', DB_VER = 1;
  function openIDB() {
    return new Promise(function (res, rej) {
      var rq = indexedDB.open(DB_NAME, DB_VER);
      rq.onupgradeneeded = function () {
        var db = rq.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      rq.onsuccess = function () { res(rq.result); };
      rq.onerror = function () { rej(rq.error); };
    });
  }
  function idbGet(key) {
    return openIDB().then(function (db) {
      return new Promise(function (res, rej) {
        var tx = db.transaction(STORE, 'readonly').objectStore(STORE).get(key);
        tx.onsuccess = function () { res(tx.result); };
        tx.onerror = function () { rej(tx.error); };
      });
    });
  }
  function idbSet(key, val) {
    return openIDB().then(function (db) {
      return new Promise(function (res, rej) {
        var tx = db.transaction(STORE, 'readwrite').objectStore(STORE).put(val, key);
        tx.onsuccess = function () { res(); };
        tx.onerror = function () { rej(tx.error); };
      });
    });
  }

  // ---- state -----------------------------------------------------------
  var client = null;           // supabase client
  var ctx = { orgId: null, uid: null, role: 'owner' };
  var snapshot = [];           // last state confirmed on server (nested[])
  var online = true;   // push/pull mutual exclusion is handled by serialize()

  function keyFor(suffix) { return (ctx.orgId || 'anon') + ':' + suffix; }

  // ---- pull: server -> nested[] ---------------------------------------
  function pull() {
    if (!client) return Promise.resolve([]);
    return Promise.all([
      client.from('leads').select('*'),
      client.from('comms').select('*')
    ]).then(function (rr) {
      if (rr[0].error) throw rr[0].error;
      if (rr[1].error) throw rr[1].error;
      var leadRows = rr[0].data || [], commRows = rr[1].data || [];
      var byLead = {};
      commRows.forEach(function (c) { (byLead[c.lead_id] = byLead[c.lead_id] || []).push(c); });
      return leadRows.map(function (lr) { return Sync.inflate(lr, byLead[lr.id] || []); });
    });
  }

  // ---- push: apply a diff to Supabase in FK-safe order ----------------
  function applyDiff(ops) {
    var chain = Promise.resolve();
    if (ops.leadUpserts.length) chain = chain.then(function () {
      return client.from('leads').upsert(ops.leadUpserts).then(thrle);
    });
    if (ops.commUpserts.length) chain = chain.then(function () {
      return client.from('comms').upsert(ops.commUpserts).then(thrle);
    });
    if (ops.commDeletes.length) chain = chain.then(function () {
      return client.from('comms').delete().in('id', ops.commDeletes).then(thrle);
    });
    if (ops.leadDeletes.length) chain = chain.then(function () {
      return client.from('leads').delete().in('id', ops.leadDeletes).then(thrle);
    });
    return chain;
  }
  function thrle(r) { if (r && r.error) throw r.error; return r; }

  // ---- operation lock --------------------------------------------------
  // push (flush) and pull (pullMerge) BOTH read+advance `snapshot` and rewrite
  // the mirror, so they must never interleave. A pull that fetched a stale
  // server view while a push was mid-flight would wrongly treat a just-pushed
  // lead as "deleted remotely". Serialize every push/pull through one chain.
  var opChain = Promise.resolve();
  function serialize(fn) {
    var run = opChain.then(fn, fn);
    opChain = run.then(function () {}, function () {});
    return run;
  }

  // ---- public API ------------------------------------------------------
  var API = {
    // called after auth resolves; sets identity + loads the local mirror
    boot: function (supabaseClient, identity) {
      client = supabaseClient;
      ctx.orgId = identity.orgId; ctx.uid = identity.uid; ctx.role = identity.role || 'owner';
      online = (typeof navigator === 'undefined') ? true : navigator.onLine;
      if (typeof root.addEventListener === 'function') {
        root.addEventListener('online', function () { online = true; API.flush(); });
        root.addEventListener('offline', function () { online = false; });
      }
      return idbGet(keyFor('snapshot')).then(function (s) { snapshot = s || []; });
    },

    role: function () { return ctx.role; },
    uid: function () { return ctx.uid; },
    isOnline: function () { return online; },

    // instant local read (offline-first paint)
    loadLocal: function () {
      return idbGet(keyFor('leads')).then(function (v) { return v || null; });
    },
    saveLocal: function (leads) { return idbSet(keyFor('leads'), leads); },

    // Persist nested leads: write mirror now, then try to push the delta.
    // Never throws to the caller — offline just leaves work for flush().
    persist: function (leads) {
      return API.saveLocal(leads).then(function () { return API.flush(leads); });
    },

    // Diff current (or last mirror) vs snapshot and push; snapshot advances
    // only on full success, so failures self-heal on the next call.
    flush: function (leads) {
      if (!client) return Promise.resolve({ ok: false, reason: 'no-client' });
      return serialize(function () {
        var run = leads ? Promise.resolve(leads) : API.loadLocal();
        return run.then(function (cur) {
          cur = cur || [];
          var ops = Sync.diff(cur, snapshot, ctx);
          if (ops.empty) return { ok: true, empty: true };
          if (!online) return { ok: false, reason: 'offline' };
          return applyDiff(ops).then(function () {
            snapshot = JSON.parse(JSON.stringify(cur));
            return idbSet(keyFor('snapshot'), snapshot).then(function () {
              return { ok: true, pushed: ops };
            });
          }).catch(function (e) {
            online = (typeof navigator === 'undefined') ? true : navigator.onLine;
            return { ok: false, reason: 'error', error: (e && e.message) || String(e) };
          });
        });
      });
    },

    // Pull server state and merge into `local` (LWW). Also drops leads that
    // the server no longer has AND that aren't waiting to be pushed (i.e.
    // they were deleted on another device), so deletes propagate.
    pullMerge: function (localHint) {
      return serialize(function () {
        // read the freshest mirror INSIDE the lock (not the caller's snapshot),
        // so a lead added between call and execution is never dropped.
        return API.loadLocal().then(function (local) {
          if (!local) local = localHint || [];
          return pull().then(function (server) {
          var merged = Sync.mergeByUpdated(local || [], server);
          var serverIds = {}; server.forEach(function (s) { serverIds[s.id] = true; });
          var snapIds = {}; snapshot.forEach(function (s) { snapIds[s.id] = true; });
          // keep a lead if: server has it, OR it's brand-new locally (never
          // synced). Because this runs serialized after any in-flight push,
          // `snapshot` and `server` are consistent — a lead in snapshot but not
          // on the server was genuinely deleted on another device.
          merged = merged.filter(function (l) { return serverIds[l.id] || !snapIds[l.id]; });
          return merged;
          });
        });
      });
    },

    _reset: function () { snapshot = []; client = null; }
  };

  root.DB = API;
})(typeof self !== 'undefined' ? self : this);
