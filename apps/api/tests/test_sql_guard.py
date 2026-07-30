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


def test_qualified_relation_cannot_hide_behind_cte_name() -> None:
    with pytest.raises(SQLRejected, match="pg_catalog.pg_class"):
        validate_sql(
            "WITH pg_class AS (SELECT 1 FROM agent.paper_catalog_v1) "
            "SELECT * FROM pg_catalog.pg_class"
        )


@pytest.mark.parametrize(
    "sql",
    [
        "WITH unused AS (SELECT 1 FROM agent.paper_catalog_v1) "
        "SELECT count(*) FROM pg_class AS c "
        "WHERE EXISTS (WITH pg_class AS (SELECT 1) SELECT 1 FROM pg_class)",
        "WITH unused AS (SELECT 1 FROM agent.paper_catalog_v1) "
        "SELECT n.nspname, c.relname "
        "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' "
        "AND EXISTS (WITH pg_class AS (SELECT 1), pg_namespace AS (SELECT 1) "
        "SELECT 1 FROM pg_class, pg_namespace)",
    ],
)
def test_decoy_cte_in_inner_scope_cannot_hide_outer_catalog_reference(sql: str) -> None:
    with pytest.raises(SQLRejected, match="pg_class"):
        validate_sql(sql)


def test_benign_cte_reference_is_allowed() -> None:
    validated = validate_sql(
        "WITH recent AS (SELECT title FROM agent.paper_catalog_v1) SELECT title FROM recent"
    )

    assert "recent" in validated.normalized


def test_cte_can_match_approved_view_basename() -> None:
    validated = validate_sql(
        "WITH paper_catalog_v1 AS (SELECT title FROM agent.paper_catalog_v1) "
        "SELECT title FROM paper_catalog_v1"
    )

    assert "paper_catalog_v1" in validated.normalized


def test_curated_aggregate_functions_are_allowed() -> None:
    validated = validate_sql(
        "SELECT status, count(*) FROM agent.ingestion_summary_v1 GROUP BY status"
    )
    assert "COUNT(*)" in validated.normalized


def test_topic_aggregation_cte_is_allowed() -> None:
    validated = validate_sql(
        "WITH flattened AS (SELECT unnest(topics) AS topic "
        "FROM agent.paper_catalog_v1 "
        "WHERE topics IS NOT NULL AND array_length(topics, 1) > 0) "
        "SELECT topic, count(*) AS paper_count FROM flattened "
        "GROUP BY topic ORDER BY paper_count DESC"
    )

    assert "UNNEST(topics)" in validated.normalized
    assert "ARRAY_LENGTH(topics, 1)" in validated.normalized


def test_comma_join_unnest_is_allowed() -> None:
    validated = validate_sql(
        "SELECT topic, count(*) FROM agent.paper_catalog_v1, unnest(topics) AS topic GROUP BY topic"
    )

    assert "UNNEST(topics)" in validated.normalized


def test_catalog_aggregation_helpers_are_allowed() -> None:
    string_aggregation = validate_sql("SELECT string_agg(title, ', ') FROM agent.paper_catalog_v1")
    json_aggregation = validate_sql(
        "SELECT json_agg(json_build_object('title', title, 'topics', topics)) "
        "FROM agent.paper_catalog_v1"
    )

    assert "STRING_AGG(title, ', ')" in string_aggregation.normalized
    assert "JSON_AGG(JSON_BUILD_OBJECT" in json_aggregation.normalized


def test_concat_helpers_are_allowed() -> None:
    validated = validate_sql(
        "SELECT concat_ws(' ', title, arxiv_id) AS label, "
        "concat(title, ' [', arxiv_id, ']') AS tagged FROM agent.paper_catalog_v1"
    )

    assert "CONCAT_WS(' ', title, arxiv_id)" in validated.normalized
    assert "CONCAT(title, ' [', arxiv_id, ']')" in validated.normalized


def test_additional_catalog_functions_are_allowed() -> None:
    validated = validate_sql(
        "SELECT array_agg(length(title)), "
        "jsonb_agg(jsonb_build_object('title', title)) "
        "FROM agent.paper_catalog_v1"
    )

    assert "ARRAY_AGG(LENGTH(title))" in validated.normalized
    assert "JSONB_AGG(JSONB_BUILD_OBJECT" in validated.normalized


def test_schema_qualified_approved_function_is_rejected() -> None:
    with pytest.raises(SQLRejected, match="schema-qualified functions"):
        validate_sql("SELECT public.length(title) FROM agent.paper_catalog_v1")


