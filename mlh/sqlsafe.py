"""Input validation and the only places where values become SQL text.

Policy
- Identifiers (database/table/NC names) are never user input: they are built
  from server-generated hex ids and re-checked against IDENT_RE.
- String literals go through query parameters `{name:String}` when the
  statement supports them (SELECT, CREATE MATERIALIZED VIEW ... AS SELECT).
- CREATE NAMED COLLECTION rejects parameters, so it uses `literal()`, which
  rejects control characters and escapes backslash and single quote.
- host is parsed as an IP address or DNS name. ClickHouse address-pattern
  syntax ({a,b}, |, comma, spaces) is rejected so one form field can only ever
  mean one endpoint.
"""

from __future__ import annotations

import ipaddress
import re
import secrets

from pydantic import BaseModel, Field, field_validator

IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_DNS_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(6)}"


def identifier(name: str) -> str:
    if not IDENT_RE.fullmatch(name):
        raise ValueError("invalid identifier")
    return name


def literal(value: str) -> str:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("control characters are not allowed")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def parse_host(value: str) -> str:
    host = value.strip()
    if not host or len(host) > 253:
        raise ValueError("host must be 1..253 characters")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    if not all(_DNS_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("host must be a single IP address or DNS name (no ports, patterns or lists)")
    return host.lower()


def format_address(host: str, port: int) -> str:
    """host:port as understood by remote()/remoteSecure() (IPv6 in brackets)."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _no_control(value: str) -> str:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("control characters are not allowed")
    return value


class EndpointInput(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    tls: bool
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return parse_host(value)

    @field_validator("username", "password")
    @classmethod
    def _text(cls, value: str) -> str:
        return _no_control(value)


class NodeAddress(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return parse_host(value)


class ProbeRequest(EndpointInput):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _no_control(value.strip())


class RegisterRequest(ProbeRequest):
    nodes: list[NodeAddress] = Field(min_length=1, max_length=32)
