from scdl_web import APP_VERSION


def test_web_service_version_is_semantic() -> None:
    assert APP_VERSION == "0.1.1"
    assert len(APP_VERSION.split(".")) == 3
