"""The Acme backend's config fragment — its own env-prefixed, frozen settings model.

`frozen=True` because the config becomes an `lru_cache` key in `utils/acme.py` (distinct configs →
distinct cached connections). A real backend adds TLS, auth, timeout/retry, and outbound-strategy
fields under the same prefix; this keeps just enough to show the shape.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class AcmeConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACME_DB_", frozen=True, extra="ignore")

    host: str = "127.0.0.1"
    port: int = 9000
    timeout: int = 1
