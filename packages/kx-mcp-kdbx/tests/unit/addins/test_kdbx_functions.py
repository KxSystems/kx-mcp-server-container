import json

import pytest

from kx_mcp_kdbx.addins.kdbx_functions import kdbx_functions_impl


@pytest.mark.anyio
async def test_single_function_lookup_is_token_efficient(mocker):
    build = mocker.patch(
        "kx_mcp_kdbx.addins.kdbx_functions.build_functions_document",
        return_value={"status": "success", "functions": [{"name": ".analytics.vwap"}]},
    )

    result = await kdbx_functions_impl(
        ".analytics.vwap", config="config", cache="cache"
    )

    assert json.loads(result[0].text)["functions"] == [{"name": ".analytics.vwap"}]
    build.assert_called_once_with(
        config="config", cache="cache", function=".analytics.vwap"
    )
