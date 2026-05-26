from tools import iac_static_gate  # noqa: F401


def test_module_imports():
    assert iac_static_gate is not None
