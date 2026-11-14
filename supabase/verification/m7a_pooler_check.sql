-- GrowthPulse AI: transaction-pooler isolation check for the signed context (gate G4,
-- step 6). Read-only: it writes nothing, and the context it creates lasts one transaction.
--
-- Run as postgres through the TRANSACTION pooler (port 6543), many times, so that later
-- transactions land on server connections an earlier context used:
--     for i in $(seq 30); do psql "$POOLER_6543" -X -A -t -F ' ' -f supabase/verification/m7a_pooler_check.sql; done > pooler.txt
--     grep -c ' f$' pooler.txt        # must print 0
--     awk '$1=="backend" {print $2}' pooler.txt | sort | uniq -d | head    # must print at least one PID
-- The second command shows that server connections were reused, which is what makes the check
-- meaningful. If it prints nothing, run more iterations.
--
-- Each line is "<check> <ok>", or "backend <pid>" (informational).

begin transaction read only;
select 'backend', pg_catalog.pg_backend_pid();
select growthpulse_auth.begin_job(
    'workspace', '26f3f7ff-8015-413a-8029-06e04727974c', pg_catalog.gen_random_uuid()
);
select 'context_inside_transaction', pg_catalog.count(*) = 1
from growthpulse_auth.current_context();
commit;

-- Each statement below is its own transaction, so the pooler may place it on any server
-- connection, including the one used above.
select 'backend', pg_catalog.pg_backend_pid();
select 'no_context_after_commit', pg_catalog.count(*) = 0 from growthpulse_auth.current_context();
select 'no_setting_after_commit',
    coalesce(pg_catalog.current_setting('growthpulse.context', true), '') = '';
select 'no_mac_after_commit',
    coalesce(pg_catalog.current_setting('growthpulse.context_mac', true), '') = '';
