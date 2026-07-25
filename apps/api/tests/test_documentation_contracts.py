from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_every_phony_make_target_has_a_help_description() -> None:
    makefile = (ROOT / "Makefile").read_text()
    lines = makefile.splitlines()
    phony_line = next(index for index, line in enumerate(lines) if line.startswith(".PHONY:"))
    declaration = lines[phony_line].partition(":")[2].strip()
    while declaration.endswith("\\"):
        declaration = f"{declaration[:-1]} {lines[phony_line + 1].strip()}"
        phony_line += 1

    phony_targets = set(declaration.split())
    described_targets = set(
        re.findall(r"^([a-zA-Z0-9_-]+):[^\n]*##\s+\S", makefile, flags=re.MULTILINE)
    )
    missing = sorted(phony_targets - described_targets)

    assert not missing, f"Make targets missing help descriptions: {missing}"


def test_operator_make_targets_pass_explicit_runtime_contracts() -> None:
    makefile = (ROOT / "Makefile").read_text()

    assert "scripts/seed_evaluation.py /dev/stdin \\" in makefile
    assert "--suite pageindex-v2 --corpus evaluation/pageindex-v2-corpus.json" in makefile
    assert "$(COMPOSE) exec -T api python scripts/run_pageindex_pilot.py" in makefile
    assert '--release-sha "$$release_sha" --output -' in makefile
    assert 'mkdir -p "$$(dirname "$$output_dir")"' in makefile
    assert makefile.index('mkdir "$$output_dir"') < makefile.index(
        "$(COMPOSE) exec -T api python scripts/run_pageindex_pilot.py"
    )

    dockerignore = (ROOT / "apps/api/.dockerignore").read_text().splitlines()
    assert "evaluation/run/" in dockerignore
    assert "pilots/run/" in dockerignore


def test_helm_secret_references_match_the_deployment_inventory() -> None:
    templates = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "infra/helm/vectorless-rag/templates").glob("*.yaml"))
    )
    template_keys = set(
        re.findall(r"secretKeyRef:.*?\bkey:\s*([a-z0-9-]+)", templates, flags=re.DOTALL)
    )

    deployment = (ROOT / "docs/deployment.md").read_text()
    start_marker = "<!-- helm-secret-inventory:start -->"
    end_marker = "<!-- helm-secret-inventory:end -->"
    inventory = deployment.split(start_marker, maxsplit=1)[1].split(end_marker, maxsplit=1)[0]
    documented_keys = set(re.findall(r"^- `([a-z0-9-]+)`$", inventory, flags=re.MULTILINE))

    missing = sorted(template_keys - documented_keys)
    extra = sorted(documented_keys - template_keys)

    assert not missing and not extra, (
        f"Helm Secret inventory mismatch: missing={missing}, extra={extra}"
    )
