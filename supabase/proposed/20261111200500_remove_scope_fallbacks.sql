-- GrowthPulse AI, PROPOSED migration 9: remove the Phase B scope fallbacks.
--
-- NOT APPLIED ANYWHERE except disposable test databases, and kept outside
-- supabase/migrations so `supabase db push` cannot run it. Move it into the migrations
-- folder only with explicit approval, after the Phase C code is deployed.
--
-- Phase B let the Milestone 4 store keep working by deriving scope in the database: runs
-- defaulted to the fictional legacy client, and records, findings, reports and proposals
-- had their account or client filled in by a trigger. Since Phase C, the application
-- passes every scope value explicitly, so these fallbacks are no longer needed. Without
-- them, a writer that omits scope fails on NOT NULL instead of being filled in.
--
-- Kept: the run-scope check on records (check_record_run_scope), every composite foreign
-- key, and every constraint. No row is read or changed. Restrict-only.

alter table growthpulse.pipeline_runs alter column client_id drop default;

drop trigger ad_performance_records_1_derive_scope
    on growthpulse.ad_performance_records restrict;
drop trigger findings_derive_scope on growthpulse.findings restrict;
drop trigger analytics_reports_derive_scope on growthpulse.analytics_reports restrict;
drop trigger experiment_proposals_derive_scope on growthpulse.experiment_proposals restrict;

drop function growthpulse.derive_scope() restrict;
