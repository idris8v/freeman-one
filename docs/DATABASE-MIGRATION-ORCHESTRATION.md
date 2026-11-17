# Freeman One: migration runner и schema readiness

Дата: 14 октября 2026 года. D01 исправлен в локальном коде. **Это не разрешение на production rollout: общий статус остаётся NO-GO до остальных release gates.**

## Причина

Startup вызывал четыре обработчика совместимости и `Base.metadata.create_all`, но не шесть явных remediation-миграций. `create_all` не добавляет столбцы существующих таблиц и не делает backfill. `/health` проверял только `SELECT 1`. В результате могли запускаться checkout, бот и workers на неполной схеме.

## Реализация и изменённые файлы

| Файл | Изменение |
|---|---|
| [migrations.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/app/database/migrations.py>) | Единственный offline CLI, фиксированный порядок, журнал `schema_migrations`, SHA-256, атомарный DDL + journal, PostgreSQL advisory lock |
| [schema_bootstrap.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/app/database/schema_bootstrap.py>) | Прежний bootstrap вынесен из startup в отдельный шаг |
| [schema_readiness.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/app/database/schema_readiness.py>) | Read-only проверка истории, таблиц/столбцов и критичных fences; shared runtime lease; HTTP gate |
| [session.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/app/database/session.py>) | `Database.initialize()` теперь explicit offline alias runner; normal startup его не вызывает |
| [main.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/app/main.py>) | Schema gate до seed/бота/всех jobs; HTTP 503 при NOT READY, отдельный `/live`, остановка работы при потере lease |
| [runtime.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/app/bot/runtime.py>) | Telegram supervisor проверяет schema lease перед конфигурацией/обработкой |
| [test_schema_migrations.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/tests/test_schema_migrations.py>) | 20 новых PostgreSQL orchestration/lifecycle/CLI tests |
| [test_telegram_startup.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/tests/test_telegram_startup.py>), [test_bot.py](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/tests/regression/test_bot.py>) | Фикстуры явно готовят схему до старта приложения |
| README и DEPLOYMENT-READINESS | Обновлён вход для оператора; первоначальная репетиция сохранена как историческое доказательство |

Существующие migration-функции, финансовые services, модели клиентов/подписок, RemnaWave recovery и checkout алгоритмы не переписывались. Добавляется только служебная таблица `schema_migrations(version PRIMARY KEY, checksum, applied_at)`; отдельная ручная migration для неё не нужна — runner создаёт её под exclusive lock.

## Порядок и семантика

1. `001_proxy_compatibility` — PostgreSQL no-op, SQLite compatibility.
2. `002_accounts` — account migration.
3. `003_client_offers` — offer compatibility.
4. `004_customer_identity` — identity migration.
5. `005_base_schema` — metadata bootstrap и nullable email delivery subscription.
6. `006_vless_creation` — I01.
7. `007_vless_renewal` — I02.
8. `008_vless_capacity` — I03 и reservation backfill.
9. `009_vless_sync` — I04.
10. `010_vless_fallback` — I09.
11. `011_tariff_identity` — F03 последней.

Для старой схемы без журнала runner **исполняет существующие идемпотентные шаги**, а не ставит отметки “применено” по наличию таблиц. Уже записанные шаги пропускаются только при совпадении checksum. Неизвестные версии, изменённые checksum или дырки в истории блокируют выполнение; автоматического stamp/repair/delete нет. Новые миграции добавляются в конец registry; выпущенные migration-файлы менять нельзя. Изменение ORM-модели, требующее DDL, должно сопровождаться новой версией.

В PostgreSQL один executing connection владеет session exclusive advisory lock на database/schema. Каждый шаг и запись его версии находятся в **одной транзакции этого connection**. Legacy `commit()` внутри account handler не коммитит раньше journal. Ошибка, cancellation или смерть backend откатывает текущий шаг; предыдущие commit сохраняются. Потерянный ответ после commit безопасен: повторный runner читает прежнюю запись и не исполняет шаг ещё раз. У всей цепочки нет единой глобальной транзакции.

Exclusive lock не ждёт: второй runner получает `MigrationBusy`. Работающие экземпляры нового приложения держат shared lock; migration также получает отказ до их остановки. Lock не использует website/worker pool: runtime держит один отдельный autocommit connection на экземпляр. Подключение и command timeout — 5 секунд; startup schema gate ограничен 10 секундами. Runner defaults: lock timeout 5 секунд, statement timeout 300 секунд **на SQL statement**. F03 сохраняет собственный lock timeout 5 секунд. Для больших баз нужно измерить время на копии; это не общий deadline всей цепочки.

