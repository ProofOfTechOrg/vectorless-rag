import pytest

from vectorless_rag.sql_guard import SQLRejected, validate_sql


def test_approved_select_gets_bounded_limit() -> None:
    validated = validate_sql("SELECT document_id, title FROM agent.paper_catalog_v1")
    assert "LIMIT 200" in validated.normalized


def test_large_limit_is_reduced() -> None:
    validated = validate_sql("SELECT * FROM agent.ingestion_summary_v1 LIMIT 999")
    assert "LIMIT 200" in validated.normalized
    assert "999" not in validated.normalized


def test_cte_over_approved_view_is_allowed() -> None:
    validated = validate_sql(
        "WITH papers AS (SELECT document_id FROM agent.paper_catalog_v1) SELECT * FROM papers"
    )
    assert "papers" in validated.normalized


def test_curated_aggregate_functions_are_allowed() -> None:
    validated = validate_sql(
        "SELECT status, count(*) FROM agent.ingestion_summary_v1 GROUP BY status"
    )
    assert "COUNT(*)" in validated.normalized


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM agent.paper_catalog_v1",
        "DROP TABLE agent.documents",
        "SELECT * FROM agent.documents",
        "SELECT * FROM public.paper_catalog_v1",
        "SELECT * FROM agent.paper_catalog_v1; SELECT 1",
        "SELECT * FROM agent.paper_catalog_v1 -- hide",
        "SELECT pg_sleep(10) FROM agent.paper_catalog_v1",
        "SELECT repeat(title, 1000) FROM agent.paper_catalog_v1",
        "SELECT * INTO temp x FROM agent.paper_catalog_v1",
        "SELECT * FROM agent.paper_catalog_v1 FOR UPDATE",
        "SELECT * FROM agent.paper_catalog_v1 LIMIT (SELECT 1)",
    ],
)
def test_sql_attacks_are_rejected(sql: str) -> None:
    with pytest.raises(SQLRejected):
        validate_sql(sql)
