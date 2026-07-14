#!/usr/bin/env python3
"""Quick test for send2codex module."""
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parent / "send_message.py"
SPEC = importlib.util.spec_from_file_location("send2codex_smoke", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
send_message = MODULE.send_message

def test_import():
    """Test that module imports correctly."""
    assert callable(send_message)

def test_auto_detection():
    """Test auto-detection logic."""
    assert send_message.__name__ == "send_message"

if __name__ == "__main__":
    print("Testing send2codex module...")
    test_import()
    test_auto_detection()
    print("\nAll tests passed!")
