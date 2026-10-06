import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:changeme@localhost:5439/aleksandr_kim_small")
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme").encode()
SCHEMA = "aleksandr_kim"
TOKEN_TTL_SECONDS = 8 * 3600
