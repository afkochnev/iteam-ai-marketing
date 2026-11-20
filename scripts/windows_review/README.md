# Windows review sources

These versioned sources strengthen the existing isolated Windows review launcher.
They are development artifacts. Creating or testing them does not install them in
the accepted transfer, start Docker, restore data or change credentials.

The accepted Compose file declares `windows_postgres:/var/lib/postgresql/data`
and `windows_redis:/data` under project `iteam_windows_transfer`. The launcher
checks the effective Compose configuration before `up`. The bridge separately
checks actual Docker inspect mounts before opening loopback ports and during its
existing periodic check:

| Service | Engine volume | Container destination |
| --- | --- | --- |
| postgres | `iteam_windows_transfer_windows_postgres` | `/var/lib/postgresql/data` |
| redis | `iteam_windows_transfer_windows_redis` | `/data` |
| backend, frontend | none | none |

Missing, additional, redirected, swapped, bind, credential and read-only data
mounts are rejected. The existing image, BUILD_SHA, service, network and provider
checks remain in force. Volume identity alone does not prove database contents:
the existing database fingerprints and separate transfer acceptance remain the
evidence for that.

## Offline checks

From the repository root:

```text
python -B -m unittest discover -s scripts/windows_review/tests -v
```

The tests use synthetic Compose/inspect data and mocked startup calls. They do
not need Docker, PostgreSQL, Redis, application dependencies or secrets.

## Installation preview — not performed

After source review and acceptance, the future transfer-root update consists of
these three files, kept together:

```text
scripts/windows_review/start_iteam.py          -> <transfer-root>/start_iteam.py
scripts/windows_review/windows_local_access.py -> <transfer-root>/windows_local_access.py
scripts/windows_review/review_validation.py    -> <transfer-root>/review_validation.py
```

The installed launcher continues to use the transfer's existing
`tools/windows_runtime.py`; its original validation runs before the new volume
check. No restore or credential-management helper is copied. No change to
`windows/.env`, Compose, volumes, markers or credentials is part of this update.
The files are intended to run from the accepted transfer root after installation,
not directly from this source directory. A real Windows startup remains a
separate acceptance step; passing the offline tests does not claim that it ran.
