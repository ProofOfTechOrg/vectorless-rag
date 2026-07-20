from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from vectorless_rag.config import Settings


def test_empty_optional_environment_values_are_ignored(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_INPUT_COST_PER_MILLION_USD", "")
    monkeypatch.setenv("DEEPSEEK_OUTPUT_COST_PER_MILLION_USD", "")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")

    monkeypatch.chdir(tmp_path)
    settings = Settings()

    assert settings.deepseek_api_key is None
    assert settings.deepseek_input_cost_per_million_usd is None
    assert settings.deepseek_output_cost_per_million_usd is None
    assert settings.langfuse_public_key is None
    assert settings.langfuse_secret_key is None


def test_ingestion_operational_defaults_are_bounded_and_patch_versioned() -> None:
    settings = Settings()

    assert settings.pageindex_llm_max_attempts == 3
    assert settings.pageindex_timeout_seconds == 3_600
    assert settings.ingestion_max_attempts == 3
    assert settings.ingestion_stale_grace_seconds == 300
    assert settings.pageindex_version.endswith("+vr2")


def test_unpatched_pageindex_artifact_version_cannot_be_selected(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PAGEINDEX_VERSION", "190f8b378be58199ca993566a9214dba72089c54")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        Settings()
