"""Explicit F03 migration. Never invoked by application startup."""

from sqlalchemy import inspect, text


async def upgrade(engine):
    if engine.dialect.name != "postgresql":
        raise ValueError("Tariff identity migration requires PostgreSQL")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        # Freeze legacy duration bindings before admitting duplicate durations.
        # This short, transactional migration performs no remote requests.
        await connection.exec_driver_sql("LOCK TABLE tariffs IN ACCESS EXCLUSIVE MODE")
        await connection.execute(
            text("""
            INSERT INTO app_settings (key, value)
            SELECT 'legacy_tariff:' || months::text, min(id)::text
            FROM tariffs GROUP BY months HAVING count(*) = 1
            ON CONFLICT (key) DO NOTHING
        """)
        )
        constraints, indexes = await connection.run_sync(
            lambda c: (
                inspect(c).get_unique_constraints("tariffs"),
                inspect(c).get_indexes("tariffs"),
            )
        )
        quote = connection.dialect.identifier_preparer.quote
        for item in constraints:
            if item["column_names"] == ["months"]:
                await connection.exec_driver_sql(
                    f"ALTER TABLE tariffs DROP CONSTRAINT {quote(item['name'])}"
                )
        for item in indexes:
            if (
                item.get("unique")
                and item.get("column_names") == ["months"]
                and not item.get("duplicates_constraint")
                and not item.get("dialect_options", {}).get("postgresql_where")
            ):
                await connection.exec_driver_sql(f"DROP INDEX {quote(item['name'])}")
