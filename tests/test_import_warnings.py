from __future__ import annotations

import subprocess
import sys


def test_langgraph_import_does_not_warn_about_allowed_objects() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "vectorless_rag.cli", "bootstrap-key", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "default value of `allowed_objects`" not in result.stderr