Нормальный startup не выполняет DDL/миграции. Проверяются все ORM tables/columns, journal checksums/order, nullable account/guest/offer fields, journal primary/unique fences, семантика pending renewal index, отсутствие старого global UNIQUE(months). Неполная или неподтверждённая схема → `/health` HTTP **503, `status: NOT READY`**; HTML/API requests заблокированы до identity/ORM processing. `/live` HTTP 200 означает только живой HTTP process. Public assets доступны, но не подтверждают readiness.

Seed/config/mail/publication data initialization сохранены и выполняются **только после** schema gate. Все запускаемые из lifespan фоновые jobs проходят общий gate, Telegram — также собственный. При schema lease loss новые HTTP operations блокируются, supervisor останавливает jobs/бота при следующей проверке (период 5 секунд, bounded ping). Потеря lease или первоначальный NOT READY не запускают automatic upgrades/recovery startup: после исправления схемы нужен явный restart приложения.

SQLite оставлена для изолированных локальных тестов: file lock и compatibility bootstrap. PostgreSQL-only шаги там не выполняются; атомарность DDL/ledger и production guarantees доказаны **только на PostgreSQL**.

## Доказательства

Изолированный local PostgreSQL 17.11, loopback `127.0.0.1:65433`, disposable `test_*` databases. Нет production connections, live payments, реальных VPN keys или Telegram sends.

| Прогон | Результат |
|---|---|
| Новые tests: fresh/legacy, repeats, cancellation, backend termination, journal write failure, legacy commits, lost final response, checksum/history/schema drift, runtime fencing, startup/lease loss, реальный CLI и два отдельных CLI processes | **20/20 PASS** |
| Финальный focused run вместе с Telegram startup | **48/48 PASS**: 20 новых + 28 Telegram; [focused-final.log](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/artifacts/migration-orchestration-20261014/focused-final.log>) |
| Прежние billing/provisioning/RemnaWave/checkout/tariff/key/publication/Telegram regressions | **439/439 PASS**; включает C01–C03, I01–I09, I12, F01, F03; [previous-regressions.log](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/artifacts/migration-orchestration-20261014/previous-regressions.log>) |
| Весь existing pytest regression набор, включая 6 bot tests/SQLite health | **75/75 PASS**; [pytest-regressions.log](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/artifacts/migration-orchestration-20261014/pytest-regressions.log>); одно существующее предупреждение о Starlette/httpx deprecation |
| Прежний синтетический partial-upgrade archive: настоящий restore, аварийная остановка всего PostgreSQL после I03 ALTER, WAL recovery, resume, business snapshot и uncertain reservations | **8/8 PASS**; [restart-rehearsal-results.json](</Users/tamerlanidrisov/Documents/ChatGPT/Freeman One/artifacts/migration-orchestration-20261014/restart-rehearsal-results.json>) |
| Ruff correctness по app/scripts/tests и F/I для новых модулей | PASS |

48 и 439 частично пересекаются: 28 Telegram checks повторены после финальных lifecycle изменений. Не суммировать их как уникальное покрытие. PostgreSQL реальные; Telegram, платежные провайдеры и RemnaWave в regression fixtures подменены. HTTP lifecycle проверялся через реальный ASGI app, browser E2E в этой задаче не проводился.

В тесте concurrent READY startup seed/publication conversion и рабочие jobs подменены: он доказывает совместимые gates и отсутствие schema upgrades, а не конкурентную корректность прежних startup data transforms. Для fresh install первый startup выполнять одним экземпляром; полный bootstrap данных и последующий запуск replicas проверить на staging-копии. Эта задача не переписывала seed/publication services.

Архив содержал пять клиентов/подписок/ключей, четыре старых тарифа, восемь платежей/guest orders, ledger/holds/devices/support, четыре creation journals и partial renewal. Их business snapshots и checkout idempotency/order metadata сохранились. Intentional account enrichment и новые journal/reservation fields сравнивались отдельно. После crash остались 001–007, I03 ALTER откатился, runner применил 008–011. Reservations остались `reserved, reserved, released, consumed`.

В первой версии rehearsal harness проверка после DDL использовала старые prepared statements и получила `InvalidCachedStatementError`. Переподключение перед post-upgrade проверкой исправило harness и соответствует обязательному restart всех runtime connections в runbook. Application services и migrations ради этого не менялись. Локальный стенд остановлен после проверок.

## Точные команды для будущего контролируемого rollout

