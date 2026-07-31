from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pytest import MonkeyPatch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = API_ROOT.parents[1]
sys.path.insert(0, str(API_ROOT))

from scripts import (  # noqa: E402
    execute_evaluation,
    judge_evaluation,
    run_evaluation,
    run_pageindex_pilot,
    seed_evaluation,
)
from vectorless_rag.config import Settings  # noqa: E402
from vectorless_rag.db import make_engine  # noqa: E402
from vectorless_rag.evaluation_suite import (  # noqa: E402
    EVALUATION_RUNTIME_SETTING_NAMES,
    EVIDENCE_BUDGETS,
    ClaimJudgment,
    EvaluationCorpusManifest,
    EvaluationScore,
    EvaluationTrial,
    FrozenEvaluationManifest,
    build_trials,
    evaluation_configuration_sha256,
    evaluation_price_snapshot,
    file_sha256,
    frozen_index_sha256,
    load_cases,
    load_corpus_manifest,
    validate_indexed_corpus,
)
from vectorless_rag.llm import MAX_PROVIDER_ATTEMPTS  # noqa: E402
from vectorless_rag.models import (  # noqa: E402
    ApiKey,
    Document,
    DocumentMetadataVersion,
    DocumentStatus,
    Experiment,
    ExperimentIndexVersion,
    ExperimentMembership,
    IndexLifecycleStatus,
    IndexVersion,
    QueryRun,
    RunFailureCode,
    RunRoute,
    SqlAudit,
)
from vectorless_rag.retrieval import CorpusRouter  # noqa: E402
from vectorless_rag.schemas import DocumentMetadata, MetadataConstraints  # noqa: E402
from vectorless_rag.sql_guard import SQLRejected  # noqa: E402
from vectorless_rag.usage import ExperimentBudgetExceeded  # noqa: E402

OWNER_URL = os.getenv("TEST_OWNER_DATABASE_URL")


def _complete_price_snapshot() -> dict[str, float]:
    return {
        "input_per_million_usd": 0.14,
        "cache_hit_input_per_million_usd": 0.0028,
        "output_per_million_usd": 0.28,
        "max_input_tokens": 1_000_000.0,
        "max_output_tokens": 8_192.0,
        "max_attempts": float(MAX_PROVIDER_ATTEMPTS),
    }


def test_pilot_schema_guard_matches_alembic_head() -> None:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option("path_separator", "os")

    assert run_pageindex_pilot.MEASUREMENT_SCHEMA_REVISION == (
        ScriptDirectory.from_config(config).get_current_head()
    )


def test_pageindex_installer_resolves_patch_from_relative_script_path(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        """#!/usr/bin/env python3
import sys
from pathlib import Path

arguments = sys.argv[1:]
if arguments[0] == "clone":
    Path(arguments[-1]).mkdir(parents=True)
elif arguments[0] == "-C" and "apply" in arguments:
    patch = Path(arguments[-1])
    if not patch.is_absolute() or not patch.is_file():
        raise SystemExit(42)
""",
        encoding="utf-8",
    )
    fake_git.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    script = API_ROOT / "scripts/install-pageindex.sh"
    shell = shutil.which("sh")
    assert shell is not None
    environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = subprocess.run(  # noqa: S603
        [shell, str(script.relative_to(REPOSITORY_ROOT)), str(tmp_path / "pageindex")],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


async def _add_indexed_document(
    session: AsyncSession,
    *,
    label: str,
    metadata: DocumentMetadata,
) -> tuple[Document, IndexVersion]:
    identifier = uuid.uuid4()
    digest = identifier.hex + identifier.hex
    document = Document(
        id=identifier,
        sha256=digest,
        arxiv_id=f"evaluation-{identifier.hex[:12]}",
        original_filename=f"{label}.pdf",
        pdf_object_key=f"pdf/{identifier}.pdf",
        title=metadata.title,
        authors=list(metadata.authors),
        abstract=metadata.abstract,
        description=metadata.description,
        topics=list(metadata.topics),
        submitted_date=metadata.submitted_date,
        status=DocumentStatus.ready,
        page_count=1,
    )
    session.add(document)
    await session.flush()

    metadata_version = DocumentMetadataVersion(
        id=uuid.uuid4(),
        document_id=document.id,
        recipe_key=f"evaluation-{identifier.hex[:12]}",
        configuration_hash=digest,
        content_sha256=digest,
        metadata_payload=metadata.model_dump(mode="json"),
        page_count=1,
        extraction_quality=1.0,
    )
    session.add(metadata_version)
    await session.flush()

    index_version = IndexVersion(
        id=uuid.uuid4(),
        document_id=document.id,
        version_key=f"evaluation-{identifier.hex[:12]}",
        pageindex_version="evaluation-test",
        prompt_version="evaluation-test",
        model_name="evaluation-test",
        artifact_object_key=f"artifacts/{identifier}.json",
        artifact_sha256=digest,
        extraction_quality=1.0,
        artifact_schema_version=2,
        recipe_key=f"evaluation-{identifier.hex[:12]}",
        configuration_hash=digest,
        metadata_version_id=metadata_version.id,
        page_count=1,
        node_count=1,
        max_depth=0,
        lifecycle_status=IndexLifecycleStatus.verified,
        verified_at=datetime.now(UTC),
    )
    session.add(index_version)
    await session.flush()
    document.active_index_version_id = index_version.id
    await session.flush()
    return document, index_version


def test_pilot_decimal_cost_is_json_serializable() -> None:
    reported = run_pageindex_pilot.report_cost(Decimal("1.2345678"))

    assert reported == 1.234568
    assert json.loads(json.dumps({"cost": reported})) == {"cost": 1.234568}


def test_pilot_price_snapshot_covers_pageindex_and_metadata_retry_bounds() -> None:
    for pageindex_attempts, expected in (
        (1, MAX_PROVIDER_ATTEMPTS),
        (MAX_PROVIDER_ATTEMPTS, MAX_PROVIDER_ATTEMPTS),
        (MAX_PROVIDER_ATTEMPTS + 1, MAX_PROVIDER_ATTEMPTS + 1),
    ):
        assert (
            run_pageindex_pilot.pilot_price_snapshot(
                Settings(
                    pageindex_llm_max_attempts=pageindex_attempts,
                )
            )["max_attempts"]
            == expected
        )


def test_explicit_pageindex_suite_does_not_depend_on_stdin_filename(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path]] = []
    corpus = Path("evaluation/pageindex-v2-corpus.json")

    def load_cases(path: Path) -> list[Any]:
        calls.append(("cases", path))
        return []

    def load_corpus(path: Path) -> object:
        calls.append(("corpus", path))
        return object()

    def validate(cases: object, manifest: object, *, require_reviewed: bool) -> None:
        del cases, manifest
        assert require_reviewed is True

    def fail_release(path: Path) -> list[dict[str, object]]:
        pytest.fail(f"selected release loader for {path}")

    monkeypatch.setattr(seed_evaluation, "load_pageindex_cases", load_cases)
    monkeypatch.setattr(seed_evaluation, "load_corpus_manifest", load_corpus)
    monkeypatch.setattr(seed_evaluation, "validate_suite", validate)
    monkeypatch.setattr(seed_evaluation, "load_release_cases", fail_release)

    cases = seed_evaluation.load_seed_cases(
        Path("/dev/stdin"),
        suite="pageindex-v2",
        corpus=corpus,
    )

    assert cases == []
    assert calls == [("cases", Path("/dev/stdin")), ("corpus", corpus)]


