from scdl.scdl import apply_all_command_defaults


def test_apply_all_command_defaults_sets_expected_flags() -> None:
    arguments = {
        "all": True,
        "-f": False,
        "--best-quality": False,
        "-c": False,
        "--retries": None,
        "--download-archive": None,
    }

    apply_all_command_defaults(arguments)

    assert arguments["-f"] is True
    assert arguments["--best-quality"] is True
    assert arguments["-c"] is True
    assert arguments["--retries"] == "3"
    assert arguments["--download-archive"] == "archive.txt"


def test_apply_all_command_defaults_keeps_explicit_values() -> None:
    arguments = {
        "all": True,
        "-f": False,
        "--best-quality": False,
        "-c": False,
        "--retries": "7",
        "--download-archive": "custom-archive.txt",
    }

    apply_all_command_defaults(arguments)

    assert arguments["--retries"] == "7"
    assert arguments["--download-archive"] == "custom-archive.txt"


def test_apply_all_command_defaults_noop_when_all_disabled() -> None:
    arguments = {
        "all": False,
        "-f": False,
        "--best-quality": False,
        "-c": False,
        "--retries": None,
        "--download-archive": None,
    }

    apply_all_command_defaults(arguments)

    assert arguments["-f"] is False
    assert arguments["--best-quality"] is False
    assert arguments["-c"] is False
    assert arguments["--retries"] is None
    assert arguments["--download-archive"] is None
