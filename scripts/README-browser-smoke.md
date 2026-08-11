# KM VMS browser smoke

Постоянный безопасный smoke-набор подтверждает, что установленный KM VMS принимает вход, открывает 11 основных маршрутов в desktop/mobile viewport и выполняет выход. Он не изменяет камеры, записи, настройки, Storage или workspace layout и не заменяет Chapter 14 Final Test.

## Запуск на рабочем NAS

Из корня `/Volume3/docker/vms`:

```sh
KMVMS_USERNAME='...' KMVMS_PASSWORD='...' sh scripts/km-vms-browser-smoke.sh
```

Runner сам определяет фактический опубликованный порт активного `nginx` этого project root и разрешает только loopback origin рабочего NAS. `KMVMS_BASE_URL` допускает только `127.0.0.1`, `localhost` или `[::1]` с тем же HTTP-портом. Произвольный host, другой port, path, query, fragment или embedded credentials отклоняются до запуска browser container.

Для Codex значения берутся из restricted local test-user file и передаются SSH-процессу через stdin с LF-only (`StandardInput.NewLine = "\n"`). Значения нельзя помещать в команду, файл, отчёт или artifact.

Опциональные переменные:

- `DOCKER_BIN` — абсолютный путь к NAS Docker binary;
- `PLAYWRIGHT_IMAGE` — безопасный image reference, по умолчанию `km-vms-playwright-tools:1.44.1`;
- `KMVMS_BASE_URL` — только approved loopback origin;
- `KMVMS_SMOKE_OUT_DIR` — только `/tmp/km-vms-stage-13-5-7-0-*/smoke`.

Проверка конфигурации без запуска browser container:

```sh
KMVMS_USERNAME=dummy KMVMS_PASSWORD=dummy sh scripts/km-vms-browser-smoke.sh --validate-config
```

## Что проверяется

Для `/`, `/live`, `/chronology`, `/recordings`, `/cameras`, `/storage`, `/settings`, `/diagnostics`, `/security-journal`, `/system-status`, `/apk` проверяются route marker, authenticated shell, отсутствие page-level overflow, fatal render/pageerror и обязательные initial API responses.

Общие обязательные API: `GET /api/system/status` и `GET /api/auth/me`. Route-specific table хранится в `scripts/browser-smoke/core-smoke.js`; любой status `>= 400`, отсутствие response или timeout обязательного entry означает FAIL. Для `/settings` запрос `GET /api/users` обязателен только при `admin_access` и фактическом initial request.

Ожидаемые unauthenticated `401/403` до `/login`, background polling, optional indicators/status, export-support metadata, media/HLS/preview/playback и запросы после пользовательских действий не становятся обязательными глобально.

## Результат

Runner печатает только `SMOKE_PASS <output-path>` или `SMOKE_FAIL <output-path>`.

Output содержит:

- `summary.json`;
- отдельные unauthenticated/login/logout facts;
- по одному sanitized `*.facts.json` и redacted `*.png` на route/viewport.

JSON не содержит credentials, tokens, raw bodies/headers, operator text или dynamic identifiers. Неизвестный dynamic pathname опускается; известный сохраняется только как безопасный template.

Exit codes:

- `0` — PASS;
- `1` — smoke FAIL;
- `2` — configuration boundary FAIL.

Найденные product defects этот набор не исправляет: их нужно зарегистрировать в фактическом Roadmap owner, включая Chapter 13.7.
