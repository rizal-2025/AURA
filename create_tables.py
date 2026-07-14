from app.db.base import Base
from app.db.database import engine

Base.metadata.create_all(bind=engine)

print("✅ Semua tabel berhasil dibuat!")