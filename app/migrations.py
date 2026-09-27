"""Versioned migrations (each applied once, recorded in schema_migrations) + plan rows from pinned config."""
from sqlalchemy import text
from .db import Base, Plan, SessionLocal, engine
from .pricing import PLANS


MIGRATIONS = [("001_initial_schema", lambda conn: Base.metadata.create_all(conn))]


def migrate():
    applied = []
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(100) PRIMARY KEY)"))
        done = {r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))}
        for v, fn in MIGRATIONS:
            if v not in done:
                fn(conn)
                conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": v})
                applied.append(v)
    db = SessionLocal()   # plans mirror the pinned constants in pricing.py
    for code, p in PLANS.items():
        row = db.get(Plan, code) or Plan(code=code)
        row.name, row.api_calls_limit, row.ai_tokens_limit, row.base_fee_micros = p["name"], p["api_calls"], p["ai_tokens"], p["base_fee_micros"]
        db.merge(row)
    db.commit()
    db.close()
    return applied


if __name__ == "__main__":
    print("applied:", migrate() or "up to date")
