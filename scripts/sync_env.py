#!/usr/bin/env python3
"""Reconcile a local .env against .env.example without disturbing configured values.

The example file is the schema: it carries the full key set, their grouping, and the
comments explaining each one. A .env drifts as the example gains keys, and the operator
only discovers the gap when something reads a default it never chose.

Rewriting .env in the example's shape would be trivial if values did not matter, but .env
holds live credentials. So an existing value is never replaced, never printed, and never
dropped - a key the example no longer lists is preserved in a trailing section rather than
deleted, because this tool cannot tell a stale key from a deliberate local one.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# Masked in every report. Matching the name is deliberate: a value that looks harmless today
# may not tomorrow, and the cost of masking a non-secret is nil.
SECRET = re.compile(r"(KEY|SECRET|PASSWORD|PEPPER|TOKEN|SALT|CREDENTIAL)", re.I)


def parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def show(key: str, value: str) -> str:
    if SECRET.search(key):
        return "<set>" if value.strip() else "<empty>"
    return value if value.strip() else "<empty>"


def render(
    example: Path, current: dict[str, str], adopt: set[str]
) -> tuple[str, list[str]]:
    """Emit the example's layout, substituting the values already configured locally."""
    lines: list[str] = []
    added: list[str] = []
    seen: set[str] = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT.match(line.strip())
        if match is None:
            lines.append(line)
            continue
        key, default = match.group(1), match.group(2)
        seen.add(key)
        if key in current and key not in adopt:
            lines.append(f"{key}={current[key]}")
        else:
            lines.append(f"{key}={default}")
            if key not in current:
                added.append(key)
    orphans = [key for key in current if key not in seen]
    if orphans:
        lines += [
            "",
            "# Keys not present in .env.example, preserved by scripts/sync_env.py.",
            "# Remove them by hand once you have confirmed nothing still reads them.",
        ]
        lines += [f"{key}={current[key]}" for key in orphans]
    return "\n".join(lines).rstrip("\n") + "\n", added


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=root / ".env")
    parser.add_argument("--example", type=Path, default=root / ".env.example")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero without writing",
    )
    parser.add_argument(
        "--adopt",
        default="",
        help="comma-separated keys to overwrite with the example's value",
    )
    args = parser.parse_args()

    if not args.env.exists():
        raise SystemExit(f"{args.env} does not exist; run make env first")
    current = parse(args.env)
    example = parse(args.example)
    adopt = {key.strip() for key in args.adopt.split(",") if key.strip()}
    unknown = sorted(adopt - set(example))
    if unknown:
        raise SystemExit(
            f"--adopt names keys absent from the example: {', '.join(unknown)}"
        )

    missing = [key for key in example if key not in current]
    orphans = [key for key in current if key not in example]
    differing = [
        key
        for key, value in example.items()
        if key in current and value.strip() and current[key].strip() != value.strip()
    ]

    print(f"{len(example)} keys in example, {len(current)} in {args.env.name}")
    print(f"\nmissing ({len(missing)}):")
    for key in missing:
        print(f"  + {key}={show(key, example[key])}")
    print(f"\nnot in example ({len(orphans)}), preserved:")
    for key in orphans:
        print(f"  ? {key}")
    print(
        f"\ndiffering from example ({len(differing)}), kept unless --adopt names them:"
    )
    for key in differing:
        print(
            f"  ~ {key}: local={show(key, current[key])} example={show(key, example[key])}"
        )

    if args.check:
        drifted = bool(missing or orphans)
        print("\ndrift detected" if drifted else "\nno structural drift")
        return 1 if drifted else 0
    if not missing and not adopt and not orphans:
        print("\nnothing to write")
        return 0

    content, added = render(args.example, current, adopt)
    # Verify before replacing: a rewrite that loses a configured value is unrecoverable.
    rebuilt = {
        match.group(1): match.group(2)
        for line in content.splitlines()
        if (match := ASSIGNMENT.match(line.strip()))
    }
    lost = [
        key
        for key, value in current.items()
        if key not in adopt and rebuilt.get(key) != value
    ]
    if lost:
        raise SystemExit(
            f"refusing to write: {len(lost)} value(s) would change: {lost}"
        )

    mode = args.env.stat().st_mode & 0o777
    # with_suffix on a dotfile yields ".env.env.bak"; name it explicitly so the backup lands
    # on the .gitignore entry rather than beside it, holding real credentials untracked.
    backup = args.env.with_name(f"{args.env.name}.bak")
    shutil.copy2(args.env, backup)
    os.chmod(backup, mode)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.env.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    temporary.replace(args.env)
    print(
        f"\nadded {len(added)} key(s); backup at {backup.name}; mode {mode:o} preserved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
