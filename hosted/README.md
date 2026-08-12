# PowerWash CRM — hosted version (developer notes)

The **hosted** app: the proven single‑file CRM (`../index.html`) turned into a
multi‑user, cross‑device, offline‑capable client on top of Supabase
(Postgres + Auth + Row‑Level Security).

For the **non‑technical setup guide**, see **[DEPLOY.md](./DEPLOY.md)**.

## How it's built (and why)

The free app is the single source of truth for all CRM logic (pipeline,
follow‑up cadence engine, filters, messaging, etc.). Instead of forking 1,900
lines, **`build_hosted.py`** applies a small set of anchored transforms to it to
produce `app/index.html`:

- swaps the localStorage `load`/`save` for a Supabase‑backed data layer;
- swaps id generation to UUIDs (Postgres primary keys);
- adds an auth gate, owner/agent role gating, and per‑record attribution;
- injects the sync/offline glue and PWA wiring.

Re‑run it any time the free app changes: `python3 build_hosted.py`
(it's idempotent — same input yields byte‑identical output).

## Files

```
app/
  index.html          generated hosted app  (do not hand‑edit — edit _glue.inc.js / ../index.html)
  _glue.inc.js        hosted glue: auth, identity, sync bridge, account/team UI  (injected into the app)
  sync.js             PURE nested<->flat mapping + minimal‑diff sync engine (unit‑tested)
  db.js               offline‑first IndexedDB mirror + serialized push/pull to Supabase
  config.js           your Supabase URL + anon key (or pass via ?supabase_url=&supabase_key=)
  sw.js               service worker — cache‑first app shell, network‑only for API
  manifest.webmanifest, icons/   installable PWA
  vendor/supabase.js  vendored supabase-js UMD (self‑hosted so the app works offline)
supabase/migrations/
  0001_init.sql       tables + Row‑Level Security (multi‑tenant, owner/agent)
  0002_rpc_invites.sql signup bootstrap + teammate‑invite RPCs
build_hosted.py       generates app/index.html from ../index.html
test/                 see below
```

## Data model

- In memory the app keeps **nested** leads (each lead carries its `comms[]` and
  `followUp`). Postgres stores them **flat** across `leads` + `comms`.
  `sync.js` is the only bridge: `flatten` / `inflate` / `diff` / `mergeByUpdated`.
- **Sync model:** the IndexedDB mirror holds the current desired state plus the
  last snapshot confirmed on the server. Each push diffs current‑vs‑snapshot and
  sends only the delta; snapshot advances only on success, so offline/failed
  pushes self‑heal on the next attempt. Pull merges server rows last‑write‑wins
  on `updated`. **Push and pull are serialized** through one lock so a pull can
  never mistake an in‑flight push for a remote delete.
- **Security** is enforced in the database (RLS), not the UI: org isolation on
  every table; only owners can delete. The role gating in the UI is just UX.

## Testing (against a real stack, RLS enforced)

No cloud needed. Tests run the actual app in headless Chromium against native
Postgres + PostgREST, fronted by a tiny gateway that presents the slice of the
Supabase HTTP API supabase-js uses (`test/gateway.js`).

Bring up the fixtures (Postgres already running with the app DB):

```bash
# 1. schema + local auth shim
psql -h 127.0.0.1 -U postgres -d powerwash -f supabase/migrations/0001_init.sql
psql -h 127.0.0.1 -U postgres -d powerwash -f supabase/migrations/0002_rpc_invites.sql
psql -h 127.0.0.1 -U postgres -d powerwash -f test/local_bootstrap.sql   # local-only auth.uid() etc

# 2. data API + gateway + static server
postgrest /tmp/postgrest.conf &                       # :3999
GW_PORT=4001 node test/gateway.js &                   # :4001  (Supabase API shim)
( cd app && python3 -m http.server 4100 ) &           # :4100  (serves the app)

# 3. run everything
bash test/run_all.sh
```

### Suites

| Suite | What it proves |
|---|---|
| `sync_test.js` (28) | flatten/inflate round‑trip; diff produces minimal correct ops; LWW merge |
| `run_rls_tests.sh` (14) | tenant isolation; agent‑can't‑delete; owner‑can; cross‑tenant writes blocked |
| `e2e.js` (18) | auth, org bootstrap, create→sync, persist across reload, org sharing, role‑gated delete, tenant isolation, offline write→sync, invite flow |
| `e2e_pwa.js` (8) | service worker caches the shell; app boots with **no network**; migration import from the free app's JSON (UUID re‑issue + attribution) |
| `e2e_multi.js` (6) | two devices: create/edit(LWW)/comm/delete propagation; account‑switch leaks nothing across orgs |

**Production uses real Supabase** — the gateway and `local_bootstrap.sql` are
test‑only and are never deployed. The migrations in `supabase/migrations/` run
unchanged on Supabase cloud.
