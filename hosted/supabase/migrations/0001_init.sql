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
