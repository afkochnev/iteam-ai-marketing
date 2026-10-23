"""Bounded, one-off operator authorization; never a general retry policy."""

from uuid import UUID

CANARY_ID = UUID("1142723f-4c25-4334-8f22-7e3ceb1f55f4")
CANARY_VERSION_ID = UUID("6a4685cd-128b-4a76-ae4b-4595fb8a3238")
CANARY_OWNER_ID = -150574411
RETRY_EVENT = "PUBLICATION_CANARY_RETRY_REQUESTED"
RETRY_KEY = f"publication-canary-retry:{CANARY_ID}"
CONSUMED_KEY = f"{RETRY_KEY}:consumed"
