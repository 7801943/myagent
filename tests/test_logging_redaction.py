import logging

from myagent.utils.logging import SensitiveDataFilter, redact_sensitive_text


def test_redact_sensitive_query_parameters_without_hiding_other_parameters():
    secret = "header.payload.signature"
    value = (
        f'GET /api/documents/plugin-config?token={secret}&lang=zh-CN '
        f'callback=/save?access_token={secret}'
    )

    redacted = redact_sensitive_text(value)

    assert secret not in redacted
    assert "token=<redacted>&lang=zh-CN" in redacted
    assert "access_token=<redacted>" in redacted


def test_sensitive_filter_redacts_uvicorn_access_log_arguments():
    secret = "header.payload.signature"
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "192.0.2.1:1234",
            "GET",
            f"/api/documents/plugin-config?token={secret}",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    assert SensitiveDataFilter().filter(record) is True
    rendered = record.getMessage()

    assert secret not in rendered
    assert "token=<redacted>" in rendered


def test_sensitive_filter_redacts_json_fragments():
    secret = "header.payload.signature"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='payload={"token": "%s"}',
        args=(secret,),
        exc_info=None,
    )

    SensitiveDataFilter().filter(record)

    assert secret not in record.getMessage()
    assert secret not in redact_sensitive_text(f'payload={{"token": "{secret}"}}')
