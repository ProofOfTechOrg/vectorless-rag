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


def test_pageindex_runtime_does_not_fetch_litellm_cost_metadata() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import vectorless_rag.pageindex_runtime"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Failed to fetch remote model cost map" not in result.stderr
