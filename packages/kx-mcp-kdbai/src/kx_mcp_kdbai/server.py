import sys
import logging
from fastmcp import FastMCP
from kx_mcp_kdbai.settings import AppSettings
from kx_mcp_kdbai.addins import register_addins

# The FastMCP server name. The bundle is mount-only, so the container (kx-mcp-core) owns
# transport/host/port; only the name is decided here (was ServerConfig.server_name, now retired).
SERVER_NAME = "kdbai"


class McpServer:
    def __init__(self, config: AppSettings):
        self.logger = logging.getLogger(__name__)

        self.db_config = config.db
        self.logger.info(f"KDBAIConfig: {self.db_config=}")

        # FastMCP 3.x: transport/host/port move to the container's run() — not ctor args here.
        self.mcp = FastMCP(SERVER_NAME)

        self._check_kdbai_connection()

        # Per-instance context for instance-safety: tools read this off the server object via
        # `ctx.fastmcp` at call time, so each mounted backend routes to ITS OWN client (config) —
        # never a module global. This is what lets two KDB.AI backends coexist in one container.
        self.mcp._kdbai_config = self.db_config  # type: ignore[attr-defined]  # instance-safety stash (read via getattr)

        self._register_addins()

    def _check_kdbai_connection(self):
        """Check if KDB.AI service is reachable and accessible."""
        try:
            from kdbai_client import KDBAIException
            from kx_mcp_kdbai.utils.kdbai import _open_session

            protocol = self.db_config.rest_protocol if self.db_config.mode == "rest" else "http"
            endpoint = f"{protocol}://{self.db_config.host}:{self.db_config.port}"

            # `passthrough` forwards the inbound caller's bearer at call time, so there is NO principal
            # at startup — and against an OAuth-enforcing server an unauthenticated open would be
            # *rejected*, not "reachable". So for passthrough the pre-flight is a tokenless socket
            # reachability probe (the passthrough-only pre-flight tolerates a
            # 401); the real auth is exercised per-request. For service_account/static the open below
            # carries credentials and exercises the *real* auth path.
            if self.db_config.outbound_strategy == "passthrough":
                import socket
                with socket.create_connection((self.db_config.host, self.db_config.port), timeout=5):
                    pass
                self.logger.info(
                    f"KDB.AI connectivity check: SUCCESS - {endpoint} reachable "
                    "(passthrough: per-request bearer, auth deferred to call time)"
                )
                return

            # _open_session is the single source of truth for outbound auth — when a `service_account`
            # strategy is configured it mints the workload-identity bearer (via the shared exchange()
            # seam) and injects it per mode (qipc password / REST external_token oauth file), so the
            # pre-flight exercises the *real* auth path.
            client = _open_session(self.db_config)

            self.logger.info(
                f"KDB.AI connectivity check: SUCCESS - {self.db_config.mode} {endpoint} is accessible"
            )

            client.close()

        except OSError as e:
            # passthrough socket probe: a genuine connection failure (refused / timeout / DNS) is fatal.
            self.logger.error(f"KDB.AI connectivity check: FAILED - {endpoint} unreachable ({e})")
            self.logger.error("Check KDBAI_DB_HOST / KDBAI_DB_PORT and that KDB.AI is reachable. Exiting...")
            sys.exit(1)

        except (ValueError, RuntimeError) as e:
            # Outbound-auth misconfiguration / failed token mint (KDBAI_DB_OUTBOUND_* or an unreachable
            # IdP) — a clear operator error, not a transient KDB.AI connectivity blip. Same fail-fast
            # contract as the connectivity check below.
            self.logger.error(f"KDB.AI outbound-auth check: FAILED - {e}")
            self.logger.error(
                "KDB.AI MCP server cannot obtain an outbound credential — check KDBAI_DB_OUTBOUND_* "
                "(strategy / token_url / client_id / client_secret). Exiting..."
            )
            sys.exit(1)
        except KDBAIException as e:
            self.logger.error(
                f"KDB.AI connectivity check: FAILED - {endpoint} ({e})"
            )
            if self.db_config.mode == 'qipc' and self.db_config.qipc_tls:
                self.logger.error("You are attempting to connect using QIPC with TLS enabled. Ensure you have set the environment variable `KX_SSL_CA_CERT_FILE` that CA certificate on your local filesystem that your TLS proxy is using. For local development and testing you can set `KX_SSL_VERIFY_SERVER=NO`")
            if "authentication error" in str(e).lower():
                self.logger.error(
                    "Authentication is enabled on KDB.AI server - set valid "
                    "KDBAI_DB_USERNAME and KDBAI_DB_PASSWORD environment variables"
                )
            if "failed to open a session" in str(e).lower():
                self.logger.error(f"Check your KDB.AI Server is running and accepting '{self.db_config.mode}' connections on port '{self.db_config.port}'")
            self.logger.error(
                "KDB.AI MCP server cannot function without connection to a KDB.AI database. Exiting..."
            )
            sys.exit(1)

    def _register_addins(self):
        try:
            registered = register_addins(self.mcp)
            self.logger.info(f"Successfully registered {len(registered)} add-in(s)")
            for name in registered:
                self.logger.debug(f"Registered add-in: {name}")
        except Exception as e:
            self.logger.error(f"Failed to register add-ins: {e}")
            raise


app_settings = AppSettings()


def build_server(config: AppSettings | None = None) -> FastMCP:
    """Build and return this bundle's configured FastMCP server — the extension seam.

    This is what the container composes (``parent.mount(build_server(), namespace="kdbai")``).
    The connectivity pre-flight and primitive registration run **eagerly** here, returning a
    ready-to-mount server — deliberately, because under a 3.x *direct* mount a child subserver's
    lifespan does not run.

    Mount-only: there is no standalone serving entry point — the container owns transport/host/port.
    Run the KDB.AI backend via the container: ``uv run kx-mcp --bundles kdbai``.
    """
    if config is None:
        config = app_settings
    return McpServer(config).mcp
