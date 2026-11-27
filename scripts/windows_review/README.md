# Windows Review Tools

Use these sources to run the four-service review profile in a restored, review-only Windows transfer. The review opens the frontend on `127.0.0.1:3000` and the API on `127.0.0.1:8000`. A transfer running publishing or the user-post worker has a different launcher and lifecycle.

## Review profile

Both entry points require the local `desktop-linux` context. They reject transfers with a publishing or user-post profile, and also check the engine for existing publishing or user-post Compose containers, including stopped containers. This matters because both installations use the `iteam_windows_transfer` project name for their core services.

The launcher requires the transfer's existing `tools/windows_runtime.py`, `windows/release.json`, and verified database import marker. It validates the effective Compose configuration before requesting startup of PostgreSQL, Redis, backend, and frontend. Before binding either port, the bridge checks that the internal network contains exactly those four running containers. It rechecks the snapshot every ten seconds and closes local access if validation fails.

| Service | Required named volume | Destination |
| --- | --- | --- |
| postgres | `iteam_windows_transfer_windows_postgres` | `/var/lib/postgresql/data` |
| redis | `iteam_windows_transfer_windows_redis` | `/data` |
| backend, frontend | None | — |

Before startup the launcher verifies that both named volumes already exist and checks their driver, options and available Compose labels. Existing core containers must belong to this transfer and its single accepted review Compose file. The data volumes must be writable with the expected driver. The validators reject additional, swapped or redirected mounts. The bridge also checks the accepted application image references, backend build identifier, internal network, loopback port bindings and publishing flags. Database contents are verified by the transfer's separate import acceptance.

## Offline verification

From the repository root, run:

```powershell
python -B -m unittest discover -s scripts/tests -p 'test_windows_review*.py' -v
```

This suite runs with the Python standard library and synthetic Compose and Docker inspect fixtures. The release-tools CI job discovers it under `scripts/tests`. Pull requests that change the review scripts, their tests or their workflow also run the suite on a GitHub-hosted Windows runner with Python 3.13.

## Use in a dedicated review transfer

For a separately approved installation, keep `start_iteam.py`, `windows_local_access.py`, `review_validation.py`, and `review_origin.py` together in the root of the **dedicated review transfer**. Keep its accepted runtime module in `tools` and its release manifest and database marker in `windows`. Run the launcher from that transfer with `py -3.13 .\start_iteam.py`. Leave the window open while using the review UI. Ctrl+C closes the bridge.

Keep the publishing installation's profile selector and autostart files in place. To accept these sources on Windows, use a separate review-only Docker Desktop installation with the expected restored volumes, record the detected image and volume identities, then check the home page, health and readiness. The offline suite covers validation and startup ordering. Live review acceptance is recorded separately.
