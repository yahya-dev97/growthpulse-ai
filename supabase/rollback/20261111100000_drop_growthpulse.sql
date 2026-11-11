-- DESTRUCTIVE. Reverses all three 20261111 migrations and deletes every GrowthPulse row.
-- Never run this without explicit approval.
--
-- Kept outside supabase/migrations so `supabase db push` can never run it. Run it by hand,
-- as the project owner (postgres), only after reviewing it. Afterwards, mark the
-- migrations as reverted so the CLI history matches the database:
--
--   supabase migration repair --status reverted 20261111100000 20261111100100 20261111100200
--
-- Every object is dropped by name with RESTRICT, never CASCADE. If anything outside these
-- two schemas depends on them (a view, a foreign key, a function), or if a schema holds
-- an object these migrations did not create, the statement fails and the whole
-- transaction rolls back, leaving everything in place. "if exists" makes it safe to run
-- on a partly applied state.

begin;

drop view if exists
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

-- Dropped together, so foreign keys between these tables do not block each other.
-- Their indexes, constraints, triggers, policies and identity sequence go with them.
drop table if exists
    growthpulse.experiment_status_events,
    growthpulse.experiment_proposals,
    growthpulse.findings,
    growthpulse.analytics_reports,
    growthpulse.quarantined_records,
    growthpulse.ad_performance_records,
    growthpulse.pipeline_runs
    restrict;
drop function if exists
    growthpulse.check_experiment_status_transition(),
    growthpulse.reject_update()
    restrict;
drop schema if exists growthpulse restrict;

-- Fails, and rolls everything back, if either role still holds a privilege or owns an
-- object anywhere else in the database. Close any session using them first.
drop role if exists growthpulse_dashboard;
drop role if exists growthpulse_writer;

commit;
