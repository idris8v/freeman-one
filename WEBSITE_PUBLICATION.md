# Safe website publication — Freeman One

This instruction supersedes the earlier full-project publication procedures for
website-only tasks. It does not authorize a new deployment by itself.

## Historical handoff, 16 October 2026

The server has `/opt/freemanone/publish-web-safe.py`. Its source was inspected
read-only, and its validator was tested locally without invoking Docker or any
production write. It permits only regular files/directories below `app/web`,
rejects symlinks, traversal, duplicate paths and oversized archives. It builds
on the **currently running image**, then checks SHA-256 hashes of application
files outside `web` before and after switching. It detects an intervening image
or Compose change, and rolls back its image switch on failure.

The running image during this inspection was
`freemanone-bot:websafe-20261015-235818-1b9a92ff7fd4`.
All **127 tracked `app/web` files matched** this checkout's `1082b265` version;
**17 tracked non-web paths differed**. Those live files belong to ongoing bot /
shared-backend work and must not be overwritten by this local source tree.
No live customer records, keys, payment details or secret configuration were
retrieved. Evidence consists of file hashes and code-path names only, under
ignored `artifacts/publication-handoff-20261016/`.

The Windows handoff paths in the user's message are not local Mac paths. The
actual installed server script and running source hashes were available directly;
no claim is made that the Windows ZIP was imported or its 59 tests rerun here.

## Procedure for the next authorized website change

1. Recheck the running source/image and live `app/web` hashes. Preserve any newer
   changes on either computer; reconcile changes before building an archive.
2. Work and test against the current shared-backend contract. Changes to services,
   models, repositories, startup or dependencies cannot be delivered as web-only
   files. Coordinate these changes with the developer responsible for the bot.
3. Rebuild the five CSS outputs and content-hashed references when styles change.
   Test affected browser flows and relevant backend/API contracts in isolation.
4. Prepare `site-web.tar.gz` with only the reviewed website delta under `app/web`.
   No Git metadata, credentials, data, migrations, dependencies or bot files.
   Include coupled built assets and entry references together. The publisher
   copies files and does not remove obsolete paths; handle removals separately.
5. Upload this archive to a distinct temporary server path. Validate it first:

   ```sh
   python3 /opt/freemanone/publish-web-safe.py /tmp/site-web.tar.gz --check
   ```

6. When the intended change is authorized and verified, publish using:

   ```sh
   python3 /opt/freemanone/publish-web-safe.py /tmp/site-web.tar.gz
   ```

   Preserve the command's reported backup directory and old/new image names.
   Do not run earlier full-source/image deployment helpers.
7. Verify the published website hashes and actual served assets, schema READY,
   HTTP availability, bot polling, unchanged non-web source hashes and unchanged
   payment settings. Test actual expected interface behavior without charging.

## Limits and rollback

This is code-preserving publication, not separate deployment architecture. The
shared `bot` container is recreated, briefly interrupting website/bot processing.
The database is not restored or migrated by this command. Do not use it for
schema changes. It backs up Compose and application source, not a database dump;
retain the normal database backup policy separately.

Do not run bot and website publications simultaneously. The script checks for
an intervening change, but the other computer's release process may use another
lock. Its healthy check covers application/database/bot availability; also verify
schema readiness and published frontend hashes explicitly.

For a rollback, use the old image and protected Compose backup reported by this
publisher, after ensuring another publication has not superseded it. Recreate
only the affected shared application service with `--no-build --no-deps`.
Never restore an older database merely to roll back website code: newer payments
and customer changes must survive. Never print private Compose/environment data.

## Local protection

The three legacy local full-project release launchers are retained as historical
artifacts but now exit immediately with a retirement message, before archiving,
SSH, Docker or service commands. This prevents accidental reuse. Server-side
legacy commands were not altered; all developers must follow this ownership rule.
