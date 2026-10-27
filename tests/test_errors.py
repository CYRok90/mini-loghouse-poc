"""Raw messages below were captured in evidence/day0/error_detail.log (chDB 26.9 -> CH 26.8)."""
import pytest

from mlh.errors import classify

WRAP = "Code: 519. DB::NetException: All attempts to get table structure failed. Log: \n\nCode: 279. DB::NetException: All connection tries failed. Log: \n\n{inner}\n\n. (ALL_CONNECTION_TRIES_FAILED) (version 26.9.2.1)\n\n. (NO_REMOTE_SHARD_AVAILABLE)"

CASES = [
    ("Code: 210. DB::NetException: SSL Exception: error:0A00010B:SSL routines::wrong version number, while writing to socket", "tls_not_tls_endpoint"),
    ("Code: 102. DB::NetException: Unexpected packet from server 127.0.0.1:19401 (expected Hello or Exception, got 21)", "tls_endpoint_plain_client"),
    ("Code: 102. DB::NetException: Unexpected packet from server 127.0.0.1:18003 (expected Hello or Exception, got 72)", "http_endpoint"),
    ("There is no table `system`.`no_such_table` on server: 127.0.0.1:19003", "table_missing"),
    ("Code: 210. DB::NetException: Connection refused (127.0.0.1:1). (NETWORK_ERROR)", "refused"),
    ("Code: 209. DB::NetException: Timeout: connect timed out: 10.255.255.1:9000. (SOCKET_TIMEOUT)", "timeout"),
    ("Code: 198. DB::NetException: Not found address of host: no-such-host.invalid. (DNS_ERROR)", "dns"),
]


@pytest.mark.parametrize("inner,category", CASES)
def test_wrapped_remote_errors_use_inner_cause(inner, category):
    assert classify(WRAP.format(inner=inner)).category == category


def test_auth_and_access_denied():
    assert classify("Code: 516. DB::Exception: Received from 127.0.0.1:19003. DB::Exception: mlh_collector: Authentication failed. (AUTHENTICATION_FAILED)").category == "auth"
    assert classify("Code: 497. DB::Exception: Received from x. DB::Exception: mlh_noperm: Not enough privileges. (ACCESS_DENIED)").category == "access_denied"


def test_unknown_is_unclassified_and_message_is_fixed():
    raw = "Code: 999. DB::Exception: something with password='hunter2'"
    info = classify(raw)
    assert info.category == "unclassified"
    assert "hunter2" not in info.message and info.codes == (999,)


def test_user_strings_cannot_select_category():
    # Codex review counterexample: a username that looks like an error name.
    raw = ("Code: 516. DB::Exception: Received from 127.0.0.1:19003. DB::Exception: DNS_ERROR: Authentication failed: "
           "password is incorrect, or there is no user with such name. (AUTHENTICATION_FAILED)")
    assert classify(raw).category == "auth"


def test_sql_text_in_unrelated_code_is_unclassified():
    raw = "Code: 62. DB::Exception: Syntax error near 'Connection refused SSL Exception ACCESS_DENIED'. (SYNTAX_ERROR)"
    assert classify(raw).category == "unclassified"


def test_innermost_code_wins_over_wrappers():
    raw = WRAP.format(inner="Code: 209. DB::NetException: Timeout: connect timed out: h:9000. (SOCKET_TIMEOUT)")
    assert classify(raw).codes == (519, 279, 209) and classify(raw).category == "timeout"
