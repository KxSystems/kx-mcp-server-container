from pathlib import Path
from pydantic import SecretStr, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the packaged embeddings CSV relative to this module, not the process CWD — the container
# launcher (`kx-mcp`) runs from an arbitrary working dir.
_DEFAULT_EMBEDDING_CSV = str(Path(__file__).resolve().parent / "utils" / "embeddings.csv")

class KDBConfig(BaseSettings):
    """KDB- X database connection settings"""

    model_config = SettingsConfigDict(
        env_prefix="KDBX_DB_",
        frozen=True,
        env_file='.env',
        extra="ignore",
    )

    host: str = Field(
        default="127.0.0.1",
        description="KDB-X server hostname or IP address [env: KDBX_DB_HOST]"
    )
    port: int = Field(
        default=5010,
        description="KDB-X server port number [env: KDBX_DB_PORT]"
    )
    username: str = Field(
        default="",
        description="Username for KDB-X authentication [env: KDBX_DB_USERNAME]"
    )
    password: SecretStr = Field(
        default=SecretStr(""),
        description="Password for KDB-X authentication [env: KDBX_DB_PASSWORD]"
    )
    tls: bool = Field(
        default=False,
        description="""Enable TLS for KDB-X connections.
        When using TLS you will need to set the environment variable `KX_SSL_CA_CERT_FILE` that points
        to the certificate on your local filesystem that your KDB-X server is using. For local development and testing
        you can set `KX_SSL_VERIFY_SERVER=NO` to bypass this requirement [env: KDBX_DB_TLS]"""
    )
    timeout: int = Field(
        default=1,
        description="Timeout in seconds for KDB-X connection attempts [env: KDBX_DB_TIMEOUT]"
    )
    retry: int = Field(
        default=2,
        description="Number of connection retry attempts on failure [env: KDBX_DB_RETRY]"
    )
    embedding_csv_path: str = Field(
        default=_DEFAULT_EMBEDDING_CSV,
        description = "Path to embeddings csv [env: KDBX_DB_EMBEDDING_CSV_PATH]"
    )
    metric: str = Field(
        default="CS",
        description="Distance metric used for vector similarity search (e.g., CS, L2, IP) [env: KDBX_DB_METRIC]"
    )
    k: int = Field(
        default=5,
        description="Default number of results to return from vector searches [env: KDBX_DB_K]"
    )
    assert_identity: bool = Field(
        default=False,
        description="""Enable identity assertion: project the validated inbound principal into a
        q dict and call `.kx.auth.bind[principal]` on the (service-account) connection so q-side
        permission functions can consult the caller. Opt-in — off behaves exactly as the
        single-principal posture (no bind), so a vanilla kdb+ without the `kx.auth` module is
        unaffected. When on, the pre-flight verifies `.kx.auth.bind` is defined on the target.
        [env: KDBX_DB_ASSERT_IDENTITY]"""
    )
    password_file: str = Field(
        default="",
        description="""Path to a file holding the service-account password (read at load), so the
        highest-trust secret can arrive as a K8s/Docker mounted secret instead of sitting in the
        process environment. When set and present, it overrides KDBX_DB_PASSWORD [env: KDBX_DB_PASSWORD_FILE]"""
    )
    data_gate: bool = Field(
        default=False,
        description="""Enable the data-entitlement gate: before running a query, the tool
        path explicitly consults the q-side `.kx.auth.entitled[action;resources]` on the bound
        per-principal handle and acts on the verdict — allow, structured permission_denied, or
        scope-down (filter) via the decision's obligations. Requires KDBX_DB_ASSERT_IDENTITY=true
        (the gate reads the bound principal; unbound handles default-deny everything) and a kx.auth
        module that ships `entitled` (the pre-flight verifies). Opt-in — off, only a host-side
        `.s.e` wrap gates data.
        [env: KDBX_DB_DATA_GATE]"""
    )

    @model_validator(mode="before")
    @classmethod
    def _read_password_file(cls, data):
        """Fold a file-backed secret into `password` before validation (file wins when present)."""
        if isinstance(data, dict):
            pf = data.get("password_file")
            if pf:
                p = Path(pf)
                if p.exists():
                    data["password"] = p.read_text().strip()
        return data

    @model_validator(mode="after")
    def _data_gate_requires_assertion(self):
        """The data gate consults the q policy on the *bound* per-principal handle — without
        identity assertion the handle is never bound and `.kx.auth` default-denies every query.
        Refuse the combination at config time (startup) rather than denying everything at runtime."""
        if self.data_gate and not self.assert_identity:
            raise ValueError(
                "KDBX_DB_DATA_GATE=true requires KDBX_DB_ASSERT_IDENTITY=true — the data gate "
                "consults the q-side policy on the bound per-principal handle; without assertion "
                "the handle is unbound and .kx.auth default-denies everything."
            )
        return self

    @model_validator(mode="after")
    def _assertion_requires_service_account(self):
        """Identity assertion must connect as a real service account: the q side's `.kx.auth.bind`
        gate keys on the caller's authenticated login (`.z.u`), so an empty username/password can
        never satisfy a `configure`d host — bind would deny every assertion. Fail fast at config time
        with a clear message rather than surface a confusing runtime 'denied / connection refusal.
        (The container validates its OWN config here; it does not probe the host's posture.)"""
        if self.assert_identity and not (self.username and self.password.get_secret_value()):
            raise ValueError(
                "KDBX_DB_ASSERT_IDENTITY=true requires a service-account credential — set "
                "KDBX_DB_USERNAME and KDBX_DB_PASSWORD (or KDBX_DB_PASSWORD_FILE). The q-side "
                ".kx.auth.bind gate authorises the caller by its authenticated login, so an empty "
                "credential is refused."
            )
        return self



class AppSettings(BaseSettings):
    """KDB-X backend (bundle) settings.

    The kdb-x backend is **mount-only**: the composition container (`kx-mcp-core`) owns transport,
    host, port, and logging via the `KX_MCP_*` prefix, so this bundle no longer carries any serving
    config. `build_server()` just returns a configured FastMCP for the container to `mount()`. All
    that remains here is the backend's own connection config (`KDBX_DB_*`), still env/`.env`-parsed
    so a mounted bundle picks up its connection.
    """

    db: KDBConfig = Field(
        default_factory=KDBConfig,
        description="KDB-X database connection settings"
    )

