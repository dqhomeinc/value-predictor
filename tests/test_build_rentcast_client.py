import pytest

from integrations.rentcast_mock import MockRentCastSession
from services.analyzer import build_rentcast_client


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Belt and suspenders: these tests are all about which env vars are
    # set, so make sure neither leaks in from the real shell/CI environment.
    monkeypatch.delenv('RENTCAST_MOCK', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)


class TestBuildRentcastClient:
    def test_defaults_to_mock_when_database_url_is_unset(self):
        # No DATABASE_URL (local dev) and no explicit RENTCAST_MOCK ->
        # mock is the default, so routine local testing never spends
        # real RentCast quota by accident.
        client = build_rentcast_client('real-key')

        assert isinstance(client.session, MockRentCastSession)

    def test_defaults_to_a_real_client_when_database_url_is_set(self, monkeypatch):
        # DATABASE_URL set (Render/production, or a local Postgres-backed
        # run) and no explicit RENTCAST_MOCK -> must default to real data,
        # never synthetic.
        monkeypatch.setenv('DATABASE_URL', 'postgres://example')

        client = build_rentcast_client('real-key')

        assert not isinstance(client.session, MockRentCastSession)
        assert client.api_key == 'real-key'

    def test_explicit_zero_opts_out_of_mock_locally(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '0')

        client = build_rentcast_client('real-key')

        assert not isinstance(client.session, MockRentCastSession)

    def test_explicit_one_forces_mock_on(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '1')

        client = build_rentcast_client('real-key')

        assert isinstance(client.session, MockRentCastSession)

    def test_mock_mode_works_without_a_real_api_key(self):
        client = build_rentcast_client(None)

        assert isinstance(client.session, MockRentCastSession)
        assert client.api_key  # RentCastClient itself requires a truthy value

    def test_refuses_to_activate_alongside_database_url(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '1')
        monkeypatch.setenv('DATABASE_URL', 'postgres://example')

        with pytest.raises(RuntimeError):
            build_rentcast_client('real-key')

    def test_a_value_other_than_1_does_not_force_mock_on(self, monkeypatch):
        # Explicit-but-not-'1' is treated the same as '0': an opt-out, not
        # a typo that silently falls through to the mock default.
        monkeypatch.setenv('RENTCAST_MOCK', 'true')
        monkeypatch.setenv('DATABASE_URL', 'postgres://example')

        client = build_rentcast_client('real-key')

        assert not isinstance(client.session, MockRentCastSession)
