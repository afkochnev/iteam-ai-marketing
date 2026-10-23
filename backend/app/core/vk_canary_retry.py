"""Bounded, one-off operator authorization; never a general retry policy."""

from uuid import UUID

CANARY_ID = UUID("1142723f-4c25-4334-8f22-7e3ceb1f55f4")
CANARY_VERSION_ID = UUID("6a4685cd-128b-4a76-ae4b-4595fb8a3238")
CANARY_OWNER_ID = -150574411
RETRY_EVENT = "PUBLICATION_CANARY_RETRY_REQUESTED"
RETRY_KEY = f"publication-canary-retry:{CANARY_ID}"
CONSUMED_KEY = f"{RETRY_KEY}:consumed"

# A distinct authorization after the empty-static-token fix. Never reuse v1 keys.
RETRY_V2_ID = "credential-store-fixed-v1"
RETRY_V2_EVENT = "PUBLICATION_CANARY_RETRY_V2_REQUESTED"
RETRY_V2_KEY = f"publication-canary-retry-v2:{CANARY_ID}"
CONSUMED_V2_KEY = f"{RETRY_V2_KEY}:consumed"
