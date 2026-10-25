# Stage 2: immutable releases

Этап 2 принят для текущего localhost-размещения 2026-10-25. [Итоги приёмки и проверяемые доказательства](stage-2-acceptance-2026-10-25.md).

CI-success на принятом main push запускает multi-platform сборку backend/frontend в GHCR. Каждый release связан с точным commit, двумя immutable digest, ARM64/AMD64, SBOM/provenance и keyless signatures. PR-код не публикует release images. Production не компилирует исходники и не использует ZIP bundles как стандартный механизм release.

Цепочка: accepted main → tests → multi-arch images → GHCR → SBOM/provenance → signing → digest manifest → ручной environment approval → подписанный receipt → отдельно разрешённый локальный deploy. GitHub Actions не получает production credentials, SSH или production runner. Включение publishing и согласование контента остаются отдельными решениями приложения.

Операционный протокол, проверки и ограничения описаны в [release-operations.md](release-operations.md). Конфигурации main/environment хранятся в infra/release/; изменение файлов или merge PR не применяют GitHub settings автоматически. Проверяются реальные API rules: обязательные checks/PR для main, reviewer `afkochnev`, protected branches, `can_admins_bypass=false`. `apply` дополнительно проверяет фактическую историю approval и подписанный receipt.

Первый production digest deploy: runtime `9472225798064b4a8b0b971bf2589d67f35f7986`, операторский протокол `abe07880615e4fa81ffe6f66e69c63f3c66fc84f` (PR56), предыдущий runtime `51c21f18cebfc33db68b03ae3307c4c3c8a1053e`. Source/target SHA приложения не следует путать с SHA инструмента.

Production сохраняет VK/Telegram publishing enabled. Обновление проверяет ближайшие слоты и отсутствие отправок в работе; pipeline не создаёт тестовых публикаций и не разрешает повторную отправку canary.

Приёмка ограничена текущим localhost endpoint и проверенным Chromium: Secure-cookie, production-mode и вход проверены. Frontend API URL встроен как `http://localhost:8000/api/v1`; домен/HTTPS требуют отдельного reviewed change и новой сборки. Это не приёмка внешнего HTTPS-размещения или Safari.

Следующий этап исходного аудита — observability и operations: exception reporting, structured logs, traces, metrics, состояние очередей/workers и alerts. Его внедрение является отдельной работой.
