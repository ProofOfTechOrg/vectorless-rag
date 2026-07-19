"""Privacy-preserving launcher for the pinned PageIndex CLI."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

import litellm


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pageindex-script", type=Path, required=True)
    known, remaining = parser.parse_known_args()
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        os.environ.setdefault("LANGFUSE_OTEL_HOST", os.getenv("LANGFUSE_HOST", ""))
        litellm.callbacks = ["langfuse_otel"]
        # PageIndex prompts contain whole PDF pages. Keep timing/tokens/model, not passage content.
        litellm.turn_off_message_logging = True
        litellm.log_raw_request_response = False
    sys.argv = [str(known.pageindex_script), *remaining]
    runpy.run_path(str(known.pageindex_script), run_name="__main__")


if __name__ == "__main__":
    main()
