-- 0011_profile_node: a resume's headline facts (years of experience, focus) as a memory node of
-- their own, kind 'profile', so the career digest can carry and cite them. In the 2026-10-28
-- interview the resume's "15 years of experience" summary reached no answer: extraction had
-- nowhere to put it.
-- Source: ARCHITECTURE.md, "Memory system" and "Session pack and hot digest formats".
--
-- Expand only: the kind check allows one more value; every existing row still passes.

set local role app_owner;

alter table app.memory_nodes drop constraint memory_nodes_kind_check;
alter table app.memory_nodes
  add constraint memory_nodes_kind_check check (kind in
    ('profile','role','project','story','skill','work_item','commitment','company','person',
     'decision','system','term'));
