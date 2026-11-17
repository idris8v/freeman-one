# Freeman One deployment ownership

Read `WEBSITE_PUBLICATION.md` before any publication work.

- Website and Telegram bot share a production image. The bot is also developed
  on another computer. This checkout is NOT the authoritative complete backend.
- Website-only releases MUST use `/opt/freemanone/publish-web-safe.py` on the
  server with an archive containing only reviewed `app/web` files, based on the
  currently running source. Run its `--check` validation first.
- Never deploy a whole Git archive/image from this checkout over production.
  The previous `artifacts/**/release.py` workflows are retired. Never remove or
  bypass their retirement guards to publish website changes.
- Never include `app/bot`, `app/services`, `app/database`, `app/main.py`, config,
  dependencies or migrations in a website bundle. Shared-backend changes need
  explicit coordination and reconciliation against the current bot sources.
- Compare live source hashes before preparing a release. Do not overwrite newer
  website changes from another developer. A stale image label alone is not proof
  that a combined release matches this Git commit.
- Preserve database, runtime settings, secrets and live payment mode. Website
  publication authorization does not authorize creating a real payment.
- The safe publisher still restarts the shared container; it preserves bot code,
  but does not provide independently running website/bot processes.
