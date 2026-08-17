import importlib

import pytest

from kx_auth_core.authz import AuthzDecision
from kx_mcp_core.auth import AuthzSettings
from kx_mcp_kdbx.addins.kdbx_metadata_tools import kdbx_refresh_metadata_impl


@pytest.mark.anyio
async def test_refresh_metadata_authorizes_dotted_q_resource(mocker):
    """The administrative capability uses the canonical q-safe dotted vocabulary."""
    authorize_mod = importlib.import_module("kx_mcp_core.auth.authorize")
    captured = {}

    def allow(request, strategy):
        captured["request"] = request
        captured["strategy"] = strategy
        return AuthzDecision(allowed=True, adapter="capture")

    mocker.patch.object(authorize_mod, "_settings", return_value=AuthzSettings(mode="capture"))
    mocker.patch.object(authorize_mod, "current_principal", return_value=None)
    mocker.patch.object(authorize_mod, "decide", side_effect=allow)

    # The implementation may return its clean error envelope after authorization; this regression
    # is deliberately about the request sent to the adapter, not metadata connectivity.
    await kdbx_refresh_metadata_impl()

    request = captured["request"]
    assert captured["strategy"] == "capture"
    assert request.action == "admin"
    assert request.resource == "kdbx.metadata"
    assert request.namespace == "kdbx"
