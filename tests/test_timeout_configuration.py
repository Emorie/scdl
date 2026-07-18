from scdl.scdl import request_timeout


def test_direct_request_timeouts_are_operation_specific(monkeypatch):
    monkeypatch.setenv("SCDL_CONNECT_TIMEOUT_SECONDS", "11")
    monkeypatch.setenv("SCDL_METADATA_TIMEOUT_SECONDS", "22")
    monkeypatch.setenv("SCDL_MEDIA_RESOLVE_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("SCDL_READ_TIMEOUT_SECONDS", "44")
    assert request_timeout() == (11, 22)
    assert request_timeout("media_resolution") == (11, 33)
    assert request_timeout("audio") == (11, 44)