def test_schema_qualified_dangerous_function_is_rejected() -> None:
    with pytest.raises(SQLRejected):
        validate_sql("SELECT pg_catalog.pg_sleep(1) FROM agent.paper_catalog_v1")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM agent.paper_catalog_v1, public.unnest(topics) AS t",
        "SELECT * FROM agent.paper_catalog_v1, public.count(*) AS t",
    ],
)
def test_schema_qualified_table_function_is_rejected(sql: str) -> None:
    with pytest.raises(SQLRejected, match="schema-qualified functions"):
        validate_sql(sql)


def test_unapproved_from_function_has_precise_rejection() -> None:
    with pytest.raises(SQLRejected, match="function is not approved: regexp_split_to_table"):
        validate_sql(
            "SELECT word FROM agent.paper_catalog_v1, regexp_split_to_table(abstract, 'x') AS word"
        )


def test_table_function_alone_does_not_satisfy_catalog_view_requirement() -> None:
    with pytest.raises(SQLRejected, match="query must read an approved catalog view"):
        validate_sql("SELECT * FROM unnest(ARRAY['a','b']) AS t")


def test_dangerous_from_function_is_rejected() -> None:
    with pytest.raises(SQLRejected, match="dangerous function is not allowed: pg_ls_dir"):
        validate_sql("SELECT * FROM agent.paper_catalog_v1, pg_ls_dir('.') AS f")


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
    ("sql", "expected"),
    [
        (
            r"SELECT count(*) FROM agent.paper_catalog_v1 "
            r"WHERE abstract ~* '(?im)^\mai\M$'",
            r"(?im)^\mai\M$",
        ),
        (
            r"SELECT count(*) FROM agent.paper_catalog_v1 "
            r"WHERE abstract ~* '***:(?im)^\mai\M$'",
            r"***:(?im)^\mai\M$",
        ),
    ],
)
def test_regex_option_prefix_is_not_treated_as_a_search_term(sql: str, expected: str) -> None:
    validated = validate_sql(sql)

    assert expected in validated.normalized


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?im)^ai$'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '***:(?im)^ai$'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?b)ai'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?e)ai'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?q)ai'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '***=ai'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?b)\mai\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?e)\mai\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?q)\mai\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '***=\mai\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?b)\\mai\\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?e)\\mai\\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '(?q)\\mai\\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '***=\\mai\\M'",
        r"SELECT count(*) FROM agent.paper_catalog_v1 WHERE abstract ~* '***:(?q)\\mai\\M'",
    ],
)
def test_regex_mode_prefix_does_not_hide_an_unbounded_search_term(sql: str) -> None:
    with pytest.raises(SQLRejected, match="short term 'ai'"):
        validate_sql(sql)


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
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* $$rag$$",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'rag'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '%rag%'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE E'%rag%'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\yrag\y'",
    ],
    ids=[
        "dollar-quote-unbounded",
        "e-string-unbounded",
        "similar-to-substring",
        "e-string-like-substring",
        "e-string-fake-boundary",
    ],
)
def test_short_terms_reject_across_e_string_dollar_quote_and_similar_to(sql: str) -> None:
    with pytest.raises(SQLRejected):
        validate_sql(sql)


def test_e_string_fake_boundary_is_reported_as_unverifiable() -> None:
    with pytest.raises(SQLRejected, match="escape-processed string literal cannot be verified"):
        validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\yrag\y'")


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\yrag\y'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* $$\yrag\y$$",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\mdistillation\M'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%machine learning%'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO 'rag'",
        r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\mmodel\M'",
    ],
    ids=[
        "plain-bounded",
        "dollar-quote-bounded",
        "plain-long-term",
        "like-long-phrase",
        "similar-to-exact",
        "count-bounded",
    ],
)
def test_bounded_and_long_term_predicates_survive_new_string_forms(sql: str) -> None:
    validated = validate_sql(sql)

    assert validated.normalized


