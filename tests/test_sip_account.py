import ipaddress

import pytest

from sipgram.config import ConfigError, SipConfig
from sipgram.sip.account import CallError, SipAccount
from sipgram.sip.message import SipMessage


def make(**over) -> SipAccount:
    params = dict(server="localhost", username="1", password="x", register=False, keepalive=0,
                  local_port=0, rtp_port_min=45000, rtp_port_max=45010)
    params.update(over)
    return SipAccount(SipConfig(**params), "127.0.0.1")


@pytest.mark.asyncio
async def test_server_name_is_resolved_to_ip():
    """Windows' proactor loop rejects a host name in sendto() (WSAEINVAL), so the
    destination must be numeric before any datagram is sent."""
    acc = make()
    assert acc.server_addr[0] == "localhost"
    await acc.start()
    try:
        ipaddress.ip_address(acc.server_addr[0])
        assert acc.server_addr[1] == 5060
    finally:
        await acc.transport.stop()


@pytest.mark.asyncio
async def test_tcp_transport_target_follows_resolution():
    acc = make(transport="tcp", server="localhost", port=15060)
    await acc.resolve_server()
    ipaddress.ip_address(acc.server_addr[0])
    assert acc.transport.remote == acc.server_addr


@pytest.mark.asyncio
async def test_unresolvable_server_reports_clearly():
    acc = make(server="pbx.invalid-tld-for-tests.example")
    with pytest.raises(CallError) as ei:
        await acc.start()
    assert ei.value.code == 503 and "cannot resolve" in ei.value.text


def _invite(source: str, call_id: str) -> bytes:
    sdp = (f"v=0\r\no=- 1 1 IN IP4 {source}\r\ns=-\r\nc=IN IP4 {source}\r\nt=0 0\r\n"
           "m=audio 4000 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n")
    return (f"INVITE sip:1@127.0.0.1 SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {source}:5060;branch=z9hG4bK{call_id}\r\n"
            f"From: <sip:100@{source}>;tag=t{call_id}\r\nTo: <sip:1@127.0.0.1>\r\n"
            f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\nContact: <sip:100@{source}:5060>\r\n"
            f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}").encode()


@pytest.mark.asyncio
async def test_calls_are_taken_only_from_the_pbx_and_allowed_networks():
    """Incoming requests are not authenticated, so a stranger who finds the port must not ring the users."""
    acc = make(server="127.0.0.1", allow_from=["10.20.0.0/16"])
    await acc.start()
    calls = []
    acc.on_incoming_call = calls.append
    try:
        acc._on_message(SipMessage.parse(_invite("203.0.113.5", "stranger")), ("203.0.113.5", 5060))
        assert not calls and "stranger" not in acc._calls
        acc._on_message(SipMessage.parse(_invite("127.0.0.1", "pbx")), ("127.0.0.1", 5060))
        acc._on_message(SipMessage.parse(_invite("10.20.3.4", "second-pbx")), ("10.20.3.4", 5060))
        assert [c.call_id for c in calls] == ["pbx", "second-pbx"]
    finally:
        await acc.stop()


def test_allow_from_must_be_addresses():
    with pytest.raises(ConfigError):
        SipConfig(server="pbx", username="1", password="x", allow_from=["pbx.example.com"])
