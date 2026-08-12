-- LOCAL TEST ONLY — recreates the pieces Supabase provides (auth schema,
-- auth.uid(), anon/authenticated roles) so the real migration can be applied
-- and RLS tested exactly as it behaves on Supabase cloud. NOT deployed.

create extension if not exists pgcrypto;
create schema if not exists auth;

create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text
);

-- Supabase-style: the current user id comes from the request JWT's `sub` claim.
create or replace function auth.uid() returns uuid
  language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
create or replace function auth.role() returns text
  language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.role', true), ''), 'anon')
$$;

do $$ begin
  if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
end $$;
grant usage on schema auth to anon, authenticated;
grant select on auth.users to anon, authenticated;
