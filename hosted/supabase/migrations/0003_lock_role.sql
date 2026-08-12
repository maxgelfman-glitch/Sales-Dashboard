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
