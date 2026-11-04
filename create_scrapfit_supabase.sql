-- ScrapFit: initial Supabase cloud-storage setup for the current Streamlit app.
-- Run this entire script once in Supabase Dashboard > SQL Editor > New query.
-- The app uses a SERVER-SIDE Supabase secret key. Do not put that key in browser code.

create table if not exists public.scrapfit_documents (
    doc_key text primary key check (doc_key in ('inventory', 'users', 'transactions')),
    payload jsonb not null,
    updated_at timestamptz not null default now()
);

-- Only the Streamlit server's Supabase secret key (service_role) accesses this table.
-- No direct anon/authenticated access is granted. The Streamlit app keeps the existing
-- ScrapFit login and ownership checks during this migration stage.
alter table public.scrapfit_documents enable row level security;
revoke all on table public.scrapfit_documents from anon, authenticated;
grant all on table public.scrapfit_documents to service_role;

-- Public listing photos can be viewed by anyone; uploads/deletes are performed by the
-- Streamlit server using its secret key, never by a browser using a privileged key.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'listing-photos',
    'listing-photos',
    true,
    8388608,
    array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update
set public = true,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- Verify the table exists:
-- select doc_key, updated_at from public.scrapfit_documents order by doc_key;
