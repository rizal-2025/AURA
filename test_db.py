from app.db.database import engine

print(engine)

with engine.connect() as conn:
    print("✅ Berhasil terhubung ke PostgreSQL!")