import importlib.util
from pathlib import Path
import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/browser-virtual-session.py'
spec = importlib.util.spec_from_file_location('virtual_browser', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_ready_probe_returns_without_sleep():
    module.wait_for(lambda: True, [], seconds=1)


def test_dead_component_is_not_ready():
    class Dead:
        args = ['fake']
        returncode = 1
        def poll(self): return 1
    with pytest.raises(RuntimeError, match='component exited'):
        module.wait_for(lambda: True, [Dead()])


def test_readiness_is_bounded():
    with pytest.raises(RuntimeError, match='timed out'):
        module.wait_for(lambda: False, [], seconds=0)


def test_local_only_no_sandbox_bypass_or_password_argument():
    source = SCRIPT.read_text()
    assert "'-listen', '127.0.0.1'" in source
    assert "'-no6'" in source
    assert "'x11vnc', '-norc'" in source
    assert "'-nolisten', 'tcp'" in source
    assert "'-passwdfile'" in source
    assert '--no-sandbox' not in source
    assert "'--remote-debugging-address=127.0.0.1'" in source
