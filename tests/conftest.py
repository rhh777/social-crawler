import os
import uuid

import pytest
from sqlalchemy import create_engine

from social_crawler.storage.store import Store


@pytest.fixture(autouse=True)
def isolate_dotenv(tmp_path, monkeypatch):
    """Entry-point tests must never load a developer's credentials or database."""
    path = tmp_path / "test.env"
    path.write_text("")
    monkeypatch.setenv("CRAWLER_ENV_FILE", str(path))


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request, tmp_path):
    admin = None
    if request.param == "sqlite":
        result = Store(f"sqlite:///{tmp_path / 'test.db'}")
    else:
        url = os.environ.get("CRAWLER_TEST_DATABASE_URL")
        if not url:
            pytest.skip("Set CRAWLER_TEST_DATABASE_URL to an isolated local PostgreSQL")
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        admin = create_engine(url)
        schema = "test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        result = Store(url)
        result.engine.dispose()
        result.engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        result.test_schema = schema
    result.initialize()
    yield result
    result.close()
    if admin:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()
