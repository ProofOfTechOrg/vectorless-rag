#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path


def generated_values() -> dict[str, str]:
    return {
        "SESSION_SECRET": secrets.token_hex(32),
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "RAG_OWNER_PASSWORD": secrets.token_hex(32),
        "RAG_APP_PASSWORD": secrets.token_hex(32),
        "RAG_READER_PASSWORD": secrets.token_hex(32),
        "API_KEY_PEPPER": secrets.token_hex(32),
        "MINIO_ROOT_PASSWORD": secrets.token_hex(32),
        "CLICKHOUSE_PASSWORD": secrets.token_hex(32),
        "REDIS_PASSWORD": secrets.token_hex(32),
        "LANGFUSE_DB_PASSWORD": secrets.token_hex(32),
        "LANGFUSE_NEXTAUTH_SECRET": secrets.token_hex(32),
        "LANGFUSE_SALT": secrets.token_hex(32),
        "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": f"lf_pk_{secrets.token_hex(32)}",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY": f"lf_sk_{secrets.token_hex(32)}",
    }


def render(template: str, values: dict[str, str]) -> str:
    remaining = set(values)
    lines: list[str] = []
    for line in template.splitlines(keepends=True):
        key, separator, _ = line.partition("=")
        if separator and key in values:
            lines.append(f"{key}={values[key]}\n")
            remaining.remove(key)
        else:
            lines.append(line)

    if remaining:
        missing = ", ".join(sorted(remaining))
        raise ValueError(f"template is missing required assignments: {missing}")
    return "".join(lines)


def create_atomically(template_path: Path, output_path: Path) -> None:
    if output_path.exists():
        raise FileExistsError(f"{output_path} already exists; leaving it unchanged")

    content = render(template_path.read_text(encoding="utf-8"), generated_values())
    temporary_path = output_path.with_name(
        f".{output_path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    os.chmod(output_path, 0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a local Vectorless RAG environment without overwriting an existing file."
    )
    parser.add_argument("--template", type=Path, default=Path(".env.example"))
    parser.add_argument("--output", type=Path, default=Path(".env"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        create_atomically(args.template, args.output)
    except (FileExistsError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    print(f"created {args.output} with mode 600; add DEEPSEEK_API_KEY before provider-backed use")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
