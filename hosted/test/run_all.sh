#!/usr/bin/env bash
# One command to run the whole hosted test stack against the local
# Postgres + PostgREST + gateway + static-server fixtures.
#
# Prereqs (started by the harness during development; see hosted/README.md):
#   - Postgres on :5432 (db powerwash) with migrations + local_bootstrap applied
#   - PostgREST on :3999
#   - gateway on :4001   (node test/gateway.js with GW_PORT=4001)
#   - static server on :4100  (python3 -m http.server 4100 in app/)
set -u
cd "$(dirname "$0")/.."
export PGPASSWORD=postgres
PSQL="psql -h 127.0.0.1 -U postgres -d powerwash -qAt"
OWNA=11111111-0000-0000-0000-000000000001
AGTA=11111111-0000-0000-0000-000000000002
OWNB=22222222-0000-0000-0000-000000000001
ORGA=aaaaaaaa-0000-0000-0000-000000000001
ORGB=bbbbbbbb-0000-0000-0000-000000000002
reseed() {
$PSQL >/dev/null <<SQL
truncate public.comms, public.leads, public.profiles, public.orgs, public.org_invites cascade;
delete from auth.users;
insert into auth.users(id,email) values ('$OWNA','owner@a.co'),('$AGTA','agent@a.co'),('$OWNB','owner@b.co');
insert into public.orgs(id,name) values ('$ORGA','A Co'),('$ORGB','B Co');
insert into public.profiles(id,org_id,role,name,email) values
  ('$OWNA','$ORGA','owner','Max','owner@a.co'),
  ('$AGTA','$ORGA','agent','VA','agent@a.co'),
  ('$OWNB','$ORGB','owner','Rival','owner@b.co');
SQL
}
rc=0
echo "== sync unit tests =="; node test/sync_test.js || rc=1
echo; echo "== RLS security tests =="; bash test/run_rls_tests.sh || rc=1
echo; echo "== app E2E (auth, sync, roles, tenant isolation, offline, invite) =="; reseed; node test/e2e.js || rc=1
echo; echo "== PWA offline-shell + migration import =="; reseed; node test/e2e_pwa.js || rc=1
echo; echo "== multi-device (propagation, LWW, delete, account switch) =="; reseed; node test/e2e_multi.js || rc=1
echo; echo "== stress (bulk sync, rapid edits, free-app smoke) =="; reseed; node test/e2e_stress.js || rc=1
echo; echo "== conflict (offline edit vs remote delete) =="; reseed; node test/e2e_conflict.js || rc=1
echo
[ "$rc" = 0 ] && echo "ALL HOSTED TESTS PASSED" || echo "SOME HOSTED TESTS FAILED"
exit $rc
