#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

REQUIRED_FILES = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY_NOTICES.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/testing.md",
    "docs/troubleshooting.md",
}
CONTENT_TYPE = re.compile(
    r"^<!-- Content type: (Tutorial|How-to|Reference|Conceptual|Troubleshooting|Landing) -->$"
)
CONTENT_PLAN = re.compile(
    r"^<!-- Content plan: Goal: .+ Audience: .+ Content: .+ Open questions?: .+ -->$"
)
INLINE_LINK = re.compile(r"!?\[[^\]]*]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+]:\s*(\S+)", re.MULTILINE)
HTML_TAG = re.compile(r"<[^>]+>")
PUNCTUATION = re.compile(r"[^\w\s-]", re.UNICODE)


def public_markdown_files(root: Path) -> list[Path]:
    candidates = [
        *root.glob("*.md"),
        *root.glob("docs/**/*.md"),
        root / "apps/api/README.md",
        root / "apps/api/evaluation/review-pack.md",
    ]
    excluded = {"CLAUDE.md"}
    return sorted(
        {
            path
            for path in candidates
            if path.is_file() and path.name not in excluded and ".venv" not in path.parts
        }
    )


def github_anchor(heading: str) -> str:
    value = HTML_TAG.sub("", heading).strip().lower()
    value = PUNCTUATION.sub("", value)
    return re.sub(r"\s+", "-", value)


def heading_anchors(text: str) -> set[str]:
    counts: Counter[str] = Counter()
    anchors: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        base = github_anchor(match.group(1))
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def validate_markdown_file(root: Path, path: Path) -> list[str]:
    relative = path.relative_to(root)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    errors: list[str] = []

    if not any(CONTENT_TYPE.fullmatch(line) for line in lines[:6]):
        errors.append(f"{relative}: missing or invalid content-type declaration")
    if not any(CONTENT_PLAN.fullmatch(line) for line in lines[:6]):
        errors.append(f"{relative}: missing or invalid content-plan declaration")

    in_fence = False
    for number, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if not stripped.startswith("```"):
            continue
        if in_fence:
            in_fence = False
            continue
        language = stripped[3:].strip()
        if not language:
            errors.append(f"{relative}:{number}: opening code fence has no language tag")
        in_fence = True
    if in_fence:
        errors.append(f"{relative}: unclosed code fence")

    links = [*INLINE_LINK.findall(text), *REFERENCE_LINK.findall(text)]
    for raw_target in links:
        target = raw_target.strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "tel:")):
            continue
        path_part, _, fragment = target.partition("#")
        target_path = path if not path_part else (path.parent / unquote(path_part)).resolve()
        if not target_path.exists():
            errors.append(f"{relative}: local link target does not exist: {target}")
            continue
        if fragment and target_path.is_file() and target_path.suffix.lower() == ".md":
            target_text = target_path.read_text(encoding="utf-8")
            if unquote(fragment).lower() not in heading_anchors(target_text):
                errors.append(f"{relative}: local link anchor does not exist: {target}")
    return errors


def validate_index(root: Path, markdown_files: list[Path]) -> list[str]:
    index_path = root / "docs/README.md"
    if not index_path.is_file():
        return ["docs/README.md: documentation index is missing"]
    index_text = index_path.read_text(encoding="utf-8")
    indexed_paths: set[Path] = set()
    for raw_target in INLINE_LINK.findall(index_text):
        target = raw_target.strip("<>").partition("#")[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "tel:")):
            continue
        indexed_paths.add((index_path.parent / unquote(target)).resolve())

    errors: list[str] = []
    for path in markdown_files:
        if path == index_path:
            continue
        if path.resolve() not in indexed_paths:
            errors.append(f"docs/README.md: public document is not indexed: {path.relative_to(root)}")
    return errors


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for required in sorted(REQUIRED_FILES):
        if not (root / required).is_file():
            errors.append(f"{required}: required file is missing")

    markdown_files = public_markdown_files(root)
    for path in markdown_files:
        errors.extend(validate_markdown_file(root, path))
    errors.extend(validate_index(root, markdown_files))
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the public documentation contract.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    root = parse_args().root.resolve()
    errors = check(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"documentation contract passed for {len(public_markdown_files(root))} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