**Эти команды здесь не исполнялись против production.** Использовать только после отдельного разрешения и выполнения prerequisites из DEPLOYMENT-READINESS. Compose должен уже ссылаться на проверенный immutable release с runner; конфигурация и secrets должны указывать на ожидаемую PostgreSQL, без их вывода в терминал/отчёт. Команды приведены для текущей общей service `bot` (website + bot + workers); остановить также все отдельные replicas/scripts, если они существуют.

1. Повторить upgrade/check на обезличенной копии действующей базы и final image. Проверить roles/grants, encrypted root/config/data volume backup, backup restore и startup data initialization. Приостановить внешние monitor/restart jobs на maintenance window. Предусмотреть проверенный webhook ingress/replay или provider retry + server-side payment reconciliation; не выдавать ложный successful ACK.
2. Закрыть новые customer/admin writes, остановить всех старых writers и дождаться завершения транзакций/API calls. Подготовить согласованный образ и rollback checkpoint. В каталоге утверждённого Compose:

```sh
docker compose stop -t 60 bot
```

3. Снять финальные database/config/root-key/data-volume backups и checkpoint pending payments/journals/reservations. Текущий встроенный database backup, с тем же env/volume и без запуска приложения:

```sh
docker compose run --rm --no-deps bot python -m app.utils.backup
```

Подтвердить archive hash/TOC и restore в отдельную базу. Этот command не сохраняет data volume, roles или encryption key — они нужны отдельно.

4. Read-only preflight (на старой схеме `check` ожидаемо возвращает 1 и NOT READY). Зафиксировать JSON; не скрывать ошибку shell-конструкцией:

```sh
docker compose run --rm --no-deps bot python -m app.database.migrations check
```

5. Единственный offline executor; лог сохранить. При ненулевом exit **не запускать приложение**:

```sh
docker compose run --rm --no-deps bot python -m app.database.migrations upgrade --lock-timeout 5 --statement-timeout 300
docker compose run --rm --no-deps bot python -m app.database.migrations check
```

Требование: оба exit 0, JSON READY, полный непрерывный journal, ожидаемые data diffs. Exit 1 = NOT READY после read-only gate; exit 2 = execution/config/history/lock failure. `MigrationBusy` требует остановки другого owner, а не удаления lock/journal. При interrupted step устранить причину и повторить **ту же** upgrade команду с тем же release; не запускать отдельных migration modules вручную и не редактировать applied rows/checksums. Failed JSON/log сообщает step ID и тип ошибки, не credentials/SQL parameters.

6. Проверить ownership/FKs, финансовые суммы/provider IDs, paid terms, checkout snapshots/idempotency, legacy tariff bindings и reservation states. При F03 duplicate durations не восстанавливать старый UNIQUE(months). Прежние processes/connection pools не использовать: они могут держать старые prepared plans и не соблюдают новые fences.
7. Запустить только согласованную версию, проверить readiness и совместный bot/worker lifecycle:

```sh
docker compose up -d --no-deps bot
curl --fail --silent --show-error http://127.0.0.1:8000/health
```

Требовать HTTP 200 + `schema.status: READY`; Telegram может быть degraded без нарушения website readiness. Восстановить monitor jobs только после успешных gates. Если первоначальный startup получил NOT READY и миграции затем завершились, restart `bot` обязателен — процесс сам не включит обработку.
8. Контролируемо открыть traffic, наблюдать errors/pool/leases и pending recovery. Сначала reconcile неопределённые remote/payment outcomes, не создавать новый charge/key/renewal. Сохранить release/migration hashes и post-upgrade evidence.

## Оставшиеся блокеры и ограничения

- Нет обезличенной копии действующей базы: неизвестны drift, конфликты, объёмы, права и production RTO. Синтетика её не заменяет.
- Security release gates из SECURITY-REVIEW остаются открытыми; D02 rollback compatibility и D03/D04 из deployment report не устранены этим runner.
- Shared/exclusive locks защищают экземпляры **нового** кода. Старые версии и произвольные SQL/scripts обязаны быть остановлены организационно. Runner нельзя использовать через transaction-pooling PgBouncer: нужен direct PostgreSQL или session pooling; этот topology не проверялся.
- Runtime требует один dedicated schema connection на process; включить его в connection budget вместе с Telegram ownership connections. Потеря lease не отменяет уже совершённые внешние события: journals/reconciliation и operator quiesce всё равно обязательны.
- Не проверены production grants/roles, реальные внешние providers, полноценный final-image startup на production-copy, многогигабайтные upgrades и весь browser flow. Ничего не развёрнуто и не применено к production.
- Rollback после новых payments/keys должен сохранять текущую DB/journals и использовать совместимый код/forward repair. Нельзя восстановить старый dump поверх новых финансовых событий, удалять migration ledger или удалять customer data ради прохождения gates.