@pytest.mark.parametrize(
    "sql",
    [
        # Bypass A -- National strings (N'...') are not escape-processed under
        # standard_conforming_strings=on, so they must run the normal short-term checks.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* N'rag'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE N'%rag%'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO N'%rag%'",
        # Bypass B -- E-string numeric / code-point escapes decode to a hidden short term
        # the AST text never shows (hex, octal, unicode; regex and LIKE positions).
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\x72\x61\x67'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\162\141\147'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\U00000072\U00000061\U00000067'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE E'%\x72\x61\x67%'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\mml\M'",
        # Bypass C -- SIMILAR TO metacharacters hide a short token from the LIKE %term% rule.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '%(rag)%'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '%_rag_%'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '(rag)%'",
    ],
    ids=[
        "national-regex",
        "national-ilike",
        "national-similar-to",
        "e-string-hex",
        "e-string-octal",
        "e-string-unicode",
        "e-string-like-hex",
        "e-string-boundary-short-token",
        "similar-to-parens",
        "similar-to-underscore",
        "similar-to-anchor",
    ],
)
def test_review_short_term_bypasses_are_rejected(sql: str) -> None:
    with pytest.raises(SQLRejected):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # National strings still allow bounded / long-term predicates.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* N'\yrag\y'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* N'machine learning'",
        # E-strings without a numeric escape stay verifiable through the escape scan; the
        # \m..\M boundaries glue onto the term as a bounded literal (mmachineM).
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'machine learning'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\mmachine\M'",
        # SIMILAR TO without a metacharacter is an anchored exact match; long tokens are safe
        # even with metacharacters.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO 'rag'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO 'machine learning'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '%machine learning%'",
        # \A \Z \Y E-string escapes are intentionally NOT special-cased: the security review
        # ruled the concern overstated because they glue onto the term as a bounded literal
        # (E'\Arag\Z' -> AragZ), so they must not be newly over-rejected.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\Arag\Z'",
    ],
    ids=[
        "national-bounded",
        "national-long-term",
        "e-string-long-phrase",
        "e-string-boundary-glued",
        "similar-to-exact",
        "similar-to-long-phrase",
        "similar-to-long-substring",
        "e-string-azy-non-bypass",
    ],
)
def test_review_safe_string_forms_survive(sql: str) -> None:
    validated = validate_sql(sql)

    assert validated.normalized


@pytest.mark.parametrize(
    "sql",
    [
        # HOLE 1 -- POSIX regex and SIMILAR TO decode \x / \o / \u / \U code-point escapes to
        # the character they name EVEN inside a verifiable literal (plain, National), so the
        # AST text shows only the escape while the engine runs the decoded short term. Each
        # executed as an over-broad match on the reader DB ('storage' ~* '\x72\x61\x67' -> rag,
        # 'a123b' ~ '\061\062\063' -> 123) before this guard.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\x72\x61\x67'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\061\062\063'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* N'\x72\x61\x67'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '%\x72\x61\x67%'",
        # Regression -- the E-string code-point form (unverifiable) stays rejected.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* E'\x72\x61\x67'",
        # HOLE 2 -- LIKE's own escape backslash drops before a letter and reveals it: the raw
        # literal carries a real backslash ('%r\ag%'), which PostgreSQL runs as %rag%.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%r\ag%'",
    ],
    ids=[
        "regex-plain-hex",
        "regex-plain-octal",
        "regex-national-hex",
        "similar-to-plain-hex",
        "regex-estring-hex-regression",
        "like-escape-backslash",
    ],
)
def test_escape_decode_short_term_holes_are_rejected(sql: str) -> None:
    with pytest.raises(SQLRejected):
        validate_sql(sql)


def test_regex_codepoint_escape_reports_code_point_message() -> None:
    with pytest.raises(SQLRejected, match="code-point escape"):
        validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\x72\x61\x67'")


def test_like_escape_backslash_reports_backslash_message() -> None:
    with pytest.raises(SQLRejected, match="escape backslash"):
        validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%r\ag%'")


@pytest.mark.parametrize(
    "sql",
    [
        # The model's real short-term predicates use \m \M \y word boundaries and \s / [-\s]
        # classes with spelled-out words -- none match ESCAPE_CODEPOINT, so none regress.
        r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\mdistillation\M'",
        r"SELECT COUNT(*) FROM agent.paper_catalog_v1 "
        r"WHERE abstract ~* '\yretrieval[-\s]augmented[-\s]generation\y'",
        r"SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title ~* '\ymodel\y'",
        "SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE abstract ILIKE '%machine learning%'",
        "SELECT COUNT(*) FROM agent.paper_catalog_v1 WHERE title SIMILAR TO 'machine learning'",
    ],
    ids=[
        "regex-boundary-distillation",
        "regex-boundary-class-rag",
        "regex-boundary-model",
        "like-long-phrase",
        "similar-to-exact-phrase",
    ],
)
def test_real_model_patterns_survive_escape_decode_guard(sql: str) -> None:
    validated = validate_sql(sql)

    assert validated.normalized


def test_character_class_short_term_is_accepted_residual() -> None:
    # accepted residual -- see guard comment in _reject_unsafe_short_term_matches. Character-
    # class obfuscation ('[r][a][g]' runs as `rag`) is a known, deliberately unclosed limit of
    # the static check; if a future change closes it, this assertion visibly flips to a reject.
    validated = validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '[r][a][g]'")

    assert validated.normalized


