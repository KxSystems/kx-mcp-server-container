from pathlib import Path
from typing import Literal
from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the packaged embeddings CSV relative to this module, not the process CWD — the container
# launcher (`kx-mcp`) runs from an arbitrary working dir.
_DEFAULT_EMBEDDING_CSV = str(Path(__file__).resolve().parent / "utils" / "embeddings.csv")


class KDBAIConfig(BaseSettings):
    """KDB.AI database connection and search configuration"""

    model_config = SettingsConfigDict(
        env_prefix="KDBAI_DB_",
        frozen=True,
        env_file='.env',
        extra="ignore",
    )

    host: str = Field(
        default="127.0.0.1",
        description="KDB.AI server hostname or IP address [env: KDBAI_DB_HOST]"
    )
    port: int = Field(
        default=8082,
        description="KDB.AI server port number [env: KDBAI_DB_PORT]"
    )
    username: str = Field(
        default="",
        description="Username for KDB.AI authentication [env: KDBAI_DB_USERNAME]"
    )
    password: SecretStr = Field(
        default=SecretStr(""),
        description="Password for KDB.AI authentication [env: KDBAI_DB_PASSWORD]"
    )
    mode: Literal["rest", "qipc"] = Field(
        default="qipc",
       description="API mode: 'qipc' (fast binary protocol) or 'rest' (HTTP API) [env: KDBAI_DB_MODE]"
    )
    rest_protocol: Literal["http", "https"] = Field(
        default="http",
        description="Select protocol for REST mode, not considered for QIPC mode [env: KDBAI_DB_REST_PROTOCOL]"
    )
    qipc_tls: bool = Field(
        default=False,
        description="""Enable TLS for QIPC mode, not considered for REST mode.
        When using TLS with QIPC you will need to set the environment variable `KX_SSL_CA_CERT_FILE` that points
        to the certificate on your local filesystem that your TLS proxy is using. For local development and testing
        you can set `KX_SSL_VERIFY_SERVER=NO` to bypass this requirement [env: KDBAI_DB_QIPC_TLS]
        """
    )
    database_name: str = Field(
        default="default",
        description="Default database name to use for operations [env: KDBAI_DB_DATABASE_NAME]"
    )
    retry: int = Field(
        default=2,
        description="Number of connection retry attempts on failure [env: KDBAI_DB_RETRY]"
    )
    k: int = Field(
        default=5,
        description="Default number of results to return from vector searches [env: KDBAI_DB_K]"
    )
    vector_weight: float = Field(
        default=0.7,
        description="Weight for vector similarity in hybrid search (0.0-1.0) [env: KDBAI_DB_VECTOR_WEIGHT]"
    )
    sparse_weight: float = Field(
        default=0.3,
        description="Weight for text similarity in hybrid search (0.0-1.0) [env: KDBAI_DB_SPARSE_WEIGHT]"
    )
    embedding_csv_path: str = Field(
        default = _DEFAULT_EMBEDDING_CSV,
        description = "Path to embeddings csv [env: KDBAI_DB_EMBEDDING_CSV_PATH]"
    )

    # --- Outbound identity (for an OAuth-protected KDB.AI server, AUTH_TYPE=oauth) -----------------
    # The bundle delegates the actual grant to the shared
    # kx_auth_core.exchange() seam (one outbound implementation + the kx_mcp.audit line), keeping
    # only the bundle-local token-cache lifecycle. Empty outbound_strategy = off -> fall back to the
    # static username/password path (a KDB.AI server in AUTH_TYPE=static / no-auth mode), unchanged.
    outbound_strategy: str = Field(
        default="",
        description=(
            "Outbound identity strategy for an OAuth-protected KDB.AI server. '' (default) = off: use "
            "the static username/password path. 'service_account' = the container's own workload "
            "identity via the OIDC client-credentials grant, delegated to kx_auth_core.exchange(). "
            "'passthrough' = forward the inbound caller's bearer (the validated principal) as the "
            "connection credential, with a per-principal connection cache. Both work in qipc (bearer "
            "as the connection password) and rest (bearer handed to the SDK via a file-backed "
            "external_token oauth config). [env: KDBAI_DB_OUTBOUND_STRATEGY]"
        ),
    )
    token_url: str = Field(
        default="",
        description="OIDC token endpoint for the outbound grant (service_account) [env: KDBAI_DB_TOKEN_URL]",
    )
    client_id: str = Field(
        default="",
        description=(
            "OIDC client id for the outbound client-credentials grant. Distinct from `username` above, "
            "which is the static-auth identity [env: KDBAI_DB_CLIENT_ID]"
        ),
    )
    client_secret: SecretStr = Field(
        default=SecretStr(""),
        description="OIDC client secret paired with client_id for the outbound grant [env: KDBAI_DB_CLIENT_SECRET]",
    )
    audience: str = Field(
        default="",
        description=(
            "Target audience for the outbound token (Keycloak); must match the KDB.AI server's "
            "OAUTH_CLIENT_ID (the aud it validates). For Entra, leave empty and set the resource scope "
            "via `scopes` (e.g. 'api://<client-id>/.default') instead [env: KDBAI_DB_AUDIENCE]"
        ),
    )
    scopes: str = Field(
        default="",
        description=(
            "Optional space-separated scopes for the outbound token request. For Entra "
            "client-credentials use the resource default scope, e.g. 'api://<client-id>/.default' "
            "[env: KDBAI_DB_SCOPES]"
        ),
    )
    ssl_verify: bool = Field(
        default=True,
        description=(
            "Verify TLS on the outbound OIDC token call exchange() makes. Set False for a self-signed "
            "dev IdP [env: KDBAI_DB_SSL_VERIFY]"
        ),
    )


class AppSettings(BaseSettings):
    """KDB.AI backend (bundle) settings.

    The KDB.AI backend is **mount-only**: the composition container (`kx-mcp-core`) owns transport,
    host, port, and logging via the `KX_MCP_*` prefix, so this bundle no longer carries any serving
    config (the retired `ServerConfig` / `KDBAI_MCP_*` prefix). `build_server()` just returns a
    configured FastMCP for the container to `mount()`. All that remains here is the backend's own
    connection + search config (`KDBAI_DB_*`), still env/`.env`-parsed so a mounted bundle picks up
    its connection.
    """

    db: KDBAIConfig = Field(
        default_factory=KDBAIConfig,
        description="KDB.AI database connection and search configuration"
    )
