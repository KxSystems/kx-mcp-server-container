"""Config parsing: the mount-only bundle owns only KDBAI_DB_* (connection + search).

The KDBAI_MCP_* serving prefix (ServerConfig) was retired with the mount-only refactor — the
container owns transport/host/port via KX_MCP_*.
"""

from kx_mcp_kdbai.settings import KDBAIConfig


def test_db_config_env_prefix(monkeypatch):
    monkeypatch.setenv("KDBAI_DB_HOST", "kdbai-prod")
    monkeypatch.setenv("KDBAI_DB_PORT", "8083")
    monkeypatch.setenv("KDBAI_DB_MODE", "rest")
    cfg = KDBAIConfig()
    assert cfg.host == "kdbai-prod"
    assert cfg.port == 8083
    assert cfg.mode == "rest"


def test_db_config_defaults():
    cfg = KDBAIConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8082
    assert cfg.mode == "qipc"
    assert cfg.database_name == "default"
    # embeddings CSV default resolves to the packaged file (not a CWD-relative path)
    assert cfg.embedding_csv_path.endswith("kx_mcp_kdbai/utils/embeddings.csv")