@pytest.mark.parametrize(
    "sql",
    [
        # FINDING A -- a LIKE/ILIKE/SIMILAR TO ESCAPE clause consumes its escape character at
        # execution ('storage' ILIKE '%Xrag%' ESCAPE 'X' runs as %rag% -> TRUE) while the guard
        # only ever sees the inner token 'Xrag'. exp.Escape wraps the predicate, so reject it.
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%Xrag%' ESCAPE 'X'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title LIKE '%rag%' ESCAPE '#'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO '%Xrag%' ESCAPE 'X'",
    ],
    ids=["ilike-escape", "like-escape", "similar-to-escape"],
)
def test_like_similar_to_escape_clause_is_rejected(sql: str) -> None:
    with pytest.raises(SQLRejected, match="ESCAPE clause is not allowed"):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # FINDING B -- a computed / non-literal RIGHT-HAND pattern operand skips every short-term
        # scan (match_pattern_text returns None), so a function call or || concatenation rebuilds
        # a short term the guard never sees. Each executed as an over-broad match on the reader DB
        # ('storage' ~* lower('RAG') -> rag -> TRUE) before this guard.
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* lower('RAG')",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* concat('r','a','g')",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%' || 'rag' || '%'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* ('ra'||'g')",
        "SELECT 1 FROM agent.paper_catalog_v1 "
        "WHERE title ~* array_to_string(ARRAY['r','a','g'],'')",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO lower('%RAG%')",
    ],
    ids=[
        "regex-lower",
        "regex-concat",
        "ilike-dpipe",
        "regex-paren-dpipe",
        "regex-array-to-string",
        "similar-to-lower",
    ],
)
def test_computed_match_patterns_are_rejected(sql: str) -> None:
    with pytest.raises(SQLRejected, match="computed or concatenated match patterns"):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # RHS-only scoping: a computed LHS with a literal RHS is legitimate and must stay green.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE lower(title) ~* '\mrag\M'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE array_to_string(topics,' ') ~* '\yrag\y'",
        # Plain literal RHS predicates across the three match forms are untouched by FINDING B.
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* '\mdistillation\M'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%machine learning%'",
        "SELECT 1 FROM agent.paper_catalog_v1 WHERE title SIMILAR TO 'machine learning'",
    ],
    ids=[
        "computed-lhs-literal-rhs-lower",
        "computed-lhs-literal-rhs-array-to-string",
        "regex-literal-long-term",
        "ilike-literal-phrase",
        "similar-to-literal-phrase",
    ],
)
def test_computed_lhs_with_literal_rhs_still_passes(sql: str) -> None:
    validated = validate_sql(sql)

    assert validated.normalized


def test_non_ascii_short_term_is_accepted_residual() -> None:
    # accepted residual -- see guard comment in _reject_unsafe_short_term_matches. A non-ASCII
    # short term passes the regex word-boundary scan because SHORT_REGEX_TERM is ASCII-only
    # ([A-Za-z]{2,3}) and 'ραγ' leaves no [A-Za-z] run. English arXiv metadata never contains it
    # and the LLM never emits it, so this over-match-only gap is accepted; if a future change
    # closes it, this assertion visibly flips to a reject.
    validated = validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ~* 'ραγ'")

    assert validated.normalized


def test_non_ascii_short_term_under_unbounded_substring_is_still_rejected() -> None:
    # The unbounded-substring guard is Unicode-aware (str.isalnum() is true for these letters), so
    # a non-ASCII short term under an unbounded %...% match is still rejected -- only the regex /
    # SIMILAR TO word-boundary path is ASCII-scoped (see the accepted-residual note).
    with pytest.raises(SQLRejected, match="cannot use an unbounded substring match"):
        validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE '%ραγ%'")


def test_estring_like_control_char_escape_is_rejected() -> None:
    # sqlglot decodes the E-string escape \a to a 0x07 bell while PostgreSQL yields the literal
    # letter 'a', so ILIKE E'%r\ag%' executes as %rag% (over-match) though the decoded AST text
    # the guard reads ('%r' + 0x07 + 'g%') shows no short token, no backslash, no code-point
    # escape. The control character is the tell for that sqlglot-vs-PostgreSQL divergence.
    with pytest.raises(SQLRejected, match="non-printable character"):
        validate_sql(r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE E'%r\ag%'")


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE E'machine learning'",
        r"SELECT 1 FROM agent.paper_catalog_v1 WHERE title ILIKE E'%machine learning%'",
    ],
    ids=["estring-exact", "estring-substring"],
)
def test_clean_estring_like_long_phrase_survives(sql: str) -> None:
    validated = validate_sql(sql)

    assert validated.normalized


def test_unterminated_string_is_rejected_as_invalid_syntax() -> None:
    with pytest.raises(SQLRejected, match="invalid PostgreSQL syntax"):
        validate_sql("SELECT title FROM agent.paper_catalog_v1 WHERE title = 'a")


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
