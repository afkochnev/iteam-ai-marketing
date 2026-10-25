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

## Repository audit, 2026-10-25

Read-only GitHub API checks found no repository rulesets, no classic protection on `main` (404 Branch not protected), and no environments. Thus the current workflow can prove CI success but cannot claim a protected human approval boundary. Configure and independently verify these controls before production deployment is connected. Do not interpret a green image build as production acceptance.

## Следующий шаг

Подготовлен [протокол approval, digest deployment и rollback](release-operations.md), scripts/release_ops.py, scripts/release_backup.py и scripts/release_drill.py. Конфигурации защиты main/environment хранятся в infra/release/ и требуют отдельного согласованного применения; их наличие в git не означает применение. Production из GitHub Actions не вызывается. Статус приёмки определяется по реальным CI/drill proofs, live gates и отдельно разрешённому production deploy.
