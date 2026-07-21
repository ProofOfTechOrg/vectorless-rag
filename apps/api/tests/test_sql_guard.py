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


def test_boolean_regex_cast_and_date_predicates_are_allowed() -> None:
    validated = validate_sql(
        "SELECT date_trunc('day', created_at), count(*) "
        "FROM agent.paper_catalog_v1 "
        "WHERE (COALESCE(title, '') || ' ' || topics::text) ~* '\\mRAG\\M' "
        "OR abstract ILIKE '%retrieval-augmented generation%' "
        "GROUP BY date_trunc('day', created_at)"
    )

    assert "COUNT(*)" in validated.normalized
    assert "~*" in validated.normalized
    assert " OR " in validated.normalized
    assert "CAST(topics AS TEXT)" in validated.normalized


def test_array_search_helpers_and_exists_predicates_are_allowed() -> None:
    validated = validate_sql(
        "SELECT count(*) FROM agent.paper_catalog_v1 AS paper "
        "WHERE array_to_string(topics, ' ') ~* '\\mRAG\\M' "
        "OR EXISTS ("
        "SELECT 1 FROM UNNEST(paper.authors) AS author "
        "WHERE author ~* '\\mAI\\M'"
        ")"
    )

    assert "ARRAY_TO_STRING" in validated.normalized
    assert "EXISTS" in validated.normalized
    assert "UNNEST" in validated.normalized


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract LIKE '%rag%'",
        "SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ILIKE '%rag%'",
    ],
)
def test_short_terms_cannot_use_unbounded_substring_matching(sql: str) -> None:
    with pytest.raises(SQLRejected, match="cannot use an unbounded substring match"):
        validate_sql(sql)


def test_short_regex_terms_require_explicit_word_boundaries() -> None:
    with pytest.raises(SQLRejected, match="must use explicit regex word boundaries"):
        validate_sql(
            "SELECT count(*) FROM agent.paper_catalog_v1 "
            "WHERE abstract ~* 'rag|Retrieval-Augmented Generation'"
        )


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT count(*) FROM agent.paper_catalog_v1 "
        r"WHERE abstract ~* '\mRAG\M|\mRetrieval-Augmented Generation\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 "
        r"WHERE abstract ~* '\m(RAG|Retrieval-Augmented Generation)\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 "
        r"WHERE abstract ~* '\yRAG\y|\yRetrieval-Augmented Generation\y'",
    ],
)
def test_short_regex_terms_accept_postgres_word_boundaries(sql: str) -> None:
    validated = validate_sql(sql)

    assert "~*" in validated.normalized


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
