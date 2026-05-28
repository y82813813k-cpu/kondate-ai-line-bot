create table if not exists public.bot_state (
  key text primary key,
  value jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create or replace function public.set_bot_state_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists set_bot_state_updated_at on public.bot_state;

create trigger set_bot_state_updated_at
before update on public.bot_state
for each row
execute function public.set_bot_state_updated_at();

grant select, insert, update, delete on table public.bot_state to service_role;
