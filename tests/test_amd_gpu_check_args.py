import subprocess
from pathlib import Path
import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-docker-amd-gpu.sh"


def _has_functional_bash():
    try:
        proc = subprocess.run(["bash", "-c", "echo hello"], capture_output=True, text=True, check=False)
        return proc.returncode == 0 and "hello" in proc.stdout
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_functional_bash(), reason="Requires a functional bash environment")


def test_amd_gpu_check_rejects_unknown_extra_arg_before_diagnostics():
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--bad-option"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "Unknown option: --bad-option" in proc.stderr


def test_amd_gpu_check_shell_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
