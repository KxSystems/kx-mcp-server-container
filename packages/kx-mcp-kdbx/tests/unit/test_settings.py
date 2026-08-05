"""Tests for AppSettings.

The kdb-x backend is mount-only: the container owns transport/host/port (`KX_MCP_*`), so the
bundle's `AppSettings` carries only DB connection config (`KDBX_DB_*`). There is no standalone CLI
to parse — these tests cover defaults, immutability, and env-var resolution of the DB config.
"""
import pytest
from kx_mcp_kdbx.settings import AppSettings, KDBConfig


@pytest.fixture
def app_settings():
    """Provide fresh AppSettings instance for tests."""
    return AppSettings()


def test_app_settings_db_only(app_settings):
    """AppSettings is DB-only now that serving config moved to the container."""
    # No serving config attribute survives the mount-only shrink.
    assert not hasattr(app_settings, "mcp")

    # DB defaults
    assert app_settings.db.host == "127.0.0.1"
    assert app_settings.db.port == KDBConfig().port
    assert app_settings.db.timeout == 1
    assert app_settings.db.retry == 2
    assert app_settings.db.aimeta_cache_ttl == 300


def test_db_config_frozen(app_settings):
    """The DB config is frozen — it cannot be modified at runtime."""
    with pytest.raises(Exception):
        app_settings.db.host = "other-host"


def test_env_variables_with_mock(mocker):
    """KDBX_DB_* environment variables override default DB configuration values."""
    mocker.patch.dict('os.environ', {'KDBX_DB_HOST': 'test-host', 'KDBX_DB_PORT': '5011'})
    # Create fresh instance that will read the mocked environment
    settings = AppSettings()
    assert settings.db.host == "test-host"
    assert settings.db.port == 5011


def test_data_gate_defaults_off(app_settings):
    """The data gate is opt-in — default off preserves identity-assertion behavior byte-for-byte."""
    assert app_settings.db.data_gate is False


def test_aimeta_cache_can_be_disabled_but_not_negative():
    assert KDBConfig(aimeta_cache_ttl=0).aimeta_cache_ttl == 0
    with pytest.raises(Exception):
        KDBConfig(aimeta_cache_ttl=-1)


def test_data_gate_requires_assert_identity():
    """KDBX_DB_DATA_GATE without KDBX_DB_ASSERT_IDENTITY is a config error (fail at startup):
    the gate consults the q policy on the *bound* handle, and an unbound handle default-denies
    everything — refusing the combination beats denying every query at runtime."""
    with pytest.raises(Exception, match="ASSERT_IDENTITY"):
        KDBConfig(data_gate=True, assert_identity=False)


def test_data_gate_with_assertion_is_valid():
    cfg = KDBConfig(data_gate=True, assert_identity=True, username="svc", password="pw")
    assert cfg.data_gate is True and cfg.assert_identity is True


def test_assert_identity_requires_service_account_credential():
    """KDBX_DB_ASSERT_IDENTITY without a service-account username+password is a config error: the
    q-side `.kx.auth.bind` gate authorises the caller by its authenticated login (`.z.u`), so an
    empty credential can never satisfy a configure()d host — fail fast at startup, not at runtime."""
    with pytest.raises(Exception, match="service-account credential"):
        KDBConfig(assert_identity=True)  # no username/password
    with pytest.raises(Exception, match="service-account credential"):
        KDBConfig(assert_identity=True, username="svc")  # username but no password


def test_assert_identity_with_credential_is_valid():
    cfg = KDBConfig(assert_identity=True, username="svc", password="pw")
    assert cfg.assert_identity is True and cfg.username == "svc"


def test_assert_identity_off_needs_no_credential():
    """Off (the default single-principal posture) places no credential requirement."""
    cfg = KDBConfig()
    assert cfg.assert_identity is False
