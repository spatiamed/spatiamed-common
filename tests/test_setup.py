"""Verify basic project setup."""


def test_version():
    from sm_common import __version__

    assert __version__ == "0.2.0"
