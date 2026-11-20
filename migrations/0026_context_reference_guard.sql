-- Keep the selected private brief bound to its request and authorizing account.
set local role app_owner;
alter table app.context_brief_versions add constraint context_version_request_owner
 unique(id,request_id,account_id);
alter table app.context_requests drop constraint context_active_version_fk;
alter table app.context_requests add constraint context_active_version_fk
 foreign key(active_version_id,id,account_id)
 references app.context_brief_versions(id,request_id,account_id)
 deferrable initially deferred;
