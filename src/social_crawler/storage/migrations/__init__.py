from pathlib import Path

from alembic import command
from alembic.config import Config


def migration_config(url: str):
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_database(url: str):
    config = migration_config(url)
    command.upgrade(config, "head")


def downgrade_database(url: str, revision: str):
    config = migration_config(url)
    command.downgrade(config, revision)
