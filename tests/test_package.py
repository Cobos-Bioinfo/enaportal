import enaportal


def test_version_is_exposed():
    assert isinstance(enaportal.__version__, str)
    assert enaportal.__version__
