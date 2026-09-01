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
    def test_returns_a_real_client_when_mock_is_unset(self):
        client = build_rentcast_client('real-key')

        assert not isinstance(client.session, MockRentCastSession)
        assert client.api_key == 'real-key'

    def test_returns_a_mock_client_when_mock_is_enabled(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '1')

        client = build_rentcast_client('real-key')

        assert isinstance(client.session, MockRentCastSession)

    def test_mock_mode_works_without_a_real_api_key(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '1')

        client = build_rentcast_client(None)

        assert isinstance(client.session, MockRentCastSession)
        assert client.api_key  # RentCastClient itself requires a truthy value

    def test_refuses_to_activate_alongside_database_url(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', '1')
        monkeypatch.setenv('DATABASE_URL', 'postgres://example')

        with pytest.raises(RuntimeError):
            build_rentcast_client('real-key')

    def test_a_value_other_than_1_does_not_enable_mock_mode(self, monkeypatch):
        monkeypatch.setenv('RENTCAST_MOCK', 'true')

        client = build_rentcast_client('real-key')

        assert not isinstance(client.session, MockRentCastSession)
