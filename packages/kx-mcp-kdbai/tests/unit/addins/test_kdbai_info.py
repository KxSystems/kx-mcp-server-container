"""Impl tests for the info tools (mocked KDB.AI client)."""

import asyncio

import pytest

from kx_mcp_kdbai.addins import kdbai_info as mod


def test_session_info(mocker):
    client = mocker.Mock()
    client.session_info.return_value = {"version": "1.7.0"}
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)
    assert asyncio.run(mod.kdbai_session_info_impl()) == {"version": "1.7.0"}


def test_system_info(mocker):
    client = mocker.Mock()
    client.system_info.return_value = {"cpu": 8}
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)
    assert asyncio.run(mod.kdbai_system_info_impl()) == {"cpu": 8}


def test_process_info(mocker):
    client = mocker.Mock()
    client.process_info.return_value = {"pid": 1}
    mocker.patch.object(mod, "get_kdbai_client", return_value=client)
    assert asyncio.run(mod.kdbai_process_info_impl()) == {"pid": 1}


def test_info_raises_on_client_error(mocker):
    mocker.patch.object(mod, "get_kdbai_client", side_effect=RuntimeError("down"))
    with pytest.raises(RuntimeError):
        asyncio.run(mod.kdbai_session_info_impl())
