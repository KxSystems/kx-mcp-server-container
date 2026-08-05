"""KDB.AI backend connection-setting defaults and environment resolution."""

from kx_mcp_kdbai.settings import KDBAIConfig


def test_qipc_defaults_to_port_8082():
    cfg = KDBAIConfig(_env_file=None)

    assert cfg.mode == "qipc"
    assert cfg.port == 8082


def test_rest_defaults_to_port_8081():
    cfg = KDBAIConfig(_env_file=None, mode="rest")

    assert cfg.port == 8081


def test_explicit_port_wins_for_either_mode():
    assert KDBAIConfig(_env_file=None, mode="rest", port=9443).port == 9443
    assert KDBAIConfig(_env_file=None, mode="qipc", port=9444).port == 9444


def test_environment_mode_selects_default_port(monkeypatch):
    monkeypatch.setenv("KDBAI_DB_MODE", "rest")

    cfg = KDBAIConfig(_env_file=None)

    assert cfg.mode == "rest"
    assert cfg.port == 8081


def test_environment_port_overrides_mode_default(monkeypatch):
    monkeypatch.setenv("KDBAI_DB_MODE", "rest")
    monkeypatch.setenv("KDBAI_DB_PORT", "9081")

    cfg = KDBAIConfig(_env_file=None)

    assert cfg.port == 9081
