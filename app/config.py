import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:changeme@localhost:5439/aleksandr_kim_small")
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme").encode()
SCHEMA = "aleksandr_kim"
TOKEN_TTL_SECONDS = 8 * 3600
# размер пула соединений: по умолчанию прежние 10; меняется только в опыте с настройкой (второе задание)
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))
