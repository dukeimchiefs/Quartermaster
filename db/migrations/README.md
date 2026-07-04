# Migrations

Deferred until Development Priority #12 (Hardening) in `CLAUDE.md`. Once the
schema stabilizes past the prototype phase, this directory will hold Alembic
migrations generated against `db/models.py`. Until then, `db/schema.sql` and
`db.models.init_db()` are the source of truth — no migration tooling yet.
