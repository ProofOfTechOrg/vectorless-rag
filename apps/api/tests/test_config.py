from pathlib import Path

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
