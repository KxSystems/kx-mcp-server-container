import os
import sys
import logging
from packaging import version
from fastmcp import FastMCP
from kx_mcp_kdbx.settings import AppSettings
from kx_mcp_kdbx.addins import register_addins
from kx_mcp_kdbx.utils.aimeta import MetadataCache, detect_metadata, metadata_guidance

# Imported for its registration side effect: self-registers the "kdbx_rbac" capability-check authz
# adapter on the kx_auth_core.authz registry (module-bottom register_authz_adapter call). The
# @authorize decorator on the SQL tool routes a `query`/`kdbx.sql` capability check here when
# KX_MCP_AUTHZ=kdbx_rbac. This import is the sole reachability path — without it,
# decide(strategy="kdbx_rbac") would fail closed on an unknown strategy.
import kx_mcp_kdbx.utils.authz_kx_rbac  # noqa: F401

# Likewise for the "kdbx_entitlements" data-gate adapter (the explicit consult of the q data gate).
# The tools import consult_data_gate directly, so this import is belt-and-braces for the registry —
# but keeping the two adapter registrations visibly side-by-side here is deliberate.
import kx_mcp_kdbx.utils.authz_kx_entitlements  # noqa: F401

# The kdb-x backend is **mount-only**: the composition container (`kx-mcp-core`) owns transport,
# host, and port, so this bundle has no serving entry point and no serving config. The FastMCP
# instance still needs a name, sourced from this module-level constant.
SERVER_NAME = "kdbx"

# Ensure pykx throws error on import if license is not valid
os.environ["PYKX_LICENSED"] = "true"

# Global flag to track AI Libs availability
_ai_libs_available = False

def set_ai_libs_available(available: bool):
    global _ai_libs_available
    _ai_libs_available = available

def is_ai_libs_available() -> bool:
    return _ai_libs_available

