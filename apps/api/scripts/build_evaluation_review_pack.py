#!/usr/bin/env python3
"""Render a local review pack from tracked source excerpts."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "evaluation"
    rows = json.loads((root / "pageindex-v2-review-source.json").read_text(encoding="utf-8"))
    output = root / "review-pack.md"
    lines = [
        "# PageIndex v2 evaluation review pack",
        "",
        "Mark every case approved, edited, or rejected in the JSONL review record before seeding.",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## {row['item_id']}",
                "",
                f"- Split: `{row['split']}`",
                f"- Stratum: `{row['stratum']}`",
                f"- Prompt: {row['prompt']}",
                f"- Reference: {row['reference_answer']}",
                "",
            ]
        )
        for excerpt in row["excerpts"]:
            lines.extend(
                [
                    f"Source `{excerpt['arxiv_id']}`, page {excerpt['page']}:",
                    "",
                    f"> {excerpt['excerpt']}",
                    "",
                ]
            )
    output.write_text("\n".join(lines), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
