import subprocess
import sys


def test_import_yuxi_does_not_eagerly_import_knowledge():
    script = """
import sys
import yuxi

assert yuxi.get_version() == yuxi.__version__
assert "yuxi.knowledge" not in sys.modules

from yuxi import config

assert config is yuxi.config
assert "yuxi.knowledge" in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
