"""The fictional client that Milestone 4's synthetic data is backfilled into.

Milestone 4 stored records for two synthetic accounts with no client. Phase B's backfill
migration seeds exactly this workspace, client and pair of accounts, with these fixed IDs,
and assigns every existing record, report and finding to them. The external account IDs
must equal the synthetic generator's; a test enforces it.

These are fictional fixtures, not advertising account IDs.
"""

from uuid import UUID

from growthpulse_ai.accounts.models import AccountContext, ClientContext
from growthpulse_ai.schema import DataSource, Platform

LEGACY_WORKSPACE_ID = UUID("26f3f7ff-8015-413a-8029-06e04727974c")
LEGACY_CLIENT_ID = UUID("27c669c8-001a-4573-a336-a8b21331ee86")
LEGACY_GOOGLE_ACCOUNT_ID = UUID("3216f4c2-0201-4c18-a7cd-3c2007f5c18e")
LEGACY_META_ACCOUNT_ID = UUID("c8fea272-daaa-41e3-88d3-dbb54c3df6cc")

LEGACY_CLIENT_NAME = "ExampleScan UK (fictional)"
LEGACY_CURRENCY = "GBP"
LEGACY_TIMEZONE = "Europe/London"
LEGACY_EXTERNAL_ACCOUNT_IDS = {
    Platform.GOOGLE_ADS: "synthetic-google-account-001",
    Platform.META_ADS: "synthetic-meta-account-001",
}

LEGACY_SYNTHETIC_CLIENT = ClientContext(
    client_id=LEGACY_CLIENT_ID,
    name=LEGACY_CLIENT_NAME,
    data_source=DataSource.SYNTHETIC,
    accounts=tuple(
        AccountContext(
            ad_account_id=ad_account_id,
            client_id=LEGACY_CLIENT_ID,
            data_source=DataSource.SYNTHETIC,
            platform=platform,
            external_account_id=LEGACY_EXTERNAL_ACCOUNT_IDS[platform],
            currency=LEGACY_CURRENCY,
            timezone=LEGACY_TIMEZONE,
            display_name=f"{LEGACY_CLIENT_NAME} {platform.value}",
        )
        for platform, ad_account_id in (
            (Platform.GOOGLE_ADS, LEGACY_GOOGLE_ACCOUNT_ID),
            (Platform.META_ADS, LEGACY_META_ACCOUNT_ID),
        )
    ),
)
