from types import SimpleNamespace

import pytest

from myagent.interfaces.web.private_proxy_context import resolve_private_proxy_context


def _request(peer: str, host: str, **headers):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={"host": host, **headers},
    )


@pytest.mark.parametrize(
    ("peer", "host", "expected"),
    [
        ("127.0.0.2", "127.0.0.1:43171", "http://127.0.0.1:43171"),
        ("::ffff:127.0.0.2", "localhost:43171", "http://localhost:43171"),
        ("127.0.0.2", "[::1]:43171", "http://[::1]:43171"),
    ],
)
def test_trusted_tunnel_resolves_loopback_browser_origin(peer, host, expected):
    context = resolve_private_proxy_context(_request(peer, host), {"127.0.0.2"})

    assert context.trusted_tunnel is True
    assert context.peer_ip == "127.0.0.2"
    assert context.browser_origin == expected


def test_untrusted_peer_cannot_supply_browser_origin_or_spoof_forwarded_headers():
    context = resolve_private_proxy_context(
        _request(
            "192.168.0.84",
            "127.0.0.1:43171",
            **{
                "x-forwarded-host": "127.0.0.1:43171",
                "x-myagent-private-onlyoffice-origin": "http://127.0.0.1:43171",
            },
        ),
        {"127.0.0.2"},
    )

    assert context.trusted_tunnel is False
    assert context.browser_origin is None


def test_forwarded_headers_do_not_override_the_actual_host():
    context = resolve_private_proxy_context(
        _request(
            "127.0.0.2",
            "127.0.0.1:43171",
            **{"x-forwarded-host": "attacker.example.com"},
        ),
        {"127.0.0.2"},
    )

    assert context.browser_origin == "http://127.0.0.1:43171"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "192.168.0.84:43171",
        "127.0.0.2:43171",
        "127.0.0.1:0",
        "127.0.0.1:",
        "127.0.0.1:0x20",
        "127.0.0.1:70000",
        "user@127.0.0.1:43171",
        "127.0.0.1:43171/path",
        "127.0.0.1:43171\r\nX-Evil: yes",
    ],
)
def test_trusted_tunnel_rejects_invalid_or_non_loopback_host(host):
    context = resolve_private_proxy_context(_request("127.0.0.2", host), {"127.0.0.2"})

    assert context.trusted_tunnel is True
    assert context.browser_origin is None
