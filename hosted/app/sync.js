/* PowerWash CRM — sync engine (UMD: works in Node tests and the browser).
 *
 * The app keeps leads in memory as NESTED objects (a lead carries its own
 * comms[] and followUp{}). Supabase stores them FLAT across two tables
 * (leads, comms). This module is the single bridge between those two shapes:
 *
 *   flatten(lead, ctx)        nested lead  -> {leadRow, commRows}
 *   inflate(leadRow, commRows) flat rows   -> nested lead
 *   diff(current, snapshot)   two nested arrays -> minimal set of DB ops
 *
 * All functions are PURE (no I/O, no clock, no globals) so the whole sync
 * contract can be unit-tested deterministically. The IndexedDB mirror and the
 * Supabase network calls live in db.js and consume these primitives.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Sync = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ---- field maps -------------------------------------------------------
  // Nested (camelCase, app) <-> flat (snake_case, Postgres columns).

  function numOrNull(v) {
    if (v === '' || v == null) return 0;
    var n = Number(v);
    return isNaN(n) ? 0 : n;
  }

  // Nested lead -> {leadRow, commRows}. ctx carries org_id + actor uid so
  // every row is stamped for RLS and attribution.
  function flatten(lead, ctx) {
    ctx = ctx || {};
    var org = ctx.orgId || null;
    var actor = ctx.uid || null;
    var fu = lead.followUp && lead.followUp.date ? lead.followUp : null;
    var leadRow = {
      id: lead.id,
      org_id: org,
      type: lead.type === 'commercial' ? 'commercial' : 'residential',
      name: (lead.name != null && String(lead.name).trim()) ? String(lead.name) : 'Unnamed lead',
      contact: lead.contact != null ? String(lead.contact) : '',
      phone: lead.phone != null ? String(lead.phone) : '',
      email: lead.email != null ? String(lead.email) : '',
      address: lead.address != null ? String(lead.address) : '',
      value: numOrNull(lead.value),
      source: lead.source != null && String(lead.source) ? String(lead.source) : 'Other',
      property_type: lead.propertyType != null ? String(lead.propertyType) : '',
      stage: lead.stage || 'new',
      confidence: lead.confidence || 'warm',
      notes: lead.notes != null ? String(lead.notes) : '',
      services: Array.isArray(lead.services) ? lead.services : [],
      followup_date: fu ? fu.date : null,
      followup_note: fu ? (fu.note != null ? String(fu.note) : '') : '',
      reclean: Number(lead.reclean) || 0,
      lost_reason: lead.lostReason != null ? String(lead.lostReason) : '',
      sample: !!lead.sample,
      created_by: lead.createdBy || actor,
      updated_by: actor,
      // client-authored timestamps let us do last-write-wins across devices
      created_at: lead.created ? new Date(lead.created).toISOString() : null,
      updated_at: lead.updated ? new Date(lead.updated).toISOString() : null
    };
    var commRows = (Array.isArray(lead.comms) ? lead.comms : []).map(function (c) {
      return {
        id: c.id,
        org_id: org,
        lead_id: lead.id,
        method: c.method || 'note',
        outcome: c.outcome != null ? String(c.outcome) : '',
        occurred_on: c.date || null,
        notes: c.notes != null ? String(c.notes) : '',
        created_by: c.createdBy || actor,
        // ts is the app's per-comm ordering key; keep it in created_at
        created_at: c.ts ? new Date(c.ts).toISOString() : null
      };
    });
    return { leadRow: leadRow, commRows: commRows };
  }

  // {leadRow, commRows} -> nested lead (inverse of flatten).
  function inflate(leadRow, commRows) {
    commRows = commRows || [];
    return {
      id: leadRow.id,
      type: leadRow.type === 'commercial' ? 'commercial' : 'residential',
      name: leadRow.name || 'Unnamed lead',
      contact: leadRow.contact || '',
      phone: leadRow.phone || '',
      email: leadRow.email || '',
      address: leadRow.address || '',
      value: leadRow.value != null ? leadRow.value : '',
      source: leadRow.source || 'Other',
      propertyType: leadRow.property_type || '',
      stage: leadRow.stage || 'new',
      confidence: leadRow.confidence || 'warm',
      notes: leadRow.notes || '',
      services: Array.isArray(leadRow.services) ? leadRow.services : [],
      comms: commRows.map(function (c) {
        return {
          id: c.id,
          method: c.method || 'note',
          date: c.occurred_on || '',
          notes: c.notes || '',
          outcome: c.outcome || '',
          ts: c.created_at ? new Date(c.created_at).getTime() : 0,
          createdBy: c.created_by || null
        };
      }).sort(function (a, b) { return a.ts - b.ts; }),
      followUp: leadRow.followup_date ? { date: leadRow.followup_date, note: leadRow.followup_note || '' } : null,
      reclean: Number(leadRow.reclean) || 0,
      lostReason: leadRow.lost_reason || '',
      sample: !!leadRow.sample,
      createdBy: leadRow.created_by || null,
      updatedBy: leadRow.updated_by || null,
      created: leadRow.created_at ? new Date(leadRow.created_at).getTime() : Date.now(),
      updated: leadRow.updated_at ? new Date(leadRow.updated_at).getTime() : Date.now()
    };
  }

  // Stable identity signature of a lead row (minus volatile audit cols) so we
  // can tell "actually changed" from "re-saved identically".
  function leadSig(row) {
    var r = Object.assign({}, row);
    delete r.updated_by; delete r.updated_at; // audit-only churn shouldn't force a write on its own
    return JSON.stringify(r);
  }
  function commSig(row) {
    var r = Object.assign({}, row);
    return JSON.stringify(r);
  }

  // diff(current, snapshot): both are arrays of NESTED leads. Returns the
  // minimal set of DB operations to turn `snapshot` into `current`.
  // ctx stamps org/uid onto flattened rows.
  function diff(current, snapshot, ctx) {
    var curFlat = current.map(function (l) { return flatten(l, ctx); });
    var snapFlat = (snapshot || []).map(function (l) { return flatten(l, ctx); });

    var snapLeadById = {}, snapCommById = {};
    snapFlat.forEach(function (f) {
      snapLeadById[f.leadRow.id] = f.leadRow;
      f.commRows.forEach(function (c) { snapCommById[c.id] = c; });
    });
    var curLeadIds = {}, curCommIds = {};

    var leadUpserts = [], commUpserts = [], leadDeletes = [], commDeletes = [];

    curFlat.forEach(function (f) {
      curLeadIds[f.leadRow.id] = true;
      var prev = snapLeadById[f.leadRow.id];
      if (!prev || leadSig(prev) !== leadSig(f.leadRow)) leadUpserts.push(f.leadRow);
      f.commRows.forEach(function (c) {
        curCommIds[c.id] = true;
        var pc = snapCommById[c.id];
        if (!pc || commSig(pc) !== commSig(c)) commUpserts.push(c);
      });
    });

    Object.keys(snapLeadById).forEach(function (id) {
      if (!curLeadIds[id]) leadDeletes.push(id);
    });
    Object.keys(snapCommById).forEach(function (id) {
      // a comm whose parent lead was deleted is removed by ON DELETE CASCADE;
      // only enqueue an explicit comm delete when the parent still exists.
      if (!curCommIds[id] && curLeadIds[snapCommById[id].lead_id]) commDeletes.push(id);
    });

    return {
      leadUpserts: leadUpserts,
      commUpserts: commUpserts,
      leadDeletes: leadDeletes,
      commDeletes: commDeletes,
      empty: !leadUpserts.length && !commUpserts.length && !leadDeletes.length && !commDeletes.length
    };
  }

  // Merge server leads (nested) into local leads (nested), last-write-wins by
  // `updated`. Returns the merged array. Deletions are handled separately by
  // the caller (a lead absent from the server AND not locally-dirty is dropped).
  function mergeByUpdated(local, server) {
    var byId = {};
    (local || []).forEach(function (l) { byId[l.id] = l; });
    (server || []).forEach(function (s) {
      var l = byId[s.id];
      if (!l || (s.updated || 0) >= (l.updated || 0)) byId[s.id] = s;
    });
    return Object.keys(byId).map(function (k) { return byId[k]; });
  }

  return {
    flatten: flatten,
    inflate: inflate,
    diff: diff,
    mergeByUpdated: mergeByUpdated,
    _leadSig: leadSig
  };
});
