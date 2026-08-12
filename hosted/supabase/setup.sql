-- PowerWash CRM — complete database setup (run this ONCE, all at once).
-- This is migrations 0001–0004 combined into a single file so you only
-- have to paste and Run one time. Safe to re-run if needed.


-- ========================================================================
-- migrations/0001_init.sql
-- ========================================================================
-- PowerWash CRM — hosted schema + row-level security
-- Multi-tenant (per-business "org"), owner/agent roles, per-record attribution.
-- Runs on Supabase cloud (auth.users + auth.uid() already provided there).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
create table if not exists public.orgs (
  id          uuid primary key default gen_random_uuid(),
  name        text not null default 'My Business',
  created_at  timestamptz not null default now()
);

-- one row per user, linking them to an org with a role
create table if not exists public.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  org_id      uuid not null references public.orgs(id) on delete cascade,
  role        text not null default 'owner' check (role in ('owner','agent')),
  name        text,
  email       text,
  created_at  timestamptz not null default now()
);
create index if not exists profiles_org_idx on public.profiles(org_id);

create table if not exists public.leads (
  id            uuid primary key default gen_random_uuid(),
  org_id        uuid not null references public.orgs(id) on delete cascade,
  type          text not null default 'residential' check (type in ('residential','commercial')),
  name          text not null default 'Unnamed lead',
  contact       text default '',
  phone         text default '',
  email         text default '',
  address       text default '',
  value         numeric default 0,
  source        text default 'Other',
  property_type text default '',
  stage         text not null default 'new',
  confidence    text not null default 'warm' check (confidence in ('hot','warm','cold')),
  notes         text default '',
  services      jsonb not null default '[]'::jsonb,
  followup_date date,
  followup_note text default '',
  reclean       int not null default 0,
  lost_reason   text default '',
  sample        boolean not null default false,
  created_by    uuid,
  updated_by    uuid,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists leads_org_idx on public.leads(org_id);
create index if not exists leads_stage_idx on public.leads(org_id, stage);

create table if not exists public.comms (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.orgs(id) on delete cascade,
  lead_id     uuid not null references public.leads(id) on delete cascade,
  method      text not null default 'note',
  outcome     text default '',
  occurred_on date not null default current_date,
  notes       text default '',
  created_by  uuid,
  created_at  timestamptz not null default now()
);
create index if not exists comms_lead_idx on public.comms(lead_id);
create index if not exists comms_org_idx on public.comms(org_id);

-- ---------------------------------------------------------------------------
-- Helper functions (which org / role is the current user?)  SECURITY DEFINER
-- so they can read profiles without tripping profiles' own RLS.
-- ---------------------------------------------------------------------------
create or replace function public.my_org() returns uuid
  language sql stable security definer set search_path = public as $$
  select org_id from public.profiles where id = auth.uid()
$$;

create or replace function public.my_role() returns text
  language sql stable security definer set search_path = public as $$
  select role from public.profiles where id = auth.uid()
$$;

-- keep updated_at fresh
create or replace function public.touch_updated_at() returns trigger
  language plpgsql as $$
begin new.updated_at = now(); return new; end $$;
drop trigger if exists leads_touch on public.leads;
create trigger leads_touch before update on public.leads
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------
alter table public.orgs     enable row level security;
alter table public.profiles enable row level security;
alter table public.leads    enable row level security;
alter table public.comms    enable row level security;

-- ORGS: members see & (owners) rename their org; any authed user may create one (signup)
drop policy if exists orgs_select on public.orgs;
create policy orgs_select on public.orgs for select using (id = public.my_org());
drop policy if exists orgs_insert on public.orgs;
create policy orgs_insert on public.orgs for insert with check (auth.uid() is not null);
drop policy if exists orgs_update on public.orgs;
create policy orgs_update on public.orgs for update using (id = public.my_org() and public.my_role() = 'owner');

-- PROFILES: you can see everyone in your org; create your own row (signup) or,
-- as owner, invite teammates into your org; update self or (owner) teammates.
drop policy if exists profiles_select on public.profiles;
create policy profiles_select on public.profiles for select using (org_id = public.my_org());
drop policy if exists profiles_insert on public.profiles;
create policy profiles_insert on public.profiles for insert
  with check (id = auth.uid() or (org_id = public.my_org() and public.my_role() = 'owner'));
drop policy if exists profiles_update on public.profiles;
create policy profiles_update on public.profiles for update
  using (id = auth.uid() or (org_id = public.my_org() and public.my_role() = 'owner'));

-- LEADS: whole org reads & writes; only OWNER may delete.
drop policy if exists leads_select on public.leads;
create policy leads_select on public.leads for select using (org_id = public.my_org());
drop policy if exists leads_insert on public.leads;
create policy leads_insert on public.leads for insert with check (org_id = public.my_org());
drop policy if exists leads_update on public.leads;
create policy leads_update on public.leads for update using (org_id = public.my_org()) with check (org_id = public.my_org());
drop policy if exists leads_delete on public.leads;
create policy leads_delete on public.leads for delete using (org_id = public.my_org() and public.my_role() = 'owner');

-- COMMS: whole org reads & writes; only OWNER may delete a logged contact.
drop policy if exists comms_select on public.comms;
create policy comms_select on public.comms for select using (org_id = public.my_org());
drop policy if exists comms_insert on public.comms;
create policy comms_insert on public.comms for insert with check (org_id = public.my_org());
drop policy if exists comms_update on public.comms;
create policy comms_update on public.comms for update using (org_id = public.my_org()) with check (org_id = public.my_org());
drop policy if exists comms_delete on public.comms;
create policy comms_delete on public.comms for delete using (org_id = public.my_org() and public.my_role() = 'owner');

-- Supabase exposes tables to the API via the anon/authenticated roles.
grant usage on schema public to anon, authenticated;
grant all on all tables in schema public to authenticated;
grant select on all tables in schema public to anon;  -- RLS still gates every row

-- ========================================================================
-- migrations/0002_rpc_invites.sql
-- ========================================================================
-- PowerWash CRM — signup bootstrap + teammate invites
-- Atomic SECURITY DEFINER helpers so a new user reliably lands in the right
-- org (their own, or one they were invited to) without tripping the RLS-on-
-- RETURNING problem you hit when my_org() is still null mid-signup.

-- Pending invitations an owner extends to a teammate (VA).
create table if not exists public.org_invites (
  id          uuid primary key default gen_random_uuid(),
  org_id      uuid not null references public.orgs(id) on delete cascade,
  email       text not null,
  role        text not null default 'agent' check (role in ('owner','agent')),
  accepted    boolean not null default false,
  created_by  uuid,
  created_at  timestamptz not null default now()
);
create index if not exists org_invites_email_idx on public.org_invites(lower(email));
create unique index if not exists org_invites_org_email_uq on public.org_invites(org_id, lower(email));

alter table public.org_invites enable row level security;

-- Owners manage invites for their own org; members may read their org's invites.
drop policy if exists invites_select on public.org_invites;
create policy invites_select on public.org_invites for select using (org_id = public.my_org());
drop policy if exists invites_insert on public.org_invites;
create policy invites_insert on public.org_invites for insert
  with check (org_id = public.my_org() and public.my_role() = 'owner');
drop policy if exists invites_delete on public.org_invites;
create policy invites_delete on public.org_invites for delete
  using (org_id = public.my_org() and public.my_role() = 'owner');

grant all on public.org_invites to authenticated;

-- Called once per session right after auth. Idempotent: returns the caller's
-- org, creating it (or honoring a pending invite) on first run.
create or replace function public.accept_invite_or_bootstrap(p_name text default 'My Business')
returns uuid language plpgsql security definer set search_path = public as $$
declare
  v_uid   uuid := auth.uid();
  v_email text;
  v_org   uuid;
  v_role  text;
begin
  if v_uid is null then raise exception 'not authenticated'; end if;

  -- already set up?
  select org_id into v_org from public.profiles where id = v_uid;
  if v_org is not null then return v_org; end if;

  select email into v_email from auth.users where id = v_uid;

  -- pending invite for this email?
  select org_id, role into v_org, v_role
    from public.org_invites
    where lower(email) = lower(v_email) and accepted = false
    order by created_at limit 1;

  if v_org is not null then
    insert into public.profiles(id, org_id, role, name, email)
      values (v_uid, v_org, coalesce(v_role,'agent'), null, v_email);
    update public.org_invites set accepted = true
      where org_id = v_org and lower(email) = lower(v_email);
    return v_org;
  end if;

  -- otherwise start a brand-new org as its owner
  insert into public.orgs(name) values (coalesce(nullif(p_name,''),'My Business')) returning id into v_org;
  insert into public.profiles(id, org_id, role, name, email)
    values (v_uid, v_org, 'owner', null, v_email);
  return v_org;
end $$;

revoke all on function public.accept_invite_or_bootstrap(text) from public;
grant execute on function public.accept_invite_or_bootstrap(text) to authenticated;

-- Owner invites a teammate by email (defaults to agent role).
create or replace function public.invite_teammate(p_email text, p_role text default 'agent')
returns uuid language plpgsql security definer set search_path = public as $$
declare v_org uuid; v_role text; v_id uuid;
begin
  v_org := public.my_org();
  if v_org is null then raise exception 'no org'; end if;
  if public.my_role() <> 'owner' then raise exception 'only owner can invite'; end if;
  if p_email is null or position('@' in p_email) = 0 then raise exception 'valid email required'; end if;
  v_role := case when p_role = 'owner' then 'owner' else 'agent' end;
  insert into public.org_invites(org_id, email, role, created_by)
    values (v_org, lower(p_email), v_role, auth.uid())
    on conflict (org_id, lower(email)) do update set role = excluded.role, accepted = false
    returning id into v_id;
  return v_id;
end $$;

revoke all on function public.invite_teammate(text, text) from public;
grant execute on function public.invite_teammate(text, text) to authenticated;

-- ========================================================================
-- migrations/0003_lock_role.sql
-- ========================================================================
-- PowerWash CRM — close a privilege-escalation hole.
-- profiles_update lets a user edit their own row (name/email). But an RLS
-- WITH CHECK cannot compare OLD vs NEW, so nothing stopped an agent from
-- flipping their OWN role to 'owner'. Enforce role/org immutability with a
-- BEFORE UPDATE trigger: only an owner of the same org may change a role, and
-- a profile can never be moved to a different org via update.

create or replace function public.enforce_profile_guard() returns trigger
  language plpgsql security definer set search_path = public as $$
begin
  -- moving a profile between orgs is never allowed through a normal update
  if new.org_id is distinct from old.org_id then
    raise exception 'cannot change org_id';
  end if;
  -- a role change is allowed only when the caller is an owner of this org
  if new.role is distinct from old.role then
    if public.my_org() is distinct from old.org_id or public.my_role() <> 'owner' then
      raise exception 'only an owner can change a role';
    end if;
  end if;
  return new;
end $$;

drop trigger if exists profiles_guard on public.profiles;
create trigger profiles_guard before update on public.profiles
  for each row execute function public.enforce_profile_guard();

-- ========================================================================
-- migrations/0004_realtime.sql
-- ========================================================================
-- PowerWash CRM — turn on live cross-device updates.
-- The app subscribes to realtime changes on leads/comms so an edit on your
-- phone shows on your laptop within a second (it also falls back to polling,
-- so this is an enhancement, not a requirement). Supabase exposes realtime via
-- the `supabase_realtime` publication; add our tables to it. Guarded so this
-- file is a harmless no-op on a plain Postgres that has no such publication
-- (e.g. the local test database).
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    -- add each table only if it isn't already in the publication
    if not exists (
      select 1 from pg_publication_tables
      where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'leads'
    ) then execute 'alter publication supabase_realtime add table public.leads'; end if;

    if not exists (
      select 1 from pg_publication_tables
      where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'comms'
    ) then execute 'alter publication supabase_realtime add table public.comms'; end if;
  end if;
end $$;
