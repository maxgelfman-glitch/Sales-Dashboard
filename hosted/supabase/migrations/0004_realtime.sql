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
