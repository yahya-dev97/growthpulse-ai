-- DESTRUCTIVE. Reverses every GrowthPulse migration (Milestones 4, 5 and 7a.2) and
-- deletes every GrowthPulse row, including encrypted credentials, users, memberships and
-- audit events. Never run this without explicit approval. To undo only Milestone 7a.2,
-- use 20261114100000_revert_m7a2_tenant_security.sql; to undo only Milestone 5 after
-- that, use 20261111200000_revert_milestone_5.sql.
--
-- Kept outside supabase/migrations so `supabase db push` can never run it. Run it by hand,
-- as the project owner (postgres), only after reviewing it. Afterwards, mark the
-- migrations as reverted so the CLI history matches the database:
--
--   supabase migration repair --status reverted 20261111100000 20261111100100 20261111100200 \
--       20261111200000 20261111200100 20261111200200 20261111200300 20261111200400 \
--       20261114100000 20261114100100 20261114100200 20261114100300
--
-- Every object is dropped by name with RESTRICT, never CASCADE. If anything outside these
-- two schemas depends on them (a view, a foreign key, a function), or if a schema holds
-- an object these migrations did not create, the statement fails and the whole
-- transaction rolls back, leaving everything in place. "if exists" makes it safe to run
-- on a partly applied state.

begin;

drop view if exists
    growthpulse_read.audit_events,
    growthpulse_read.latest_client_reports,
    growthpulse_read.report_breakdowns,
    growthpulse_read.clients,
    growthpulse_read.ad_accounts,
    growthpulse_read.provider_connections,
    growthpulse_read.discovered_ad_accounts,
    growthpulse_read.account_sync_status,
    growthpulse_read.owner_actions,
    growthpulse_read.pipeline_runs,
    growthpulse_read.campaign_daily_conversions,
    growthpulse_read.campaign_daily_delivery,
    growthpulse_read.analytics_reports,
    growthpulse_read.latest_reports,
    growthpulse_read.findings,
    growthpulse_read.data_quality_issues,
    growthpulse_read.experiment_proposals,
    growthpulse_read.experiment_status_history
    restrict;
drop schema if exists growthpulse_read restrict;

drop table if exists
    growthpulse_auth.context_keys,
    growthpulse_auth.audit_events,
    growthpulse_auth.sessions,
    growthpulse_auth.client_grants,
    growthpulse_auth.memberships,
    growthpulse_auth.users,
    growthpulse_auth.organizations
    restrict;

drop function if exists
    growthpulse_secrets.store_credential(uuid, text, smallint, smallint, bytea, bytea, timestamptz),
    growthpulse_secrets.read_credentials(uuid),
    growthpulse_secrets.delete_credentials(uuid),
    growthpulse_secrets.mark_connection_needs_reauth(uuid),
    growthpulse_secrets.credentials_needing_rotation(smallint)
    restrict;
drop table if exists growthpulse_secrets.oauth_credentials restrict;
drop schema if exists growthpulse_secrets restrict;

drop function if exists
    growthpulse.create_client(uuid, text, text),
    growthpulse.rename_client(uuid, text),
    growthpulse.archive_client(uuid),
    growthpulse.create_connection(uuid, text, text),
    growthpulse.revoke_connection(uuid),
    growthpulse.add_ad_account(uuid, uuid, text, date),
    growthpulse.add_synthetic_ad_account(uuid, text, text, text, text, date),
    growthpulse.set_account_status(uuid, text),
    growthpulse.set_sync_config(uuid, boolean, text, smallint, smallint, smallint),
    growthpulse.request_sync(uuid),
    growthpulse.decide_experiment(uuid, text, text),
    growthpulse.start_account_sync_tracking(uuid, text, date),
    growthpulse.log_owner_action(text, text, text)
    restrict;

-- Dropped together, so foreign keys between these tables do not block each other.
-- Their indexes, constraints, triggers, policies and identity sequence go with them.
drop table if exists
    growthpulse.analytics_report_breakdowns,
    growthpulse.owner_action_log,
    growthpulse.account_sync_state,
    growthpulse.account_sync_config,
    growthpulse.experiment_status_events,
    growthpulse.experiment_proposals,
    growthpulse.findings,
    growthpulse.analytics_reports,
    growthpulse.quarantined_records,
    growthpulse.ad_performance_records,
    growthpulse.pipeline_runs,
    growthpulse.ad_accounts,
    growthpulse.discovered_ad_accounts,
    growthpulse.provider_connections,
    growthpulse.clients,
    growthpulse.workspaces
    restrict;
drop function if exists
    growthpulse.check_experiment_status_transition(),
    growthpulse.reject_update(),
    growthpulse.derive_scope(),
    growthpulse.check_record_run_scope(),
    growthpulse.check_client_update(),
    growthpulse.check_ad_account_update(),
    growthpulse.check_connection_update()
    restrict;
drop schema if exists growthpulse restrict;

drop function if exists
    growthpulse_auth.request_owner_action_ids(),
    growthpulse_auth.action_permission(text),
    growthpulse_auth.target_workspace(text, text),
    growthpulse_auth.due_account_syncs(timestamptz),
    growthpulse_auth.revoke_session(bytea),
    growthpulse_auth.create_session(uuid, bytea, timestamptz),
    growthpulse_auth.request_workspace_if(text),
    growthpulse_auth.request_permits(text),
    growthpulse_auth.request_client_ids(),
    growthpulse_auth.job_account_ids(),
    growthpulse_auth.job_client_ids(),
    growthpulse_auth.job_workspace(),
    growthpulse_auth.context_workspace(),
    growthpulse_auth.begin_job(text, uuid, uuid),
    growthpulse_auth.begin_request(bytea, uuid, uuid),
    growthpulse_auth.role_permits(text, text),
    growthpulse_auth.current_context(),
    growthpulse_auth.set_context(text, uuid, uuid, uuid, text, uuid, uuid),
    growthpulse_auth.context_mac(text),
    growthpulse_auth.check_client_grant(),
    growthpulse_auth.check_membership_update(),
    growthpulse_auth.check_client_organization(),
    growthpulse_auth.check_organization_update(),
    growthpulse_auth.create_organization_for_workspace()
    restrict;
drop schema if exists growthpulse_auth restrict;

-- Fails, and rolls everything back, if either role still holds a privilege or owns an
-- object anywhere else in the database. Close any session using them first.
drop role if exists growthpulse_authenticator;
drop role if exists growthpulse_manager;
drop role if exists growthpulse_token_manager;
drop role if exists growthpulse_dashboard;
drop role if exists growthpulse_writer;

commit;
