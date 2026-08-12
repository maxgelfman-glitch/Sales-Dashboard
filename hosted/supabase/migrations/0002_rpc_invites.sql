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
