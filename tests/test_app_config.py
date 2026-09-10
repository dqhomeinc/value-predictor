import pytest

from app import create_app


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'test-secret')
    return monkeypatch


class TestDatabasePoolConfig:
    """
    Neon closes idle connections when it suspends, and a pool that reuses a
    closed connection fails the next request with a 500. These pin the
    protection to the Postgres configuration production actually uses.
    """

    def test_postgres_checks_pooled_connections_before_reuse(self, env):
        env.setenv('DATABASE_URL', 'postgresql://user:pw@localhost:5432/db')

        app = create_app()

        assert app.config['SQLALCHEMY_ENGINE_OPTIONS']['pool_pre_ping'] is True

    def test_neon_style_url_gets_the_same_protection(self, env):
        # Neon hands out postgres:// URLs with query parameters; create_app
        # rewrites the scheme and strips the query, and the check has to
        # survive that rewriting.
        env.setenv('DATABASE_URL', 'postgres://user:pw@ep-example.neon.tech/db?sslmode=require')

        app = create_app()

        assert app.config['SQLALCHEMY_ENGINE_OPTIONS']['pool_pre_ping'] is True

    def test_sqlite_is_left_alone(self, env):
        env.setenv('DATABASE_URL', 'sqlite:///:memory:')

        app = create_app()

        assert 'pool_pre_ping' not in app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {})
