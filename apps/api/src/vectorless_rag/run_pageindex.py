"""Privacy-preserving launcher for the pinned PageIndex CLI."""

from __future__ import annotations

import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import argparse
import json
import re
import runpy
import sys
from pathlib import Path
from typing import Any

_SAFE_VALUE = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SAFE_DIAGNOSTICS = {
    "pageindex_child_failed": "PageIndex failed inside its isolated runtime.",
    "pageindex_content_filter": (
        "The provider rejected PageIndex content under its content policy."
    ),
    "pageindex_empty_response": "The provider returned an empty PageIndex completion.",
    "pageindex_insufficient_system_resource": (
        "The provider reported insufficient system resources."
    ),
    "pageindex_invalid_json": "PageIndex received malformed JSON.",
    "pageindex_invalid_response": "PageIndex received an invalid provider response.",
    "pageindex_llm_retry_exhausted": (
        "The PageIndex provider request failed after all retry attempts."
    ),
    "pageindex_provider_rejected": "The PageIndex provider rejected the request.",
    "pageindex_transient_provider_error": ("The PageIndex provider request failed transiently."),
    "pageindex_unexpected_tool_call": "PageIndex received an unexpected tool call.",
}


def _safe_value(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_VALUE.fullmatch(value) else None


def build_failure_report(error: BaseException) -> dict[str, Any]:
    code = _safe_value(getattr(error, "failure_code", None)) or "pageindex_child_failed"
    if code not in _SAFE_DIAGNOSTICS:
        code = "pageindex_child_failed"
    diagnostic = _SAFE_DIAGNOSTICS[code]
    report: dict[str, Any] = {"failure_code": code, "diagnostic": diagnostic}
    finish_reason = _safe_value(getattr(error, "finish_reason", None))
    if finish_reason is not None:
        report["finish_reason"] = finish_reason
    attempts = getattr(error, "attempts", None)
    if isinstance(attempts, int) and not isinstance(attempts, bool) and 0 <= attempts <= 100:
        report["llm_attempts"] = attempts
    return report


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pageindex-script", type=Path, required=True)
    parser.add_argument("--failure-report", type=Path, required=True)
    known, remaining = parser.parse_known_args()
    sys.argv = [str(known.pageindex_script), *remaining]
    try:
        runpy.run_path(str(known.pageindex_script), run_name="__main__")
    except BaseException as error:
        known.failure_report.write_text(
            json.dumps(build_failure_report(error), separators=(",", ":")), encoding="utf-8"
        )
        raise


if __name__ == "__main__":
    main()
