-- GrowthPulse AI: read-only fingerprints of the Milestone 4 data.
--
-- Run as postgres through psql before the Milestone 5 migrations, again right after them,
-- and after any recovery. Every value must be identical each time: the Milestone 5
-- migrations add columns, tables and roles but must never change a Milestone 4 row.
--
-- Each row digests one Milestone 4 table over its original Milestone 4 columns only, in
-- key order, so columns added by Milestone 5 never affect it. The last two rows identify
-- the historical report by its ID and the SHA-256 of its stored body.
--
-- Safety: one READ ONLY transaction ending in ROLLBACK. Only counts and SHA-256 digests are
-- returned, never row values. The timezone and date style are fixed for this transaction
-- only, so timestamps render the same whatever the psql session's settings are.

begin transaction read only;
set local timezone = 'UTC';
set local datestyle = 'ISO, YMD';

select '01 pipeline_runs' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.run_id), 'UTF8')), 'hex'), 'empty') as digest
from (select run_id, started_at, finished_at, status, data_source, triggered_by, code_version, counts, error_code, report_id from growthpulse.pipeline_runs) as t
union all
select '02 ad_performance_records' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.record_id), 'UTF8')), 'hex'), 'empty') as digest
from (select record_id, data_source, reporting_date, platform, account_id, campaign_id, campaign_name, campaign_type, impressions, clicks, spend, conversions, conversion_value, currency, account_timezone, conversion_event_id, conversion_event_name, conversion_stage, attribution_model, click_window_days, view_window_days, report_time_basis, ingested_at, first_seen_run_id, last_changed_run_id, first_seen_at, updated_at from growthpulse.ad_performance_records) as t
union all
select '03 quarantined_records' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.quarantine_id), 'UTF8')), 'hex'), 'empty') as digest
from (select quarantine_id, data_source, connector, row_index, issue_codes, issues, payload_sha256, raw_payload, first_seen_run_id, last_seen_run_id, first_seen_at, last_seen_at from growthpulse.quarantined_records) as t
union all
select '04 analytics_reports' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.report_id), 'UTF8')), 'hex'), 'empty') as digest
from (select report_id, run_id, data_source, report_schema_version, data_as_of, period_start, period_end, record_count, report, created_at from growthpulse.analytics_reports) as t
union all
select '05 findings' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.finding_id), 'UTF8')), 'hex'), 'empty') as digest
from (select finding_id, data_source, rule_id, severity, pre_guard_severity, direction, guard_outcome, is_suppressed, platform, account_id, campaign_id, basis_id, metric, comparison_label, comparison_purpose, baseline_start, baseline_end, current_start, current_end, percent_change, payload, first_report_id, latest_report_id, latest_data_as_of, first_seen_at, last_seen_at from growthpulse.findings) as t
union all
select '06 experiment_proposals' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.proposal_id), 'UTF8')), 'hex'), 'empty') as digest
from (select proposal_id, idempotency_key, data_source, report_id, finding_id, group_id, title, hypothesis, proposed_change, primary_metric, requires_human_approval, created_at from growthpulse.experiment_proposals) as t
union all
select '07 experiment_status_events' as fingerprint,
    count(*)::text as row_count,
    coalesce(encode(pg_catalog.sha256(convert_to(
        string_agg(t::text, E'\n' order by t.event_id), 'UTF8')), 'hex'), 'empty') as digest
from (select event_id, proposal_id, sequence, status, previous_status, actor, note, changed_at from growthpulse.experiment_status_events) as t
union all
select '08 report id', count(*)::text, coalesce(string_agg(report_id, ', '), 'none')
from growthpulse.analytics_reports
union all
select '09 report body sha256', count(*)::text,
    coalesce(string_agg(encode(pg_catalog.sha256(convert_to(report::text, 'UTF8')), 'hex'),
        ', ' order by report_id), 'none')
from growthpulse.analytics_reports
order by 1;

rollback;
