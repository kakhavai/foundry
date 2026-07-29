def test_package_exposes_version():
    import collector_core

    assert collector_core.__version__ == "0.1.0"
