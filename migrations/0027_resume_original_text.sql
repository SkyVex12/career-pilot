-- Original file text is distinct from authored text and bound to the current file hash.
set local role app_owner;
alter table app.resume_versions
 add column original_text_enc bytea,
 add column original_text_key_version int,
 add column original_content_sha256 text,
 add constraint resume_original_text_key check ((original_text_enc is null) = (original_text_key_version is null));
create function app.clear_replaced_resume_original() returns trigger language plpgsql as $$
begin
 if new.content_sha256 is distinct from old.content_sha256 then
  new.original_text_enc := null;
  new.original_text_key_version := null;
  new.original_content_sha256 := null;
 end if;
 return new;
end
$$;
create trigger clear_replaced_resume_original before update of content_sha256 on app.resume_versions
 for each row execute function app.clear_replaced_resume_original();
