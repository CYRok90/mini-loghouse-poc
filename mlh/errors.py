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
     "필수 system table 조회 권한이 없습니다."),
    ("table_missing", re.compile(r"There is no table|UNKNOWN_TABLE|Code: 60\."),
     "필요한 system table이 없습니다 (비활성화되었거나 첫 flush 전)."),
    ("column_missing", re.compile(r"UNKNOWN_IDENTIFIER|Code: 47\."),
     "서버 버전에 필요한 컬럼이 없습니다 (버전 호환성)."),
    ("nc_missing", re.compile(r"NAMED_COLLECTION_DOESNT_EXIST|Code: 669\."),
     "저장된 자격증명(named collection)이 없습니다. 재입력이 필요합니다."),
    ("refused", re.compile(r"Connection refused"), "연결이 거부되었습니다 (포트가 닫혀 있거나 서버 중지)."),
    ("timeout", re.compile(r"SOCKET_TIMEOUT|Timeout: connect timed out|TIMEOUT_EXCEEDED|Timeout exceeded", re.I),
     "제한 시간 안에 응답이 없습니다."),
    ("network", re.compile(r"NETWORK_ERROR|Connection reset|Broken pipe|Net Exception", re.I), "네트워크 오류가 발생했습니다."),
)


def classify(raw: str) -> ErrorInfo:
    codes = tuple(dict.fromkeys(int(c) for c in _CODE_RE.findall(raw)))
    for category, pattern, message in _RULES:
        if pattern.search(raw):
            return ErrorInfo(category, message, codes)
    return ErrorInfo("unclassified", "원인을 분류할 수 없는 오류입니다 (미분류).", codes)


def classify_exception(exc: BaseException) -> ErrorInfo:
    return classify(str(exc))


# Categories meaning "the endpoint answered as ClickHouse and authenticated".
CONNECTED_CATEGORIES = frozenset({"access_denied", "table_missing", "column_missing"})
