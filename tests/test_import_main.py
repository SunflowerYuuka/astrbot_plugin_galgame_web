import importlib
from pathlib import Path

PACKAGE_NAME = Path(__file__).resolve().parents[1].name


def test_main_module_imports_with_mocked_astrbot():
    module = importlib.import_module(f"{PACKAGE_NAME}.main")
    assert hasattr(module, "GalgamePlugin")
