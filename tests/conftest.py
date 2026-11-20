import os

# Settings are required at startup; tests that don't touch a database or NetBox only need placeholders.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://sharpwire:sharpwire@localhost:5433/sharpwire_test")
os.environ.setdefault("NETBOX_URL", "https://netbox.test.invalid")
os.environ.setdefault("NETBOX_TOKEN", "test-placeholder-token")
