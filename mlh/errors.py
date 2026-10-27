"""Map raw chDB/ClickHouse exception text to a fixed category + safe message.

Raw text can contain SQL (and therefore escaped secrets, see Day-0 evidence),
so it is never returned or logged. Only the category, a fixed Korean message
and the numeric ClickHouse error codes leave this module.

Patterns come from evidence/day0/error_detail.log (chDB 26.9 -> CH 26.8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CODE_RE = re.compile(r"Code: (\d+)\.")


@dataclass(frozen=True)
class ErrorInfo:
    category: str
    message: str
    codes: tuple[int, ...]

    def to_dict(self) -> dict:
        return {"category": self.category, "message": self.message, "codes": list(self.codes)}


# Order matters: the first matching rule wins (most specific first).
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("dns", re.compile(r"DNS_ERROR|Not found address of host"), "호스트 이름을 해석할 수 없습니다 (DNS)."),
    ("tls_not_tls_endpoint", re.compile(r"SSL Exception.*wrong version number"),
     "TLS로 접속했지만 서버가 TLS 응답을 하지 않았습니다. 평문 native 포트이거나 TLS 설정이 다릅니다."),
    ("tls_verify", re.compile(r"certificate verify failed|CERTIFICATE_VERIFY_FAILED|unable to get local issuer", re.I),
     "TLS 인증서를 검증하지 못했습니다 (CA 신뢰 설정 확인)."),
    ("tls_handshake", re.compile(r"SSL Exception|SSL routines", re.I), "TLS handshake에 실패했습니다."),
    ("http_endpoint", re.compile(r"Unexpected packet from server .*got 72\)"),
     "native protocol이 아닌 HTTP 응답을 받았습니다. HTTP 포트가 아니라 native(TCP) 포트가 필요합니다."),
    ("tls_endpoint_plain_client", re.compile(r"Unexpected packet from server .*got 2[12]\)"),
     "서버가 TLS로 응답했습니다. TLS 사용을 켜고 다시 시도하세요."),
    ("protocol", re.compile(r"UNEXPECTED_PACKET_FROM_SERVER|Unexpected packet from server"),
     "ClickHouse native protocol 응답이 아닙니다. endpoint와 포트를 확인하세요."),
    ("auth", re.compile(r"AUTHENTICATION_FAILED|Code: 516\."), "인증에 실패했습니다 (사용자 이름 또는 비밀번호)."),
    ("access_denied", re.compile(r"ACCESS_DENIED|Code: 497\.|Not enough privileges"),
     "system table 조회 권한이 없습니다 (ACCESS_DENIED)."),
    ("table_missing", re.compile(r"There is no table|UNKNOWN_TABLE|Code: 60\."),
     "필요한 system table이 없습니다 (비활성화되었거나 첫 flush 전)."),
    ("column_missing", re.compile(r"UNKNOWN_IDENTIFIER|Code: 47\."),
     "서버 버전에 필요한 컬럼이 없습니다 (버전 호환성)."),
    ("nc_missing", re.compile(r"NAMED_COLLECTION_DOESNT_EXIST|Code: 669\."),
     "저장된 자격증명(named collection)이 없습니다. 재입력이 필요합니다."),
    ("refused", re.compile(r"Connection refused"), "연결이 거부되었습니다 (포트가 닫혀 있거나 서버 중지)."),
    ("timeout", re.compile(r"SOCKET_TIMEOUT|Timeout: connect timed out|TIMEOUT_EXCEEDED|Timeout exceeded", re.I),
     "제한 시간 안에 응답이 없습니다. (원인 미확정: 방화벽·서버 지연 외에 HTTP 등 다른 protocol 포트도 응답 없이 timeout 될 수 있음)"),
    ("network", re.compile(r"NETWORK_ERROR|Connection reset|Broken pipe|Net Exception", re.I), "네트워크 오류가 발생했습니다."),
)


# Wrapper codes that only say "all attempts failed"; the cause is the innermost other code.
_WRAPPERS = frozenset({519, 279})
# Codes whose meaning does not depend on message text (user data in the text is ignored).
_BY_CODE = {
    516: "auth", 497: "access_denied", 60: "table_missing", 47: "column_missing", 669: "nc_missing",
    198: "dns", 209: "timeout", 159: "timeout",
}
# Network codes whose cause is only in the server/OS-generated text (refused vs SSL, packet byte).
_TEXT_CODES = frozenset({210, 102, 1000})
_SEGMENT_RE = re.compile(r"Code: (\d+)\.(.*?)(?=Code: \d+\.|$)", re.S)
# Server-generated phrases without a code (e.g. remote() structure lookup).
_NO_CODE_RULES = (("table_missing", re.compile(r"There is no table `[^`]*`\.`[^`]*` on server")),)
_MESSAGES = {category: message for category, _, message in _RULES}


def _info(category: str, codes: tuple[int, ...]) -> ErrorInfo:
    return ErrorInfo(category, _MESSAGES[category], codes)


def classify(raw: str) -> ErrorInfo:
    """Classify by the innermost real ClickHouse error code first.

    Message patterns are only applied to that code's own segment (for codes such
    as 210/102 whose meaning is in the server-generated text). Text elsewhere -
    usernames, SQL, hostnames - can never select a category.
    """
    segments = [(int(c), body) for c, body in _SEGMENT_RE.findall(raw)]
    codes = tuple(dict.fromkeys(c for c, _ in segments))
    inner = [(c, body) for c, body in segments if c not in _WRAPPERS]
    if inner:
        code, body = inner[-1]
        if code in _BY_CODE:
            return _info(_BY_CODE[code], codes)
        if code not in _TEXT_CODES:
            return ErrorInfo("unclassified", "원인을 분류할 수 없는 오류입니다 (미분류).", codes)
        for category, pattern, _ in _RULES:
            if pattern.search(f"Code: {code}.{body}"):
                return _info(category, codes)
        return ErrorInfo("unclassified", "원인을 분류할 수 없는 오류입니다 (미분류).", codes)
    for category, pattern in _NO_CODE_RULES:
        if pattern.search(raw):
            return _info(category, codes)
    return ErrorInfo("unclassified", "원인을 분류할 수 없는 오류입니다 (미분류).", codes)


def classify_exception(exc: BaseException) -> ErrorInfo:
    return classify(str(exc))


# Categories meaning "the endpoint answered as ClickHouse and authenticated".
CONNECTED_CATEGORIES = frozenset({"access_denied", "table_missing", "column_missing"})
# Categories that mean the endpoint could not be reached / authenticated (connection check failed).
CONNECTION_FAILURES = frozenset({
    "dns", "refused", "timeout", "network", "tls_not_tls_endpoint", "tls_verify", "tls_handshake",
    "http_endpoint", "tls_endpoint_plain_client", "protocol", "auth",
})
