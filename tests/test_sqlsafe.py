import pytest
from pydantic import ValidationError

from mlh.sqlsafe import RegisterRequest, format_address, identifier, literal, parse_host


def test_literal_escapes_backslash_and_quote():
    assert literal("pa'ss\\wo{rd}\"ü한글") == "'pa\\'ss\\\\wo{rd}\"ü한글'"


@pytest.mark.parametrize("bad", ["a\x00b", "line\nbreak", "tab\there", "del\x7f"])
def test_literal_rejects_control_characters(bad):
    with pytest.raises(ValueError):
        literal(bad)


@pytest.mark.parametrize("host,expected", [("127.0.0.1", "127.0.0.1"), ("DB-1.Example.com", "db-1.example.com"), ("::1", "::1")])
def test_parse_host_accepts_single_host(host, expected):
    assert parse_host(host) == expected


@pytest.mark.parametrize("bad", ["a{1,2}.local", "host:9000", "a,b", "a|b", "with space", "-bad.example", "", "x" * 254])
def test_parse_host_rejects_patterns_lists_ports(bad):
    with pytest.raises(ValueError):
        parse_host(bad)


def test_format_address_brackets_ipv6():
    assert format_address("::1", 9440) == "[::1]:9440"
    assert format_address("db", 9000) == "db:9000"


def test_identifier_only_generated_shapes():
    assert identifier("mv_q_n0123456789ab") == "mv_q_n0123456789ab"
    for bad in ["Drop table", "a;b", "x`y", "1abc"]:
        with pytest.raises(ValueError):
            identifier(bad)


def test_register_request_repr_hides_password():
    req = RegisterRequest(name="n", host="127.0.0.1", port=9000, tls=False, username="u", password="TOPSECRET",
                          nodes=[{"host": "127.0.0.1", "port": 9000}])
    assert "TOPSECRET" not in repr(req)


def test_register_request_validates_ranges():
    with pytest.raises(ValidationError):
        RegisterRequest(name="n", host="127.0.0.1", port=70000, tls=False, username="u", password="p", nodes=[])
