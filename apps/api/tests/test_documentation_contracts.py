from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

from vectorless_rag import __version__

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
    assert 'RELEASE="$$release_sha" $(UV) run --project $(API_DIR) \\' in makefile
    assert "python $(API_DIR)/scripts/run_evaluation.py \\" in makefile
    assert (
        'RELEASE="$$release_sha" $(COMPOSE) up -d --build --force-recreate api worker' in makefile
    )
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    assert "ARG RELEASE=dev" in dockerfile
    assert "RUN printf '%s\\n' \"$RELEASE\" > /app/.release" in dockerfile
    assert "args:\n        RELEASE: ${RELEASE}" in compose

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


def test_public_documentation_contract_passes() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts/docs_check.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_documentation_checker_reports_markdown_contract_failures(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text(
        "<!-- Content type: Reference -->\n"
        "<!-- Content plan: Goal: test anchors. Audience: tests. Content: headings. "
        "Open questions: none. -->\n\n"
        "# Existing heading\n",
        encoding="utf-8",
    )
    page = tmp_path / "page.md"
    page.write_text(
        "# Missing declarations\n\n"
        "[Missing file](absent.md)\n\n"
        "[Missing anchor](target.md#absent-heading)\n\n"
        "```\n"
        "untagged\n"
        "```\n",
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts/docs_check.py"),
            "--root",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "missing or invalid content-type" in result.stderr
    assert "missing or invalid content-plan" in result.stderr
    assert "local link target does not exist" in result.stderr
    assert "local link anchor does not exist" in result.stderr
    assert "opening code fence has no language tag" in result.stderr


def test_license_and_release_metadata_are_consistent() -> None:
    pyproject = tomllib.loads((ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    root_package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    web_package = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
    chart = yaml.safe_load(
        (ROOT / "infra/helm/vectorless-rag/Chart.yaml").read_text(encoding="utf-8")
    )
    chart_values = yaml.safe_load(
        (ROOT / "infra/helm/vectorless-rag/values.yaml").read_text(encoding="utf-8")
    )
    uv_lock = (ROOT / "apps/api/uv.lock").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert {
        pyproject["project"]["version"],
        root_package["version"],
        web_package["version"],
        chart["version"],
        chart["appVersion"],
        chart_values["image"]["tag"],
        chart_values["webImage"]["tag"],
        __version__,
    } == {"0.3.0"}
    assert pyproject["project"]["license"] == "MIT"
    assert root_package["license"] == "MIT"
    assert web_package["license"] == "MIT"
    assert chart["annotations"]["artifacthub.io/license"] == "MIT"
    assert 'name = "vectorless-rag"\nversion = "0.3.0"' in uv_lock
    assert "Copyright (c) 2026 ProofOfTechOrg" in license_text


def test_vulnerable_pdf_and_glob_dependencies_are_absent() -> None:
    pyproject = tomllib.loads((ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = (ROOT / "apps/api/uv.lock").read_text(encoding="utf-8").casefold()
    pnpm_lock = (ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    workspace = yaml.safe_load((ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8"))

    dependencies = [value.casefold() for value in pyproject["project"]["dependencies"]]
    assert any(value.startswith("pypdf==") for value in dependencies)
    assert all(not value.startswith("pypdf2") for value in dependencies)
    assert '\nname = "pypdf2"\n' not in uv_lock

    brace_versions = [
        tuple(int(part) for part in version.split("."))
        for version in re.findall(
            r"^  brace-expansion@(\d+\.\d+\.\d+):$", pnpm_lock, flags=re.MULTILINE
        )
    ]
    override = tuple(
        int(part) for part in str(workspace["overrides"]["brace-expansion"]).split(".")
    )
    assert brace_versions
    assert all(version >= (5, 0, 8) for version in brace_versions)
    assert override >= (5, 0, 8)


def test_provider_price_and_retry_settings_reach_deployments() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    values = yaml.safe_load(
        (ROOT / "infra/helm/vectorless-rag/values.yaml").read_text(encoding="utf-8")
    )
    template = (ROOT / "infra/helm/vectorless-rag/templates/app.yaml").read_text(encoding="utf-8")
    prices = {
        "PAGEINDEX_INPUT_COST_PER_MILLION_USD": "pageindexInputCostPerMillionUsd",
        "PAGEINDEX_CACHE_HIT_INPUT_COST_PER_MILLION_USD": (
            "pageindexCacheHitInputCostPerMillionUsd"
        ),
        "PAGEINDEX_OUTPUT_COST_PER_MILLION_USD": "pageindexOutputCostPerMillionUsd",
    }

    for environment_name, helm_name in prices.items():
        assert f"{environment_name}=" in environment
        assert f"{environment_name}: ${{{environment_name}}}" in compose
        assert helm_name in values
        assert (
            f"- {{name: {environment_name}, value: {{{{ .Values.{helm_name} | quote }}}}}}"
        ) in template

    assert "PAGEINDEX_LLM_MAX_ATTEMPTS=3" in environment
    assert "PAGEINDEX_LLM_MAX_ATTEMPTS: ${PAGEINDEX_LLM_MAX_ATTEMPTS:-3}" in compose
    assert values["pageindexLlmMaxAttempts"] == 3
    assert (
        "- {name: PAGEINDEX_LLM_MAX_ATTEMPTS, value: {{ .Values.pageindexLlmMaxAttempts | quote }}}"
    ) in template
