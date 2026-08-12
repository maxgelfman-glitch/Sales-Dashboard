#!/usr/bin/env bash
# Row-Level-Security pressure test: tenant isolation + owner/agent permissions.
# Simulates authenticated Supabase users by setting the JWT `sub` claim and the
# `authenticated` role (exactly how Supabase enforces RLS in production).
set -u
export PGPASSWORD=postgres
PSQL="psql -h 127.0.0.1 -U postgres -d powerwash -qAt"
pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "  PASS: $1"; pass=$((pass+1)); else echo "  FAIL: $1 (got '$2', want '$3')"; fail=$((fail+1)); fi; }

# fixed uuids
ORGA=aaaaaaaa-0000-0000-0000-000000000001
ORGB=bbbbbbbb-0000-0000-0000-000000000002
OWNA=11111111-0000-0000-0000-000000000001
AGTA=11111111-0000-0000-0000-000000000002
OWNB=22222222-0000-0000-0000-000000000001
LEADA=cccccccc-0000-0000-0000-00000000000a
LEADB=dddddddd-0000-0000-0000-00000000000b
COMMA=eeeeeeee-0000-0000-0000-00000000000c

# ---- seed as superuser (bypasses RLS) ----
$PSQL >/dev/null <<SQL
truncate public.comms, public.leads, public.profiles, public.orgs cascade;
delete from auth.users;
insert into auth.users(id) values ('$OWNA'),('$AGTA'),('$OWNB');
insert into public.orgs(id,name) values ('$ORGA','A Co'),('$ORGB','B Co');
insert into public.profiles(id,org_id,role,name) values
  ('$OWNA','$ORGA','owner','Owner A'),
  ('$AGTA','$ORGA','agent','Agent A'),
  ('$OWNB','$ORGB','owner','Owner B');
insert into public.leads(id,org_id,name,value,stage,created_by) values
  ('$LEADA','$ORGA','Lead A1',5000,'quoted','$OWNA'),
  ('$LEADB','$ORGB','Lead B1',9000,'new','$OWNB');
insert into public.comms(id,org_id,lead_id,method,created_by) values
  ('$COMMA','$ORGA','$LEADA','call','$OWNA');
SQL

# helper: run a query as a given user (authenticated role + jwt sub)
as_user(){ local uid="$1"; local q="$2"; $PSQL <<SQL
select set_config('request.jwt.claim.sub','$uid',false);
set role authenticated;
$q
SQL
}

echo "== Tenant isolation =="
ck "Owner A sees exactly their 1 lead"        "$(as_user $OWNA 'select count(*) from public.leads;'|tail -1)" "1"
ck "Owner A sees Lead A1 (not B)"             "$(as_user $OWNA "select name from public.leads;"|tail -1)" "Lead A1"
ck "Agent A sees the org lead"               "$(as_user $AGTA 'select count(*) from public.leads;'|tail -1)" "1"
ck "Owner B sees exactly their 1 lead"        "$(as_user $OWNB 'select count(*) from public.leads;'|tail -1)" "1"
ck "Owner B sees Lead B1 (not A)"             "$(as_user $OWNB 'select name from public.leads;'|tail -1)" "Lead B1"
ck "Owner B cannot see Org A comms"           "$(as_user $OWNB 'select count(*) from public.comms;'|tail -1)" "0"

echo "== Agent permissions =="
ck "Agent A CAN insert a lead in own org"     "$(as_user $AGTA "insert into public.leads(org_id,name) values ('$ORGA','Agent Added') returning 'ok';"|tail -1)" "ok"
ck "Agent A CAN update a lead"                "$(as_user $AGTA "update public.leads set notes='x' where id='$LEADA' returning 'ok';"|tail -1)" "ok"
# agent delete should affect 0 rows (RLS delete policy owner-only)
ck "Agent A CANNOT delete a lead"             "$(as_user $AGTA "with d as (delete from public.leads where id='$LEADA' returning 1) select count(*) from d;"|tail -1)" "0"
ck "Agent A CANNOT delete a comm"             "$(as_user $AGTA "with d as (delete from public.comms where id='$COMMA' returning 1) select count(*) from d;"|tail -1)" "0"

echo "== Owner permissions =="
ck "Owner A CAN delete a comm"                "$(as_user $OWNA "with d as (delete from public.comms where id='$COMMA' returning 1) select count(*) from d;"|tail -1)" "1"
ck "Owner A CAN delete a lead"                "$(as_user $OWNA "with d as (delete from public.leads where id='$LEADA' returning 1) select count(*) from d;"|tail -1)" "1"

echo "== Cross-tenant write blocked =="
# Owner B updating Org A lead -> 0 rows (RLS hides it)
ck "Owner B CANNOT update Org A lead"         "$(as_user $OWNB "with u as (update public.leads set notes='hack' where name='Agent Added' returning 1) select count(*) from u;"|tail -1)" "0"
# Agent A inserting into Org B -> blocked (with check) -> error captured as empty
INS_ERR="$(as_user $AGTA "insert into public.leads(org_id,name) values ('$ORGB','cross') returning 'ok';" 2>&1 | grep -c 'row-level security')"
ck "Agent A CANNOT insert into another org"   "$INS_ERR" "1"

echo "== Privilege escalation blocked =="
# Agent must NOT be able to promote themselves to owner (role/org locked by trigger).
esc="$(as_user $AGTA "update public.profiles set role='owner' where id='$AGTA' returning 'ok';" 2>&1 | grep -c 'only an owner can change a role')"
ck "Agent CANNOT self-promote to owner"       "$esc" "1"
ck "Agent role unchanged after attempt"       "$(as_user $OWNA "select role from public.profiles where id='$AGTA';"|tail -1)" "agent"
# Owner CAN change a teammate's role (legitimate path still works).
ck "Owner CAN change a teammate role"          "$(as_user $OWNA "update public.profiles set role='owner' where id='$AGTA' returning 'ok';"|tail -1)" "ok"
# Nobody can move a profile to another org.
orgmove="$(as_user $OWNA "update public.profiles set org_id='$ORGB' where id='$OWNA' returning 'ok';" 2>&1 | grep -c 'cannot change org_id')"
ck "Cannot move a profile to another org"      "$orgmove" "1"

echo ""
echo "RLS RESULT: $pass passed, $fail failed"
[ "$fail" = "0" ] && echo "ALL RLS TESTS PASS" || echo "RLS FAILURES PRESENT"
exit $fail
