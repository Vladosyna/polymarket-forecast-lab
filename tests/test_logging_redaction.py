"""httpx logs full request URLs at INFO -- FRED's api_key query param must
never reach data/logs/lab.jsonl in plain text (util.RedactSecretsFilter).
"""

from __future__ import annotations

import logging

from lab.util import RedactSecretsFilter


def _filtered_message(url: str) -> str:
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg='HTTP Request: GET %s "HTTP/1.1 200 OK"', args=(url,), exc_info=None,
    )
    RedactSecretsFilter().filter(record)
    return record.getMessage()


def test_redacts_fred_api_key_query_param():
    url = "https://api.stlouisfed.org/fred/series/observations?series_id=GDPNOW&api_key=abc123secret&file_type=json"
    out = _filtered_message(url)
    assert "abc123secret" not in out
    assert "api_key=***REDACTED***" in out
    assert "series_id=GDPNOW" in out  # non-secret params untouched


def test_does_not_touch_unrelated_token_params():
    """token_id (Polymarket CLOB) must survive -- it's not a secret."""
    url = "https://clob.polymarket.com/book?token_id=7132104567925221"
    out = _filtered_message(url)
    assert "token_id=7132104567925221" in out


def test_passthrough_when_no_secret_present():
    url = "https://clob.polymarket.com/midpoint?token_id=123"
    assert _filtered_message(url) == f'HTTP Request: GET {url} "HTTP/1.1 200 OK"'