class McpServer:

    def __init__(self, config: AppSettings):
        self.logger = logging.getLogger(__name__)

        self.db_config = config.db
        self.logger.info(f"KDBConfig: {self.db_config=}")

        self.ai_libs_available = False
        self.metadata_cache = MetadataCache(self.db_config.aimeta_cache_ttl)

        # Initialize server. Mount-only: the container owns transport/host/port, so the only thing
        # this bundle decides is the server name (the SERVER_NAME constant — no serving config).
        self.mcp = FastMCP(SERVER_NAME)

        self._check_kdb_connection()

        # Per-instance context for instance-safety: the primitives' register_* functions read these
        # off the server object and capture them in their closures, so each mounted backend routes
        # to ITS OWN connection (config) and feature flags — never a module global. This is what
        # lets two kdb-x backends (different host+ports) coexist in one container.
        self.mcp._kdbx_config = self.db_config  # type: ignore[attr-defined]  # instance-safety stash (read via getattr)
        self.mcp._kdbx_ai_libs = self.ai_libs_available  # type: ignore[attr-defined]
        self.mcp._kdbx_metadata_cache = self.metadata_cache  # type: ignore[attr-defined]

        self._register_addins()
        self._gate_ai_tools()
        if is_ai_libs_available():
            self._preload_embedding_models()

    def _check_kdb_connection(self):
        """Check if KDB-X service is reachable and accessible."""
        try:
            import pykx as kx
            from pykx.exceptions import QError
        except Exception as e:
            self.logger.error(f"Failed to import pykx: {e}")
            self.logger.error("KDB-X Python is required to connect to KDB-X. Please ensure you have a valid license.")
            self.logger.error("Set the QLIC environment variable to point to your license directory.")
            sys.exit(1)

        try:
            conn = kx.SyncQConnection(
                host=self.db_config.host,
                port=self.db_config.port,
                username=self.db_config.username,
                password=self.db_config.password.get_secret_value(),
                timeout=self.db_config.timeout,
                tls=self.db_config.tls,

            )
            # Try to get kdbx version first, fall back to kdb+ version
            try:
                kdb_version = conn('.z.v`version').py().decode('utf-8')
                kdb_type = "KDB-X"
            except (QError, AttributeError):
                kdb_version = str(conn('.z.K').py())
                kdb_type = "KDB+"

            self.logger.info(f"KDB-X connectivity check with 'tls={self.db_config.tls}': SUCCESS - {self.db_config.host}:{self.db_config.port} is accessible. You are running {kdb_type} version: {kdb_version}")

            # check if sql interface is loaded on KDB-X service
            if not conn('@[{2< count .s};(::);{0b}]').py():
                self.logger.error("KDB-X SQL interface check: FAILED - KDB-X service does not have the SQL interface loaded. Load it by running .s.init[] in your KDB-X Session")
                sys.exit(1)
            else:
                self.logger.info("KDB-X SQL interface check: SUCCESS - SQL interface is loaded")

            # check if AI libs are loaded on KDB-X service
            ai_libs_available = conn('@[{2< count .ai};(::);{0b}]').py()
            if not ai_libs_available:

                # check if KDB-X version supports loading AI libs as a module
                if kdb_type == "KDB-X" and version.parse(kdb_version) < version.parse("0.1.2"):
                    self.logger.warning("KDB-X AI Libs check: NOT AVAILABLE - AI-powered tools (similarity_search, hybrid_search) will be disabled.")
                    self.logger.warning(f"To use AI tools, you need at least KDB-X version '0.1.2'. Your version is '{kdb_version}'. Please update to the latest KDB-X version.")
                elif kdb_type == "KDB+":
                    self.logger.warning("KDB-X AI Libs check: NOT AVAILABLE - AI-powered tools (similarity_search, hybrid_search) are only available in KDB-X.")
                else:
                    self.logger.warning("KDB-X AI Libs check: NOT LOADED - AI-powered tools (similarity_search, hybrid_search) will be disabled.")
                    self.logger.warning(r"To enable AI tools, load the KDB-X AI libraries by running: .ai:use`kx.ai in your KDB-X Session and then restart the MCP server")
            else:
                self.logger.info("KDB-X AI Libs check: SUCCESS - AI Libs are loaded, AI tools will be available")

            set_ai_libs_available(ai_libs_available)
            self.ai_libs_available = ai_libs_available

            # aimeta enriches discovery but is not a backend prerequisite. Probe over the existing
            # qIPC connection and retain the result in this mounted instance's cache.
            try:
                detected = detect_metadata(
                    conn=conn, config=self.db_config, cache=self.metadata_cache, force=True
                )
                message = metadata_guidance(detected)
                if detected.tier == 3:
                    self.logger.info(message)
                else:
                    self.logger.warning(message)
            except Exception as e:
                self.logger.warning(f"KDB-X aimeta check: SKIPPED - probe failed ({e})")

            # Identity assertion: when enabled, the target must carry the `kx.auth` module and the
            # channel should be TLS-wrapped (the service-account credential + asserted principal
            # otherwise cross qIPC in cleartext). Fail clean at startup rather than mid-query.
            if getattr(self.db_config, "assert_identity", False):
                if not self.db_config.tls:
                    self.logger.warning(
                        "KDB-X identity assertion is ENABLED without TLS (KDBX_DB_TLS=false). The "
                        "service-account credential and the asserted principal cross the qIPC "
                        "channel in cleartext. Permitted for local/dev use, but enable TLS for "
                        "anything beyond that."
                    )
                bind_available = conn('@[{.kx.auth.bind;1b};(::);{0b}]').py()
                if not bind_available:
                    self.logger.error(
                        "KDB-X identity assertion check: FAILED - `.kx.auth.bind` is not defined on "
                        "the target. The KDB-X host must load the kx.auth module and bind it to the "
                        "global namespace: `.kx.auth:use`kx.auth` (install it with `just "
                        "install-modules`). Otherwise unset KDBX_DB_ASSERT_IDENTITY."
                    )
                    conn.close()
                    sys.exit(1)
                self.logger.info("KDB-X identity assertion check: SUCCESS - `.kx.auth.bind` is available")

                # The data gate's explicit consult needs the kx.auth build that ships `entitled`
                # (the one-round-trip scope-down verb). Config guarantees data_gate implies
                # assert_identity, so this check nests here. Fail clean at startup.
                if getattr(self.db_config, "data_gate", False):
                    entitled_available = conn('@[{.kx.auth.entitled;1b};(::);{0b}]').py()
                    if not entitled_available:
                        self.logger.error(
                            "KDB-X data-entitlement gate check: FAILED - `.kx.auth.entitled` is not "
                            "defined on the target. KDBX_DB_DATA_GATE=true requires the kx.auth "
                            "module version that ships `entitled` (re-run `just install-modules` "
                            "and reload the host's `.kx.auth:use`kx.auth`). Otherwise unset "
                            "KDBX_DB_DATA_GATE."
                        )
                        conn.close()
                        sys.exit(1)
                    self.logger.info(
                        "KDB-X data-entitlement gate check: SUCCESS - `.kx.auth.entitled` is available"
                    )

            conn.close()

        except QError as e:
            self.logger.error(f"KDB-X self.connectivity check with 'tls={self.db_config.tls}': FAILED - {self.db_config.host}:{self.db_config.port} ({e})")

            if "Connection refused" in str(e):
                self.logger.error(f"Verify KDB-X service is running and accessible on {self.db_config.host}:{self.db_config.port}")

            if "invalid username/password" in str(e):
                self.logger.error("Verify your KDBX_DB_USERNAME and KDBX_DB_PASSWORD are correct")

            self.logger.error("KDB-X MCP server cannot function without connection to a KDB-X database. Exiting...")
            sys.exit(1)

    def _register_addins(self):
        try:
            registered_addins = register_addins(self.mcp)
            self.logger.info(f"Successfully registered {len(registered_addins)} add-ins")

            for addin_name in registered_addins:
                self.logger.debug(f"Registered add-in: {addin_name}")

        except Exception as e:
            self.logger.error(f"Failed to register add-ins: {e}")
            raise

    def _gate_ai_tools(self):
        """Apply the AI-libs feature gate to this instance, per-instance and instance-safe.

        The AI tools (`similarity_search`, `hybrid_search`) are registered unconditionally during
        discovery and tagged ``requires-ai-libs``. When the eager pre-flight found AI libs are NOT
        available on this backend, hide them with FastMCP's native visibility transform
        ``disable(tags=...)`` — which removes them from ``list_tools()``, not merely blocks them at
        call time, preserving the prior behavior of never advertising a tool we can't fulfil. The
        gate reads ``self.ai_libs_available`` (this instance's pre-flight result), so two mounted
        backends with different AI-libs status each get the correct surface.
        """
        if not self.ai_libs_available:
            self.mcp.disable(tags={"requires-ai-libs"})
            self.logger.info(
                "AI Libs not available - AI tools (similarity_search, hybrid_search) are disabled "
                "and hidden from tool listing for this backend"
            )

    def _preload_embedding_models(self):
        """Preload embedding models to avoid first-request delays."""
        try:
            import asyncio
            from kx_mcp_kdbx.utils.embeddings import preload_models_from_config

            # Run the async preload function
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(preload_models_from_config(self.db_config.embedding_csv_path))
                self.logger.info("Embedding models preloaded successfully")
            finally:
                loop.close()

        except Exception as e:
            self.logger.warning(f"Failed to preload embedding models: {e}")
            self.logger.warning("Models will be loaded on first use, which may cause delays")

app_settings = AppSettings()


def build_server(config: AppSettings | None = None) -> FastMCP:
    """Build and return this bundle's configured FastMCP server — the extension seam.

    This is what the container composes (``parent.mount(build_server(), namespace="kdbx")``).
    The kdb-x backend is **mount-only**: it has no standalone serving entry point — the container
    (`kx-mcp-core`) owns transport/host/port. Run it via the container: ``uv run kx-mcp --bundles
    kdbx``.

    All startup checks (connectivity, ``.s`` SQL-interface, ``.ai`` AI-libs feature probe) and
    primitive registration run **eagerly** here, returning a ready-to-mount server — deliberately,
    because under a 3.x *direct* mount a child subserver's lifespan does not run.
    """
    if config is None:
        config = app_settings
    return McpServer(config).mcp