def test_seed_auto_mode_preserves_legacy_filename_selection(
    monkeypatch: MonkeyPatch,
) -> None:
    expected: list[dict[str, object]] = [{"item_id": "legacy"}]

    def load_release(path: Path) -> list[dict[str, object]]:
        del path
        return expected

    def fail_pageindex(path: Path) -> list[Any]:
        pytest.fail(f"selected PageIndex loader for {path}")

    monkeypatch.setattr(seed_evaluation, "load_release_cases", load_release)
    monkeypatch.setattr(seed_evaluation, "load_pageindex_cases", fail_pageindex)

    assert (
        seed_evaluation.load_seed_cases(
            Path("release-v1.jsonl"),
            suite="auto",
            corpus=None,
        )
        == expected
    )


def test_existing_pilot_output_blocks_before_model_work(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "run" / "result.json"
    output.parent.mkdir()
    called = False

    async def fake_run(args: object) -> dict[str, object]:
        del args
        nonlocal called
        called = True
        return {}

    def load_manifest(path: Path) -> None:
        del path

    monkeypatch.setattr(run_pageindex_pilot, "load_pilot_manifest", load_manifest)
    monkeypatch.setattr(run_pageindex_pilot, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pageindex_pilot.py",
            "--apply",
            "--confirm-cost-cap",
            "10",
            "--release-sha",
            "a" * 40,
            "--output",
            str(output),
        ],
    )

    with pytest.raises(FileExistsError):
        run_pageindex_pilot.main()

    assert called is False


def test_pilot_reserves_output_before_running(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "run" / "result.json"

    async def fake_run(args: object) -> dict[str, object]:
        del args
        assert output.parent.is_dir()
        assert (output.parent / ".result.json.tmp").is_file()
        return {"release_sha": "a" * 40}

    def load_manifest(path: Path) -> None:
        del path

    monkeypatch.setattr(run_pageindex_pilot, "load_pilot_manifest", load_manifest)
    monkeypatch.setattr(run_pageindex_pilot, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pageindex_pilot.py",
            "--apply",
            "--confirm-cost-cap",
            "10",
            "--release-sha",
            "a" * 40,
            "--output",
            str(output),
        ],
    )

    run_pageindex_pilot.main()

    assert output.read_text(encoding="utf-8").endswith("\n")
    assert not (output.parent / ".result.json.tmp").exists()


