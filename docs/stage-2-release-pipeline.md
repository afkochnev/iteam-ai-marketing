# Stage 2: immutable releases

First increment: CI-success on an accepted main push triggers multi-platform backend/frontend image builds in GHCR. Each image has the exact release SHA label, SBOM/provenance and keyless signature. Release records reference immutable digests. Actions are pinned to commit SHAs. PR code cannot publish images through this workflow.

No production deployment is triggered. No secrets or runtime .env are build inputs. The frontend API URL deliberately matches the current local production endpoint; moving to a reverse proxy needs a separate reviewed change.

## Remaining acceptance gates

- Merge this workflow, then prove a successful real build for its exact main SHA; verify both architectures, signature and SBOM from GHCR.
- Add an approved production environment (required reviewer configured in GitHub settings, not merely an environment name in YAML).
- Add digest-only deployment and rollback scripts, validate BUILD_SHA and image labels, Alembic head, provider gates, credential mounts and service topology.
- Confirm encrypted backup / isolated restore before deployment, retain the previous manifest. No automatic database downgrade.
- Demonstrate rollback on an isolated environment and then an approved production deployment. Do not mark Stage 2 complete before these pass.

Production presently has VK and Telegram publishing enabled. Any deployment must retain these explicit gates, inspect scheduled rows and avoid stopping workers across due slots. Release pipelines do not create or authorize publications.
