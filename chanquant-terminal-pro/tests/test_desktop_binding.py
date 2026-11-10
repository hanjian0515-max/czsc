"""Verify the Windows binding branch without relying on Linux socket semantics."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import desktop_alerts as app


@pytest.mark.parametrize('platform,exclusive', [('win32',True),('linux',False)])
def test_listener_binding_uses_windows_exclusive_socket(monkeypatch,platform,exclusive):
    server=object.__new__(app.AlertHTTPServer)
    server.socket=Mock()
    inherited=Mock()
    monkeypatch.setattr(app,'sys',SimpleNamespace(platform=platform))
    monkeypatch.setattr(app.socket,'SO_EXCLUSIVEADDRUSE',-5,raising=False)
    monkeypatch.setattr(app.ThreadingHTTPServer,'server_bind',inherited)
    server.server_bind()
    inherited.assert_called_once_with()
    if exclusive:
        assert server.allow_reuse_address is False
        server.socket.setsockopt.assert_called_once_with(app.socket.SOL_SOCKET,-5,1)
    else:
        assert server.allow_reuse_address is True
        server.socket.setsockopt.assert_not_called()