def test_environment_initializer_is_atomic_private_and_non_overwriting(tmp_path: Path) -> None:
    root = API_ROOT.parents[1]
    template = root / ".env.example"
    output = tmp_path / ".env"
    command = [
        sys.executable,
        str(root / "scripts/init_env.py"),
        "--template",
        str(template),
        "--output",
        str(output),
    ]

    processes = [
        subprocess.Popen(  # noqa: S603
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]
    return_codes = sorted(process.wait() for process in processes)

    assert return_codes == [0, 2]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".env.tmp-*"))

    assignments = dict(
        line.split("=", maxsplit=1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert assignments["DEEPSEEK_API_KEY"] == ""

    hex_keys = {
        "SESSION_SECRET",
        "POSTGRES_PASSWORD",
        "RAG_OWNER_PASSWORD",
        "RAG_APP_PASSWORD",
        "RAG_READER_PASSWORD",
        "API_KEY_PEPPER",
        "MINIO_ROOT_PASSWORD",
        "CLICKHOUSE_PASSWORD",
        "REDIS_PASSWORD",
        "LANGFUSE_DB_PASSWORD",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
    }
    for key in hex_keys:
        assert re.fullmatch(r"[0-9a-f]{64}", assignments[key]), key
    assert re.fullmatch(r"lf_pk_[0-9a-f]{64}", assignments["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"])
    assert re.fullmatch(r"lf_sk_[0-9a-f]{64}", assignments["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
    assert assignments["LANGFUSE_INIT_USER_PASSWORD"] == ""

    combined_output = "\n".join(value for result in results for value in result)
    for key in hex_keys:
        assert assignments[key] not in combined_output

    original = output.read_bytes()
    repeated = subprocess.run(  # noqa: S603
        command, text=True, capture_output=True, check=False
    )
    assert repeated.returncode == 2
    assert output.read_bytes() == original
    assert "already exists; leaving it unchanged" in repeated.stderr


def test_integration_runner_cleans_only_its_project_after_failure(tmp_path: Path) -> None:
    root = API_ROOT.parents[1]
    call_log = tmp_path / "docker-calls.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "CALL_LOG": str(call_log),
            "DOCKER": str(fake_docker),
            "UV": "/bin/false",
            "MAKE": "/bin/true",
            "TEST_POSTGRES_PORT": "55439",
        }
    )
    result = subprocess.run(  # noqa: S603
        [str(root / "scripts/test-integration-stack.sh")],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    project_matches = [
        re.search(r"--project-name (vectorless-rag-integration-[^ ]+)", call) for call in calls
    ]
    assert all(match is not None for match in project_matches)
    project_names = [match.group(1) for match in project_matches if match is not None]
    assert len(set(project_names)) == 1
    assert "up -d --wait postgres" in calls[0]
    assert "down --volumes --remove-orphans" in calls[1]
    assert "--project-name vectorless-rag " not in "\n".join(calls)


def test_a_frozen_evaluation_plan_requires_a_positive_cost_ceiling() -> None:
    from pydantic import ValidationError

    fields: dict[str, Any] = {
        "schema_version": 2,
        "experiment_id": uuid.uuid4(),
        "release_sha": "0" * 40,
        "configuration_sha256": "1" * 64,
        "corpus_sha256": "2" * 64,
        "index_sha256": "3" * 64,
        "dataset_sha256": "4" * 64,
        "price_snapshot": _complete_price_snapshot(),
        "repetitions": 1,
        "evidence_budgets": EVIDENCE_BUDGETS,
        "execution_concurrency": 4,
        "seed": 1,
        "created_at": datetime.now(UTC),
    }

    # An unbounded run is the failure this field exists to prevent, so it carries no default
    # a caller could fall through to.
    with pytest.raises(ValidationError):
        FrozenEvaluationManifest.model_validate(fields)
    with pytest.raises(ValidationError):
        FrozenEvaluationManifest.model_validate({**fields, "maximum_cost_usd": 0})
    plan = FrozenEvaluationManifest.model_validate({**fields, "maximum_cost_usd": "25.000001"})
    assert plan.maximum_cost_usd == Decimal("25.000001")
    assert (
        FrozenEvaluationManifest.model_validate_json(plan.model_dump_json()).maximum_cost_usd
        == plan.maximum_cost_usd
    )


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_frozen_evaluation_plan_requires_a_finite_cost_ceiling(value: float) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FrozenEvaluationManifest(
            schema_version=2,
            experiment_id=uuid.uuid4(),
            release_sha="0" * 40,
            configuration_sha256="1" * 64,
            corpus_sha256="2" * 64,
            index_sha256="3" * 64,
            dataset_sha256="4" * 64,
            price_snapshot=_complete_price_snapshot(),
            repetitions=1,
            evidence_budgets=EVIDENCE_BUDGETS,
            execution_concurrency=4,
            seed=1,
            maximum_cost_usd=Decimal(str(value)),
            created_at=datetime.now(UTC),
        )


@pytest.mark.parametrize("value", ["25.1234567", "100000000"])
def test_a_frozen_evaluation_plan_requires_a_database_exact_cost_ceiling(value: str) -> None:
    from pydantic import ValidationError

    fields: dict[str, Any] = {
        "schema_version": 2,
        "experiment_id": uuid.uuid4(),
        "release_sha": "0" * 40,
        "configuration_sha256": "1" * 64,
        "corpus_sha256": "2" * 64,
        "index_sha256": "3" * 64,
        "dataset_sha256": "4" * 64,
        "price_snapshot": _complete_price_snapshot(),
        "repetitions": 1,
        "evidence_budgets": EVIDENCE_BUDGETS,
        "execution_concurrency": 4,
        "seed": 1,
        "maximum_cost_usd": value,
        "created_at": datetime.now(UTC),
    }

    with pytest.raises(ValidationError):
        FrozenEvaluationManifest.model_validate(fields)
    with pytest.raises(argparse.ArgumentTypeError):
        run_evaluation.cost_ceiling(value)


def test_frozen_evaluation_plan_requires_cost_reservation_bounds() -> None:
    from pydantic import ValidationError

    fields: dict[str, Any] = {
        "schema_version": 2,
        "experiment_id": uuid.uuid4(),
        "release_sha": "0" * 40,
        "configuration_sha256": "1" * 64,
        "corpus_sha256": "2" * 64,
        "index_sha256": "3" * 64,
        "dataset_sha256": "4" * 64,
        "price_snapshot": {
            key: value for key, value in _complete_price_snapshot().items() if key != "max_attempts"
        },
        "repetitions": 1,
        "evidence_budgets": EVIDENCE_BUDGETS,
        "execution_concurrency": 4,
        "seed": 1,
        "maximum_cost_usd": 25.0,
        "created_at": datetime.now(UTC),
    }

    with pytest.raises(ValidationError, match="price snapshot is incomplete"):
        FrozenEvaluationManifest.model_validate(fields)


def test_frozen_evaluation_plan_rejects_zero_cost_rates() -> None:
    from pydantic import ValidationError

    snapshot = _complete_price_snapshot()
    snapshot.update(
        {
            "input_per_million_usd": 0.0,
            "cache_hit_input_per_million_usd": 0.0,
            "output_per_million_usd": 0.0,
        }
    )
    with pytest.raises(ValidationError, match="provider prices must be positive"):
        FrozenEvaluationManifest(
            schema_version=2,
            experiment_id=uuid.uuid4(),
            release_sha="0" * 40,
            configuration_sha256="1" * 64,
            corpus_sha256="2" * 64,
            index_sha256="3" * 64,
            dataset_sha256="4" * 64,
            price_snapshot=snapshot,
            repetitions=1,
            evidence_budgets=EVIDENCE_BUDGETS,
            execution_concurrency=4,
            seed=1,
            maximum_cost_usd=Decimal("25"),
            created_at=datetime.now(UTC),
        )


def test_run_plan_snapshots_the_largest_output_and_retry_bounds() -> None:
    snapshot = evaluation_price_snapshot(
        Settings(
            deepseek_input_cost_per_million_usd=0.14,
            deepseek_cache_hit_input_cost_per_million_usd=0.0028,
            deepseek_output_cost_per_million_usd=0.28,
            pageindex_max_output_tokens=8_192,
            query_node_selection_max_output_tokens=12_000,
        ),
        maximum_provider_attempts=MAX_PROVIDER_ATTEMPTS,
    )

    assert snapshot == {
        **_complete_price_snapshot(),
        "max_output_tokens": 12_000.0,
        "max_attempts": float(MAX_PROVIDER_ATTEMPTS),
    }


def test_evaluation_configuration_hash_covers_every_result_affecting_setting() -> None:
    assert EVALUATION_RUNTIME_SETTING_NAMES == (
        "deepseek_model",
        "pageindex_max_output_tokens",
        "query_node_selection_max_output_tokens",
        "pageindex_call_timeout_seconds",
        "request_deadline_seconds",
        "candidate_limit",
        "catalog_batch_token_limit",
        "catalog_routing_concurrency",
        "document_limit",
        "page_ranges_per_document",
        "fetched_page_limit",
        "evidence_token_limit",
        "sql_row_limit",
        "sql_timeout_ms",
        "sql_lock_timeout_ms",
        "sql_payload_limit_bytes",
        "sql_explain_cost_ceiling",
    )
    settings = Settings()
    baseline = evaluation_configuration_sha256(settings, execution_concurrency=4)

    for name in EVALUATION_RUNTIME_SETTING_NAMES:
        value = getattr(settings, name)
        changed = f"{value}-changed" if isinstance(value, str) else value + 1
        assert (
            evaluation_configuration_sha256(
                settings.model_copy(update={name: changed}),
                execution_concurrency=4,
            )
            != baseline
        ), name
    assert evaluation_configuration_sha256(settings, execution_concurrency=3) != baseline


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_evaluation_run_and_membership_persist_before_provider_work() -> None:
    assert OWNER_URL is not None
    engine = make_engine(OWNER_URL)
    experiment_id = uuid.uuid4()
    manifest = FrozenEvaluationManifest(
        schema_version=2,
        experiment_id=experiment_id,
        release_sha="0" * 40,
        configuration_sha256="1" * 64,
        corpus_sha256="2" * 64,
        index_sha256="3" * 64,
        dataset_sha256="4" * 64,
        price_snapshot=_complete_price_snapshot(),
        repetitions=1,
        evidence_budgets=EVIDENCE_BUDGETS,
        execution_concurrency=4,
        seed=1,
        maximum_cost_usd=Decimal("25"),
        created_at=datetime.now(UTC),
    )
    trial = EvaluationTrial(
        trial_id=uuid.uuid4(),
        sequence_number=0,
        item_id=uuid.uuid4(),
        split="dev",
        stratum="unconstrained_discovery",
        experiment_kind="route",
        evidence_token_budget=6_400,
        repetition=1,
    )
    async with engine.connect() as connection:
        transaction = await connection.begin()
        sessions = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with sessions() as session:
                _, index_version = await _add_indexed_document(
                    session,
                    label="experiment-provenance",
                    metadata=DocumentMetadata(
                        title="Experiment provenance",
                        authors=["Evaluation Author"],
                        abstract="An immutable metadata fixture.",
                        description="An immutable metadata fixture.",
                        topics=["evaluation"],
                        submitted_date=date(2026, 7, 29),
                    ),
                )
                frozen_index_versions = frozenset({index_version.id})
                api_key_id = await execute_evaluation.ensure_experiment(
                    session,
                    manifest,
                    frozen_index_versions,
                )
                await session.commit()
            async with sessions() as session:
                run, created = await execute_evaluation.create_trial_run(
                    session,
                    experiment_id=experiment_id,
                    api_key_id=api_key_id,
                    question="Which finding is supported?",
                    trial=trial,
                )
                assert created is True
            async with sessions() as session:
                stored_key = await session.get(ApiKey, api_key_id)
                stored_run = await session.get(QueryRun, run.id)
                assert (
                    await execute_evaluation.ensure_experiment(
                        session,
                        manifest,
                        frozen_index_versions,
                    )
                    == api_key_id
                )
                membership = await session.scalar(
                    select(ExperimentMembership).where(ExperimentMembership.query_run_id == run.id)
                )
                assert await session.get(Experiment, experiment_id) is not None
                persisted_index_versions = frozenset(
                    await session.scalars(
                        select(ExperimentIndexVersion.index_version_id).where(
                            ExperimentIndexVersion.experiment_id == experiment_id
                        )
                    )
                )
                assert persisted_index_versions == frozen_index_versions
                assert stored_key is not None and stored_key.disabled is True
                assert stored_key.scopes == []
                assert stored_run is not None and stored_run.api_key_id == api_key_id
                assert membership is not None and membership.experiment_id == experiment_id
                resumed_run, resumed_created = await execute_evaluation.create_trial_run(
                    session,
                    experiment_id=experiment_id,
                    api_key_id=api_key_id,
                    question="Which finding is supported?",
                    trial=trial,
                )
                assert resumed_run.id == run.id
                assert resumed_created is False
                with pytest.raises(SystemExit, match="identity"):
                    await execute_evaluation.create_trial_run(
                        session,
                        experiment_id=experiment_id,
                        api_key_id=api_key_id,
                        question="A different question",
                        trial=trial,
                    )
        finally:
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_pinned_catalog_query_executes_against_immutable_metadata() -> None:
    assert OWNER_URL is not None
    engine = make_engine(OWNER_URL)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        sessions = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with sessions() as session:
                frozen = DocumentMetadata(
                    title="Immutable quasar calibration findings",
                    authors=["Snapshot Author"],
                    abstract="The immutable snapshot records a calibrated quasar finding.",
                    description="A frozen quasar calibration record.",
                    topics=["quasar-calibration"],
                    submitted_date=date(2025, 6, 14),
                )
                document, index_version = await _add_indexed_document(
                    session,
                    label="immutable-metadata",
                    metadata=frozen,
                )
                other_document, other_index_version = await _add_indexed_document(
                    session,
                    label="unrelated-metadata",
                    metadata=DocumentMetadata(
                        title="Unrelated baseline control",
                        authors=["Control Author"],
                        abstract="A control fixture with unrelated terms.",
                        description="An unrelated control record.",
                        topics=["control"],
                        submitted_date=date(2024, 1, 2),
                    ),
                )

                document.title = "Current-only title"
                document.authors = ["Current-only Author"]
                document.abstract = "current-only abstract"
                document.description = "current-only description"
                document.topics = ["current-only-topic"]
                document.submitted_date = date(2026, 7, 29)
                other_document.title = frozen.title
                await session.flush()

                pinned = {
                    document.id: index_version.id,
                    other_document.id: other_index_version.id,
                }
                router = CorpusRouter(Settings(), cast(Any, object()))
                ranked = await router.candidates(
                    session,
                    frozen.title,
                    MetadataConstraints(),
                    index_version_ids=pinned,
                )
                constrained = await router.candidates(
                    session,
                    frozen.title,
                    MetadataConstraints(
                        authors=[frozen.authors[0]],
                        topics=[frozen.topics[0]],
                        date_from=frozen.submitted_date,
                        date_to=frozen.submitted_date,
                    ),
                    index_version_ids=pinned,
                )
                current_only = await router.candidates(
                    session,
                    "Current-only title",
                    MetadataConstraints(
                        authors=["Current-only Author"],
                        topics=["current-only-topic"],
                    ),
                    index_version_ids=pinned,
                )

            assert ranked[0].document.id == document.id
            assert ranked[0].document.title == frozen.title
            assert [candidate.document.id for candidate in constrained] == [document.id]
            assert current_only == []
        finally:
            await transaction.rollback()
    await engine.dispose()


def _score_line(trial_id: str) -> str:
    return json.dumps({"trial_id": trial_id, "route_correct": 1.0})


def test_resume_discards_only_a_torn_final_line(tmp_path: Path) -> None:
    from scripts import execute_evaluation

    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    ledger = tmp_path / "scores.jsonl"
    # A kill during the final append leaves the last line half written.
    ledger.write_text(f'{_score_line(first)}\n{_score_line(second)}\n{{"trial_id": "abc')

    done = execute_evaluation.scored_trials(ledger)

    assert done == {uuid.UUID(first), uuid.UUID(second)}
    # Rewritten without the torn tail, so the next append cannot run onto it.
    assert ledger.read_text() == f"{_score_line(first)}\n{_score_line(second)}\n"


def test_resume_refuses_a_torn_line_that_is_not_the_last(tmp_path: Path) -> None:
    from scripts import execute_evaluation

    ledger = tmp_path / "scores.jsonl"
    ledger.write_text(f'{{"trial_id": "abc\n{_score_line(str(uuid.uuid4()))}\n')

    # Damage anywhere but the tail was not caused by an interrupted append, so it is not
    # something this script may quietly discard.
    with pytest.raises(SystemExit):
        execute_evaluation.scored_trials(ledger)


def test_judge_queue_append_is_idempotent_and_rejects_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "judge-queue.jsonl"
    item = execute_evaluation.ClaimJudgeItem(
        trial_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        claims=["The answer includes the target finding."],
        answer="The answer includes the target finding.",
    )

    execute_evaluation.append_judge_item(path, item)
    execute_evaluation.append_judge_item(path, item)

    assert path.read_text(encoding="utf-8") == item.model_dump_json() + "\n"
    with pytest.raises(SystemExit, match="conflicting content"):
        execute_evaluation.append_judge_item(
            path,
            item.model_copy(update={"answer": "Different reconstructed answer."}),
        )


def test_each_experiment_kind_maps_onto_its_own_overrides() -> None:
    from scripts import execute_evaluation

    oracle = (uuid.uuid4(),)
    free = execute_evaluation.options_for("route", 6_400, oracle)
    oracled = execute_evaluation.options_for("oracle_document_retrieval", 6_400, oracle)
    forced = execute_evaluation.options_for("forced_route_end_to_end", 6_400, oracle)

    assert free.forced_route is None, "route measures the router's own choice"
    assert oracled.oracle_document_ids == oracle
    assert forced.forced_route is execute_evaluation.EXPECTED_ROUTE
    assert forced.oracle_document_ids == ()


def test_trial_plan_does_not_repeat_end_to_end_runs_as_corpus_routing() -> None:
    cases = load_cases(API_ROOT / "evaluation/pageindex-v2-regression.jsonl")

    trials = build_trials(cases, seed=7)

    assert len(trials) == 1_080
    assert {trial.experiment_kind for trial in trials} == {
        "route",
        "oracle_document_retrieval",
        "forced_route_end_to_end",
    }


def test_claim_judgments_are_validated_and_applied_atomically(tmp_path: Path) -> None:
    trial_id = uuid.uuid4()
    item_id = uuid.uuid4()
    run = tmp_path / "run"
    run.mkdir()
    (run / "judge-queue.jsonl").write_text(
        json.dumps(
            {
                "trial_id": str(trial_id),
                "item_id": str(item_id),
                "claims": ["first", "second"],
                "answer": "Only the first claim is present.",
            }
        )
        + "\n"
    )
    (run / "scores.jsonl").write_text(EvaluationScore(trial_id=trial_id).model_dump_json() + "\n")
    (run / "claim-judgments.jsonl").write_text(
        json.dumps(
            {
                "trial_id": str(trial_id),
                "item_id": str(item_id),
                "claims": ["first", "second"],
                "answer_sha256": hashlib.sha256(b"Only the first claim is present.").hexdigest(),
                "supported": [True, False],
                "reviewer": "reviewer",
                "method": "human",
                "reviewed_at": datetime.now(UTC).isoformat(),
            }
        )
        + "\n"
    )

    assert judge_evaluation.apply_judgments(run) == (1, 0)
    score = EvaluationScore.model_validate_json((run / "scores.jsonl").read_text())
    assert score.answer_claim_recall == 0.5
    assert judge_evaluation.apply_judgments(run) == (1, 0)

    queue_item = json.loads((run / "judge-queue.jsonl").read_text())
    queue_item["answer"] = "A different answer."
    (run / "judge-queue.jsonl").write_text(json.dumps(queue_item) + "\n")
    with pytest.raises(ValueError, match="does not match queued answer"):
        judge_evaluation.apply_judgments(run)
    queue_item["answer"] = "Only the first claim is present."
    (run / "judge-queue.jsonl").write_text(json.dumps(queue_item) + "\n")

    judgment = json.loads((run / "claim-judgments.jsonl").read_text())
    judgment["claims"] = ["stale"]
    judgment["supported"] = [True]
    (run / "claim-judgments.jsonl").write_text(json.dumps(judgment) + "\n")
    with pytest.raises(ValueError, match="does not match queued content"):
        judge_evaluation.apply_judgments(run)


def test_claim_scoring_cannot_overwrite_concurrently_appended_score(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    item = execute_evaluation.ClaimJudgeItem(
        trial_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        claims=["supported"],
        answer="supported",
    )
    first_score = EvaluationScore(trial_id=item.trial_id)
    second_score = EvaluationScore(trial_id=uuid.uuid4())
    (run / "judge-queue.jsonl").write_text(item.model_dump_json() + "\n")
    (run / "scores.jsonl").write_text(first_score.model_dump_json() + "\n")
    (run / "claim-judgments.jsonl").write_text(
        ClaimJudgment(
            trial_id=item.trial_id,
            item_id=item.item_id,
            claims=item.claims,
            answer_sha256=item.answer_sha256,
            supported=[True],
            reviewer="reviewer",
            method="human",
            reviewed_at=datetime.now(UTC),
        ).model_dump_json()
        + "\n"
    )
    scoring_started = threading.Event()
    release_scoring = threading.Event()
    apply_unlocked = judge_evaluation._apply_judgments_unlocked  # pyright: ignore[reportPrivateUsage]

    def paused_apply(path: Path) -> tuple[int, int]:
        scoring_started.set()
        assert release_scoring.wait(timeout=2)
        return apply_unlocked(path)

    monkeypatch.setattr(judge_evaluation, "_apply_judgments_unlocked", paused_apply)

    with ThreadPoolExecutor(max_workers=2) as executor:
        scoring = executor.submit(judge_evaluation.apply_judgments, run)
        assert scoring_started.wait(timeout=2)
        appending = executor.submit(
            execute_evaluation.append_trial_result,
            run / "scores.jsonl",
            run / "judge-queue.jsonl",
            second_score,
            None,
        )
        try:
            assert not appending.done()
        finally:
            release_scoring.set()
        assert scoring.result(timeout=2) == (1, 0)
        appending.result(timeout=2)

    scores = [
        EvaluationScore.model_validate_json(line)
        for line in (run / "scores.jsonl").read_text().splitlines()
    ]
    assert [score.trial_id for score in scores] == [item.trial_id, second_score.trial_id]
    assert scores[0].answer_claim_recall == 1


def test_frozen_index_binding_rejects_drift_and_pins_exact_versions() -> None:
    document_id = uuid.uuid4()
    index_version_id = uuid.uuid4()
    entries = [
        (
            "2401.00001",
            document_id,
            index_version_id,
            "a" * 64,
            uuid.uuid4(),
        )
    ]
    digest = frozen_index_sha256([("2401.00001", "a" * 64)])

    documents, index_versions = execute_evaluation.bind_frozen_index(entries, digest)

    assert documents == {"2401.00001": document_id}
    assert index_versions == {document_id: index_version_id}
    options = execute_evaluation.options_for(
        "forced_route_end_to_end",
        6_400,
        (),
        tuple(index_versions.items()),
    )
    assert dict(options.index_version_ids) == index_versions
    with pytest.raises(SystemExit, match="active index"):
        execute_evaluation.bind_frozen_index(entries, "b" * 64)
    with pytest.raises(SystemExit, match="metadata snapshot"):
        execute_evaluation.bind_frozen_index(
            [("2401.00001", document_id, index_version_id, "a" * 64, None)],
            digest,
        )


def _frozen_input_fixture() -> tuple[
    Settings,
    FrozenEvaluationManifest,
    list[EvaluationTrial],
    list[Any],
    EvaluationCorpusManifest,
    Path,
]:
    dataset = API_ROOT / "evaluation/pageindex-v2-regression.jsonl"
    corpus = load_corpus_manifest(API_ROOT / "evaluation/pageindex-v2-corpus.json")
    cases = load_cases(dataset)
    settings = Settings(
        release="a" * 40,
        deepseek_input_cost_per_million_usd=0.14,
        deepseek_cache_hit_input_cost_per_million_usd=0.0028,
        deepseek_output_cost_per_million_usd=0.28,
    )
    trials = build_trials(cases, seed=7)
    manifest = FrozenEvaluationManifest(
        schema_version=2,
        experiment_id=uuid.uuid4(),
        release_sha=settings.release,
        configuration_sha256=evaluation_configuration_sha256(
            settings,
            execution_concurrency=4,
        ),
        corpus_sha256=corpus.corpus_snapshot_sha256,
        index_sha256="3" * 64,
        dataset_sha256=file_sha256(dataset),
        price_snapshot=evaluation_price_snapshot(
            settings,
            maximum_provider_attempts=MAX_PROVIDER_ATTEMPTS,
        ),
        repetitions=1,
        evidence_budgets=EVIDENCE_BUDGETS,
        execution_concurrency=4,
        seed=7,
        maximum_cost_usd=Decimal("25"),
        created_at=datetime.now(UTC),
    )
    return settings, manifest, trials, cases, corpus, dataset


def test_frozen_input_validation_rejects_each_identity_drift() -> None:
    settings, manifest, trials, cases, corpus, dataset = _frozen_input_fixture()
    dataset_sha256 = file_sha256(dataset)

    execute_evaluation.validate_frozen_inputs(
        settings,
        manifest,
        trials,
        cases,
        dataset_sha256=dataset_sha256,
        corpus=corpus,
        image_release=manifest.release_sha,
        execution_concurrency=manifest.execution_concurrency,
    )
    checks = (
        (
            "runtime RELEASE",
            settings.model_copy(update={"release": "b" * 40}),
            manifest,
            trials,
            dataset_sha256,
            corpus,
            manifest.release_sha,
        ),
        (
            "built image release",
            settings,
            manifest,
            trials,
            dataset_sha256,
            corpus,
            "b" * 40,
        ),
        (
            "dataset",
            settings,
            manifest,
            trials,
            "f" * 64,
            corpus,
            manifest.release_sha,
        ),
        (
            "corpus",
            settings,
            manifest.model_copy(update={"corpus_sha256": "f" * 64}),
            trials,
            dataset_sha256,
            corpus,
            manifest.release_sha,
        ),
        (
            "configuration",
            settings.model_copy(update={"deepseek_model": "other-model"}),
            manifest,
            trials,
            dataset_sha256,
            corpus,
            manifest.release_sha,
        ),
        (
            "prices",
            settings.model_copy(update={"deepseek_input_cost_per_million_usd": 1.0}),
            manifest,
            trials,
            dataset_sha256,
            corpus,
            manifest.release_sha,
        ),
        (
            "trials",
            settings,
            manifest,
            trials[:-1],
            dataset_sha256,
            corpus,
            manifest.release_sha,
        ),
    )
    for (
        message,
        candidate_settings,
        candidate_manifest,
        candidate_trials,
        candidate_dataset_sha256,
        candidate_corpus,
        candidate_image_release,
    ) in checks:
        with pytest.raises(SystemExit, match=message):
            execute_evaluation.validate_frozen_inputs(
                candidate_settings,
                candidate_manifest,
                candidate_trials,
                cases,
                dataset_sha256=candidate_dataset_sha256,
                corpus=candidate_corpus,
                image_release=candidate_image_release,
                execution_concurrency=manifest.execution_concurrency,
            )
    with pytest.raises(SystemExit, match="execution concurrency"):
        execute_evaluation.validate_frozen_inputs(
            settings,
            manifest,
            trials,
            cases,
            dataset_sha256=dataset_sha256,
            corpus=corpus,
            image_release=manifest.release_sha,
            execution_concurrency=3,
        )


def test_evaluation_corpus_snapshot_rejects_tampering_and_duplicates() -> None:
    corpus = load_corpus_manifest(API_ROOT / "evaluation/pageindex-v2-corpus.json")
    indexed = [(document.arxiv_id, document.pdf_sha256) for document in corpus.documents]
    validate_indexed_corpus(indexed, corpus)

    wrong_digest = [*indexed]
    wrong_digest[0] = (wrong_digest[0][0], "f" * 64)
    with pytest.raises(ValueError, match="digest_mismatch"):
        validate_indexed_corpus(wrong_digest, corpus)
    with pytest.raises(ValueError, match="missing"):
        validate_indexed_corpus(indexed[1:], corpus)

    value = corpus.model_dump(mode="json")
    value["documents"][0]["pdf_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="snapshot"):
        EvaluationCorpusManifest.model_validate(value)

    value = corpus.model_dump(mode="json")
    value["documents"].append(dict(value["documents"][0]))
    with pytest.raises(ValueError, match="repeats"):
        EvaluationCorpusManifest.model_validate(value)


class _RunnerMustNotRun:
    async def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("a persisted terminal run must not invoke the graph")


class _BudgetDeniedRunner:
    async def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ExperimentBudgetExceeded


class _CancelledRunner:
    async def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise asyncio.CancelledError


class _FailedRunner:
    async def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("unexpected execution failure")


class _RejectedSQLRunner:
    async def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise SQLRejected("short term must use explicit regex word boundaries")


class _RejectedSQLWithAuditsRunner:
    async def run(
        self,
        session: AsyncSession,
        request: object,
        run: QueryRun,
        **kwargs: object,
    ) -> None:
        del request, kwargs
        session.add_all(
            [
                SqlAudit(
                    run_id=run.id,
                    generated_sql=f"SELECT {attempt}",
                    allowed=False,
                    rejection_reason="short term must use explicit regex word boundaries",
                )
                for attempt in range(2)
            ]
        )
        await session.flush()
        raise SQLRejected("short term must use explicit regex word boundaries")


class _SuccessfulRunner:
    async def run(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        return {
            "route": RunRoute.documents.value,
            "answer": "answer",
            "citations": [],
            "insufficient": False,
        }


class _RecordingSession:
    def __init__(self, durable_run: QueryRun | None = None) -> None:
        self.durable_run = durable_run
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def get(
        self,
        model: type[QueryRun],
        identifier: uuid.UUID,
        *,
        populate_existing: bool = False,
    ) -> QueryRun | None:
        del populate_existing
        assert model is QueryRun
        if self.durable_run is None or self.durable_run.id != identifier:
            return None
        return self.durable_run


def _single_trial() -> tuple[Any, EvaluationTrial]:
    case = load_cases(API_ROOT / "evaluation/pageindex-v2-regression.jsonl")[0]
    trial = EvaluationTrial(
        trial_id=uuid.uuid4(),
        sequence_number=0,
        item_id=case.item_id,
        split=case.split,
        stratum=case.stratum,
        experiment_kind="forced_route_end_to_end",
        evidence_token_budget=6_400,
        repetition=1,
    )
    return case, trial


async def test_terminal_resume_reconstructs_score_without_graph_work(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    run = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="terminal-resume",
        api_key_id=uuid.uuid4(),
        route=RunRoute.documents,
        question=case.prompt,
        answer="persisted answer",
        citations=[],
        latency_ms=10,
    )

    async def persisted(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, False

    async def no_tokens(*args: object, **kwargs: object) -> tuple[int, int]:
        del args, kwargs
        return 0, 0

    monkeypatch.setattr(execute_evaluation, "create_trial_run", persisted)
    monkeypatch.setattr(execute_evaluation, "token_totals", no_tokens)
    executor = execute_evaluation.Executor(
        cast(Any, _RunnerMustNotRun()),
        {},
        Settings(),
        uuid.uuid4(),
        run.api_key_id,
        {},
    )

    score, _ = await executor.execute(cast(Any, object()), case, trial)

    assert score.query_run_id == run.id
    assert score.failure_code is None


async def test_nonterminal_resume_aborts_without_graph_work(monkeypatch: MonkeyPatch) -> None:
    case, trial = _single_trial()
    run = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="nonterminal-resume",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def persisted(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, False

    monkeypatch.setattr(execute_evaluation, "create_trial_run", persisted)
    executor = execute_evaluation.Executor(
        cast(Any, _RunnerMustNotRun()),
        {},
        Settings(),
        uuid.uuid4(),
        run.api_key_id,
        {},
    )

    with pytest.raises(SystemExit, match="nonterminal"):
        await executor.execute(cast(Any, object()), case, trial)


async def test_failed_resume_preserves_the_durable_evaluation_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    run = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="failed-resume",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
        error="authentication_failed",
    )

    async def persisted(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, False

    monkeypatch.setattr(execute_evaluation, "create_trial_run", persisted)
    executor = execute_evaluation.Executor(
        cast(Any, _RunnerMustNotRun()),
        {},
        Settings(),
        uuid.uuid4(),
        run.api_key_id,
        {},
    )

    score, _ = await executor.execute(cast(Any, object()), case, trial)

    assert score.failure_code == "authentication_failed"


async def test_denied_budget_terminalizes_with_recoverable_failure_code(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    run = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="budget-denied",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def created(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, True

    monkeypatch.setattr(execute_evaluation, "create_trial_run", created)
    session = _RecordingSession()
    executor = execute_evaluation.Executor(
        cast(Any, _BudgetDeniedRunner()),
        {},
        Settings(),
        uuid.uuid4(),
        run.api_key_id,
        {},
    )

    score, _ = await executor.execute(cast(Any, session), case, trial)

    assert score.failure_code == ExperimentBudgetExceeded.failure_code
    assert run.error == ExperimentBudgetExceeded.failure_code
    assert session.rollbacks == 1
    assert session.commits == 1


async def test_rejected_generated_sql_preserves_audits_and_terminalizes(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    run = QueryRun(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4().hex,
        thread_id="rejected-generated-sql",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def created(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, True

    monkeypatch.setattr(execute_evaluation, "create_trial_run", created)
    session = _RecordingSession()
    executor = execute_evaluation.Executor(
        cast(Any, _RejectedSQLRunner()),
        {},
        Settings(),
        uuid.uuid4(),
        run.api_key_id,
        {},
    )

    score, judge = await executor.execute(cast(Any, session), case, trial)

    assert score.failure_code == execute_evaluation.GENERATED_SQL_REJECTED_FAILURE_CODE
    assert score.included is False
    assert judge is None
    assert run.error == execute_evaluation.GENERATED_SQL_REJECTED_FAILURE_CODE
    assert run.failure_code == RunFailureCode.request_failed
    assert session.rollbacks == 0
    assert session.commits == 1


@pytest.mark.integration
@pytest.mark.skipif(not OWNER_URL, reason="PostgreSQL owner URL not set")
async def test_rejected_generated_sql_commits_audits_with_terminal_run(
    monkeypatch: MonkeyPatch,
) -> None:
    assert OWNER_URL is not None
    case, trial = _single_trial()
    engine = make_engine(OWNER_URL)
    api_key_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        sessions = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with sessions() as session:
                session.add(
                    ApiKey(
                        id=api_key_id,
                        name="rejected-sql-test",
                        prefix=f"eval-{api_key_id.hex[:8]}",
                        key_hash=hashlib.sha256(api_key_id.bytes).hexdigest(),
                        scopes=[],
                    )
                )
                session.add(
                    QueryRun(
                        id=run_id,
                        trace_id=uuid.uuid4().hex,
                        thread_id="rejected-generated-sql-integration",
                        api_key_id=api_key_id,
                        question=case.prompt,
                    )
                )
                await session.commit()

            async def created(
                current_session: AsyncSession,
                *args: object,
                **kwargs: object,
            ) -> tuple[QueryRun, bool]:
                del args, kwargs
                run = await current_session.get(QueryRun, run_id)
                assert run is not None
                return run, True

            monkeypatch.setattr(execute_evaluation, "create_trial_run", created)
            executor = execute_evaluation.Executor(
                cast(Any, _RejectedSQLWithAuditsRunner()),
                {},
                Settings(),
                uuid.uuid4(),
                api_key_id,
                {},
            )
            async with sessions() as session:
                score, judge = await executor.execute(session, case, trial)

            assert score.failure_code == execute_evaluation.GENERATED_SQL_REJECTED_FAILURE_CODE
            assert score.included is False
            assert judge is None
            async with sessions() as session:
                stored_run = await session.get(QueryRun, run_id)
                audits = list(
                    await session.scalars(
                        select(SqlAudit)
                        .where(SqlAudit.run_id == run_id)
                        .order_by(SqlAudit.generated_sql)
                    )
                )
                assert stored_run is not None
                assert stored_run.failure_code == RunFailureCode.request_failed
                assert stored_run.error == execute_evaluation.GENERATED_SQL_REJECTED_FAILURE_CODE
                assert [audit.generated_sql for audit in audits] == [
                    "SELECT 0",
                    "SELECT 1",
                ]
                assert all(
                    audit.rejection_reason == "short term must use explicit regex word boundaries"
                    for audit in audits
                )
        finally:
            await transaction.rollback()
    await engine.dispose()


async def test_cancelled_execution_terminalizes_before_propagating(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    experiment_id = uuid.uuid4()
    run = QueryRun(
        id=execute_evaluation.trial_run_id(experiment_id, trial.trial_id),
        trace_id=uuid.uuid4().hex,
        thread_id="cancelled-execution",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def created(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, True

    monkeypatch.setattr(execute_evaluation, "create_trial_run", created)
    session = _RecordingSession(run)
    executor = execute_evaluation.Executor(
        cast(Any, _CancelledRunner()),
        {},
        Settings(),
        experiment_id,
        run.api_key_id,
        {},
    )

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(cast(Any, session), case, trial)

    assert run.error == execute_evaluation.EXECUTION_CANCELLED_FAILURE_CODE
    assert run.failure_code == RunFailureCode.request_failed
    assert session.rollbacks == 1
    assert session.commits == 1


async def test_unexpected_execution_terminalizes_before_propagating(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    experiment_id = uuid.uuid4()
    run = QueryRun(
        id=execute_evaluation.trial_run_id(experiment_id, trial.trial_id),
        trace_id=uuid.uuid4().hex,
        thread_id="failed-execution",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def created(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, True

    monkeypatch.setattr(execute_evaluation, "create_trial_run", created)
    session = _RecordingSession(run)
    executor = execute_evaluation.Executor(
        cast(Any, _FailedRunner()),
        {},
        Settings(),
        experiment_id,
        run.api_key_id,
        {},
    )

    with pytest.raises(RuntimeError, match="unexpected execution failure"):
        await executor.execute(cast(Any, session), case, trial)

    assert run.error == execute_evaluation.EXECUTION_FAILED_FAILURE_CODE
    assert run.failure_code == RunFailureCode.request_failed
    assert session.rollbacks == 1
    assert session.commits == 1


async def test_malformed_case_fails_before_durable_trial_lifecycle(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    malformed = case.model_copy(update={"constraints": {"date_from": "not-a-date"}})

    async def must_not_create(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("malformed frozen input must not create a durable trial")

    monkeypatch.setattr(execute_evaluation, "create_trial_run", must_not_create)
    session = _RecordingSession()
    executor = execute_evaluation.Executor(
        cast(Any, _RunnerMustNotRun()),
        {},
        Settings(),
        uuid.uuid4(),
        uuid.uuid4(),
        {},
    )

    with pytest.raises(ValueError, match="date_from"):
        await executor.execute(cast(Any, session), malformed, trial)

    assert session.rollbacks == 0
    assert session.commits == 0


async def test_cancelled_trial_creation_recovers_a_committed_running_row(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    experiment_id = uuid.uuid4()
    run = QueryRun(
        id=execute_evaluation.trial_run_id(experiment_id, trial.trial_id),
        trace_id=uuid.uuid4().hex,
        thread_id="cancelled-creation",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def cancelled_creation(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(execute_evaluation, "create_trial_run", cancelled_creation)
    session = _RecordingSession(run)
    executor = execute_evaluation.Executor(
        cast(Any, _RunnerMustNotRun()),
        {},
        Settings(),
        experiment_id,
        run.api_key_id,
        {},
    )

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(cast(Any, session), case, trial)

    assert run.error == execute_evaluation.EXECUTION_CANCELLED_FAILURE_CODE
    assert run.failure_code == RunFailureCode.request_failed
    assert session.rollbacks == 1
    assert session.commits == 1


async def test_cancelled_success_finalization_recovers_the_running_row(
    monkeypatch: MonkeyPatch,
) -> None:
    case, trial = _single_trial()
    experiment_id = uuid.uuid4()
    run = QueryRun(
        id=execute_evaluation.trial_run_id(experiment_id, trial.trial_id),
        trace_id=uuid.uuid4().hex,
        thread_id="cancelled-finalize",
        api_key_id=uuid.uuid4(),
        question=case.prompt,
    )

    async def created(*args: object, **kwargs: object) -> tuple[QueryRun, bool]:
        del args, kwargs
        return run, True

    monkeypatch.setattr(execute_evaluation, "create_trial_run", created)
    session = _RecordingSession(run)
    executor = execute_evaluation.Executor(
        cast(Any, _SuccessfulRunner()),
        {},
        Settings(),
        experiment_id,
        run.api_key_id,
        {},
    )
    durable_finalize = executor.finalize

    async def cancel_success(
        current_session: AsyncSession,
        current_run: QueryRun,
        *,
        latency: int,
        result: Any = None,
        failure_code: RunFailureCode | None = None,
        evaluation_failure_code: str | None = None,
    ) -> None:
        if result is not None:
            raise asyncio.CancelledError
        await durable_finalize(
            current_session,
            current_run,
            latency=latency,
            failure_code=failure_code,
            evaluation_failure_code=evaluation_failure_code,
        )

    monkeypatch.setattr(executor, "finalize", cancel_success)

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(cast(Any, session), case, trial)

    assert run.error == execute_evaluation.EXECUTION_CANCELLED_FAILURE_CODE
    assert run.failure_code == RunFailureCode.request_failed
    assert session.rollbacks == 1
    assert session.commits == 1


@pytest.mark.parametrize("value", ["-1", "10001", "not-an-integer"])
def test_evaluation_trial_limit_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        execute_evaluation.trial_limit(value)


@pytest.mark.parametrize("value", ["0", "12", "10000"])
def test_evaluation_trial_limit_accepts_bounded_values(value: str) -> None:
    assert execute_evaluation.trial_limit(value) == int(value)


class _EmptySessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        del args


class _ConcurrentTestDatabase:
    def session(self) -> _EmptySessionContext:
        return _EmptySessionContext()


class _ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.executed: list[int] = []


class _ConcurrentTestExecutor:
    def __init__(
        self,
        tracker: _ConcurrencyTracker,
        delays: dict[int, float],
        *,
        budget_sequence: int | None = None,
    ) -> None:
        self.tracker = tracker
        self.delays = delays
        self.budget_sequence = budget_sequence

    async def execute(
        self,
        session: object,
        case: object,
        trial: EvaluationTrial,
    ) -> tuple[Any, None]:
        del session, case
        self.tracker.active += 1
        self.tracker.maximum = max(self.tracker.maximum, self.tracker.active)
        self.tracker.executed.append(trial.sequence_number)
        try:
            await asyncio.sleep(self.delays[trial.sequence_number])
        finally:
            self.tracker.active -= 1
        budget_denied = trial.sequence_number == self.budget_sequence
        return (
            execute_evaluation.EvaluationScore(
                trial_id=trial.trial_id,
                query_run_id=uuid.uuid4(),
                failure_code=(ExperimentBudgetExceeded.failure_code if budget_denied else None),
                included=not budget_denied,
            ),
            None,
        )


def _concurrent_trials(count: int) -> tuple[Any, list[EvaluationTrial]]:
    case, template = _single_trial()
    return (
        case,
        [
            template.model_copy(
                update={
                    "trial_id": uuid.uuid4(),
                    "sequence_number": sequence,
                }
            )
            for sequence in range(count)
        ],
    )


async def test_parallel_execution_is_bounded_and_writes_in_plan_order(tmp_path: Path) -> None:
    case, trials = _concurrent_trials(6)
    tracker = _ConcurrencyTracker()
    delays = {0: 0.04, 1: 0.005, 2: 0.01, 3: 0.005, 4: 0.005, 5: 0.005}
    executors = [_ConcurrentTestExecutor(tracker, delays) for _ in range(3)]
    scores = tmp_path / "scores.jsonl"
    judge = tmp_path / "judge.jsonl"
    scores.touch()

    exhausted = await execute_evaluation.execute_pending_trials(
        cast(Any, _ConcurrentTestDatabase()),
        cast(Any, executors),
        trials,
        {case.item_id: case},
        scores,
        judge,
    )

    written = [
        execute_evaluation.EvaluationScore.model_validate_json(line)
        for line in scores.read_text(encoding="utf-8").splitlines()
    ]
    assert exhausted is False
    assert tracker.maximum == 3
    assert [score.trial_id for score in written] == [trial.trial_id for trial in trials]
    assert sorted(tracker.executed) == list(range(6))


async def test_parallel_execution_stops_scheduling_after_budget_denial(
    tmp_path: Path,
) -> None:
    case, trials = _concurrent_trials(8)
    tracker = _ConcurrencyTracker()
    delays = {sequence: (0.001 if sequence == 2 else 0.03) for sequence in range(8)}
    executors = [_ConcurrentTestExecutor(tracker, delays, budget_sequence=2) for _ in range(3)]
    scores = tmp_path / "scores.jsonl"
    judge = tmp_path / "judge.jsonl"
    scores.touch()

    exhausted = await execute_evaluation.execute_pending_trials(
        cast(Any, _ConcurrentTestDatabase()),
        cast(Any, executors),
        trials,
        {case.item_id: case},
        scores,
        judge,
    )

    assert exhausted is True
    assert sorted(tracker.executed) == [0, 1, 2]
    assert len(scores.read_text(encoding="utf-8").splitlines()) == 3
