from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import sqlglot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import build_scope

from vectorless_rag.config import Settings

APPROVED_RELATIONS = frozenset({"agent.paper_catalog_v1", "agent.ingestion_summary_v1"})
DANGEROUS_FUNCTIONS = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_sleep",
        "dblink",
        "dblink_exec",
        "lo_import",
        "lo_export",
        "set_config",
        "current_setting",
    }
)
APPROVED_FUNCTIONS = frozenset(
    {
        "array_agg",
        "array_length",
        "array_to_string",
        "avg",
        "cardinality",
        "coalesce",
        "concat",
        "concat_ws",
        "count",
        "date_trunc",
        "json_agg",
        "json_build_object",
        "jsonb_agg",
        "jsonb_build_object",
        "length",
        "lower",
        "max",
        "min",
        "string_agg",
        "sum",
        "unnest",
        "upper",
    }
)
FUNCTION_NAME_ALIASES = {
    "arrayagg": "array_agg",
    "arraysize": "array_length",
    "arraytostring": "array_to_string",
    "concatws": "concat_ws",
    "explode": "unnest",
    "groupconcat": "string_agg",
    "jsonarrayagg": "json_agg",
    "timestamptrunc": "date_trunc",
}
SHORT_REGEX_TERM = re.compile(r"(?<![A-Za-z\\])([A-Za-z]{2,3})(?![A-Za-z])")
POSTGRES_REGEX_OPTION_PREFIX = re.compile(r"^(?:\*\*\*:)?\(\?([bceimnpqstwx]+)\)")
POSTGRES_NON_ARE_REGEX_OPTIONS = frozenset({"b", "e", "q"})
# Numeric / code-point escapes -- \xHH (hex), \ooo (octal), \uHHHH, \UHHHHHHHH -- that decode
# to a character the AST text never shows. TWO independent decoders reach them: PostgreSQL's
# E-string escape processor (any E'...' literal, verifiable=False), and -- separately -- the
# POSIX-regex (~, ~*) and SIMILAR TO pattern engine, which decodes them even inside a
# verifiable literal ('storage' ~* '\x72\x61\x67' is TRUE: hex -> rag). LIKE does NOT decode
# them ('storage' ILIKE '\x72\x61\x67' is FALSE), so the LIKE branch is intentionally exempt.
ESCAPE_CODEPOINT = re.compile(r"\\(x[0-9A-Fa-f]|[0-7]|u|U)")
# SIMILAR TO metacharacters. Unlike LIKE (where only % and _ are wildcards), SIMILAR TO
# also reads | * + ( ) [ ] ? { } as regex operators, so a short token beside any of them
# is an unbounded over-match rather than a literal substring.
SIMILAR_TO_METACHARS = frozenset("%_|*+()[]?{}")
# Control characters (C0 range plus DEL). sqlglot decodes an E-string's C-style letter escape
# to its control byte (E'\a' -> 0x07 bell) whereas PostgreSQL leaves \a as the literal letter
# 'a', so a decoded control character in an unverifiable LIKE pattern marks an escape whose
# sqlglot text diverges from what PostgreSQL runs and can hide a short term (E'%r\ag%' is
# %r<0x07>g% to the guard but runs as %rag%). Catalog LIKE patterns never contain one.
CONTROL_CHAR = re.compile(r"[\x00-\x1f\x7f]")


class SQLRejected(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedSQL:
    original: str
    normalized: str


@dataclass(frozen=True)
class SQLResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    explain_cost: float
    payload_bytes: int
    result_hash: str


class DatabaseAdapter(Protocol):
    async def execute(self, query: ValidatedSQL) -> SQLResult: ...


def _relation_name(table: exp.Table) -> str:
    catalog = table.catalog
    database = table.db
    parts = [part for part in (catalog, database, table.name) if part]
    return ".".join(parts)


def _callable_name(function: exp.Func) -> str | None:
    if isinstance(function, (exp.Predicate, exp.Binary, exp.Cast, exp.TryCast, exp.Array)):
        return None
    if isinstance(function, exp.Anonymous):
        return function.name.lower()
    return FUNCTION_NAME_ALIASES.get(function.key, function.key)


def _scope_relations(statement: exp.Query) -> set[str]:
    # Scope-aware relation resolution: a table reference counts as a CTE (and is
    # excluded from the allowlist) only when a CTE of that name is visible in its
    # own or an enclosing scope, so a decoy CTE in a non-covering inner scope can
    # no longer hide an outer reference to a real relation. Fail closed: any query
    # whose scope tree cannot be built is rejected rather than reaching the reader.
    try:
        root = build_scope(statement)
    except Exception as exc:
        raise SQLRejected("query scope could not be analyzed") from exc
    if root is None:
        raise SQLRejected("query scope could not be analyzed")
    relations: set[str] = set()
    for scope in root.traverse():
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and not isinstance(source.this, exp.Func):
                relations.add(_relation_name(source))
    return relations


def match_pattern_text(node: exp.Expr | None) -> tuple[str, bool] | None:
    # (text, verifiable). standard_conforming_strings=on, so the three text-string literal
    # forms that PostgreSQL does NOT escape-process store their exact executed text and are
    # verifiable=True: a plain Literal, a dollar-quoted RawString, and a National string
    # (N'rag' executes as rag; N'\yrag\y' executes as \yrag\y). Without National here the
    # N'...' form would return None and skip every short-term check.
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this), True
    if isinstance(node, exp.RawString):
        return str(node.this), True
    if isinstance(node, exp.National):
        return str(node.this), True
    # Escape-processed text-string literals: the AST text is not the executed text, so a
    # character can be hidden past a static scan -> verifiable=False. ByteString is the
    # E'...' form. UnicodeString (U&'...') is unreachable here in sqlglot 30.12.0's postgres
    # dialect -- U&'...' parses as a BitwiseAnd (U & '...') whose normalized text does not
    # decode -- but it is classified defensively so an escape-processed text literal can
    # never be silently skipped.
    if isinstance(node, (exp.ByteString, exp.UnicodeString)):
        return str(node.this), False
    # Everything else is skipped (None): non-string operands, plus the bit-string literals
    # HexString (X'...') and BitString (B'...'), which cannot be a text pattern at all --
    # `text ~* bit` raises "operator does not exist" on the reader, so they never match.
    return None


def postgres_regex_supports_word_boundaries(value: str) -> bool:
    if value.startswith("***="):
        return False
    option_prefix = POSTGRES_REGEX_OPTION_PREFIX.match(value)
    return option_prefix is None or POSTGRES_NON_ARE_REGEX_OPTIONS.isdisjoint(
        option_prefix.group(1)
    )


def _escaped_short_term(content: str) -> str | None:
    # An escape-processed string cannot be trusted to bound a short term: regex
    # word-boundary escapes (\m, \M, \y) dissolve into bare letters, hiding an interior
    # short token from a direct scan (E'\yrag\y' executes as yragy). Probe the raw text and
    # the text with those boundary escapes removed, and treat any short standalone token
    # found either way as unverifiable.
    for candidate in (content, re.sub(r"\\+[mMy]", "", content)):
        match = SHORT_REGEX_TERM.search(candidate)
        if match is not None:
            return match.group(1)
    return None


def _reject_unverifiable_pattern(content: str) -> None:
    # A non-verifiable (escape-processed) literal's AST text is not what PostgreSQL runs, so
    # a short term can hide two ways: as a directly visible short token (raw text, or with
    # \m \M \y boundary escapes stripped -- see _escaped_short_term), or behind a numeric /
    # code-point escape that decodes to a hidden character (E'\x72\x61\x67' -> rag).
    unsafe = _escaped_short_term(content)
    if unsafe is not None:
        raise SQLRejected(
            f"short term {unsafe!r} in an escape-processed string literal cannot be "
            "verified; use a plain quoted whole-token predicate"
        )
    if ESCAPE_CODEPOINT.search(content):
        raise SQLRejected(
            "escape-encoded pattern cannot be statically verified for short terms; "
            "use a plain quoted whole-token predicate"
        )


def _reject_unbounded_substring(value: str) -> None:
    # A %<token>% pattern is an unbounded substring match wherever % is a wildcard (both
    # LIKE and SIMILAR TO); a short whole-alnum token there matches far too broadly.
    token = value[1:-1] if value.startswith("%") and value.endswith("%") else ""
    if token.isalnum() and len(token) <= 3:
        raise SQLRejected(
            f"short term {token!r} cannot use an unbounded substring match; "
            "use a whole-token predicate"
        )


def _reject_codepoint_escape(value: str) -> None:
    # POSIX regex (~, ~*) and SIMILAR TO feed the pattern to a regex engine that decodes
    # numeric / code-point escapes to the character they name EVEN in a verifiable literal
    # whose AST text shows only the escape ('storage' ~* '\x72\x61\x67' is TRUE, hex -> rag;
    # octal, \u and \U decode the same way). The escaped digits leave no >=2-letter run for
    # SHORT_REGEX_TERM, so the short term hides unless the escape itself is rejected. This runs
    # up-front for BOTH verifiability classes: a verifiable literal is otherwise unguarded here
    # (its normal short-term scan never sees the decoded letters), and an unverifiable literal
    # would otherwise reach _reject_unverifiable_pattern -- checking here first only yields the
    # clearer message. Must NOT be applied to LIKE, which does not decode these escapes.
    if ESCAPE_CODEPOINT.search(value):
        raise SQLRejected(
            "regex/SIMILAR TO pattern uses a code-point escape (\\x, \\o, \\u, \\U) "
            "that can hide a short term; use plain letters with word boundaries"
        )


def _reject_computed_match_pattern(operand: exp.Condition | None) -> None:
    # Reject a match predicate whose RIGHT-HAND pattern operand is a function call or a ||
    # concatenation. Such an operand is computed at run time, never a static string literal,
    # so match_pattern_text returns None and every short-term scan above is skipped:
    # lower('RAG'), concat('r','a','g'), array_to_string(ARRAY[...], '') and '%' || 'rag' || '%'
    # each rebuild a short term the guard can no longer see. RHS-only by construction -- a
    # computed LHS with a literal RHS (lower(title) ~* '\mrag\M') keeps a literal here and stays
    # legitimate, and a bare column RHS (title ~* abstract) is not a computed short pattern.
    # Parentheses are unwrapped first so ('ra' || 'g') is recognized as a DPipe.
    if operand is None:
        return
    unwrapped = operand.unnest()
    if isinstance(unwrapped, (exp.Func, exp.DPipe)):
        raise SQLRejected(
            "computed or concatenated match patterns are not allowed; "
            "use a plain quoted literal predicate"
        )


def _reject_unsafe_short_term_matches(statement: exp.Query) -> None:
    # LIKE / ILIKE treat only % and _ as wildcards, so the plain short-term risk is an
    # unbounded %<short>% substring; the rest of the pattern is literal text. LIKE does NOT
    # decode \x / \o / \u / \U code-point escapes (unlike the regex engines below), so those
    # are not a concern here. Two escape mechanisms still hide a letter: (1) LIKE runs its OWN
    # escape pass with backslash as the escape character, so a backslash before a letter is
    # dropped and reveals it ('%r\ag%' runs as %rag% while a static scan sees only 'r\ag'); and
    # (2) in an E-string, sqlglot decodes a C-style letter escape to a control byte (E'%r\ag%'
    # -> %r<0x07>g%) while PostgreSQL yields the literal letter (%rag%), so the text the guard
    # reads diverges from what runs. Catalog LIKE patterns never need a backslash or a control
    # character, so reject either: the backslash check for every pattern, and the
    # control-character check for the unverifiable (E-string) branch, whose decoded text is the
    # only place a sqlglot-vs-PostgreSQL escape divergence can appear.
    for predicate in statement.find_all(exp.Like, exp.ILike):
        extracted = match_pattern_text(predicate.expression)
        if extracted is None:
            _reject_computed_match_pattern(predicate.expression)
            continue
        value, verifiable = extracted
        _reject_unbounded_substring(value)
        if not verifiable:
            _reject_unverifiable_pattern(value)
            if CONTROL_CHAR.search(value):
                raise SQLRejected(
                    "LIKE pattern contains a non-printable character that can hide a "
                    "short term; use a plain %phrase% predicate"
                )
        if "\\" in value:
            raise SQLRejected(
                "LIKE pattern contains an escape backslash that can hide a short term; "
                "use a plain %phrase% predicate"
            )
    # SIMILAR TO diverges from LIKE: | * + ( ) [ ] ? { } are regex metacharacters, so the
    # LIKE %term% extraction misses e.g. '%(rag)%' or '(rag)%'. The plain %<short>% guard
    # still holds (it is unbounded under SIMILAR TO too); on top of it, any metacharacter
    # beside a short token widens the match. A metacharacter-free SIMILAR TO pattern is an
    # anchored exact match and is safe ('rag' -> pass). Its regex engine also decodes
    # code-point escapes, so reject those up-front (before verifiability, for both classes).
    for predicate in statement.find_all(exp.SimilarTo):
        extracted = match_pattern_text(predicate.expression)
        if extracted is None:
            _reject_computed_match_pattern(predicate.expression)
            continue
        value, verifiable = extracted
        _reject_codepoint_escape(value)
        if not verifiable:
            _reject_unverifiable_pattern(value)
            continue
        _reject_unbounded_substring(value)
        if any(char in SIMILAR_TO_METACHARS for char in value):
            short = SHORT_REGEX_TERM.search(value)
            if short is not None:
                raise SQLRejected(
                    f"short term {short.group(1)!r} in a SIMILAR TO pattern with "
                    "metacharacters cannot use an unbounded match; use a whole-token predicate"
                )
    # POSIX regex (~, ~*): a short term must carry explicit \m..\M or \y..\y word
    # boundaries. \A \Z \Y E-string escapes are intentionally NOT special-cased: the
    # security review ruled that concern OVERSTATED -- under E-string processing they
    # dissolve to the letters A/Z/Y and glue onto the term (E'\Arag\Z' -> AragZ), a bounded
    # narrow literal that cannot widen the match, so stripping [mMy] must not grow to
    # [mMyAZ] (it would newly over-reject safe queries without helping any real bypass).
    #
    # ACCEPTED RESIDUAL -- character-class obfuscation of a short term (e.g. '[r][a][g]', or
    # '[rR][aA][gG]', which the engine runs as `rag`) leaves no >=2-letter run for
    # SHORT_REGEX_TERM and so passes this static check. It is deliberately NOT closed: fully
    # detecting it needs a real regex analyzer, and the model legitimately uses character
    # classes (e.g. '[-\s]'), so a blanket class ban would over-reject real queries. This is a
    # quality guard, not a security boundary -- the worst case is an over-broad match over
    # public catalog metadata, and the LLM that authors these queries never emits such
    # obfuscation -- so the residual is accepted rather than fixed. (A test pins the current
    # pass so a future change that closes it visibly flips.)
    #
    # ACCEPTED RESIDUAL -- non-ASCII short terms. SHORT_REGEX_TERM is ASCII-only ([A-Za-z]{2,3}),
    # so a short token written in non-ASCII letters (~* 'ραγ', or fullwidth ~* 'ｒａｇ') leaves no
    # [A-Za-z] run and passes this regex / SIMILAR TO word-boundary scan. English arXiv titles
    # and abstracts never contain such terms and the LLM never emits them, so -- like the
    # character-class case -- this is an over-match-only gap on public metadata and is accepted,
    # not fixed. The unbounded-substring guard IS Unicode-aware (str.isalnum() is true for these
    # letters), so an ILIKE '%ραγ%' short term is still rejected; only this word-boundary path is
    # ASCII-scoped. (A test pins the current pass so a future closure visibly flips.)
    for predicate in statement.find_all(exp.RegexpLike, exp.RegexpILike):
        extracted = match_pattern_text(predicate.expression)
        if extracted is None:
            _reject_computed_match_pattern(predicate.expression)
            continue
        value, verifiable = extracted
        _reject_codepoint_escape(value)
        if not verifiable:
            _reject_unverifiable_pattern(value)
            continue
        option_prefix = POSTGRES_REGEX_OPTION_PREFIX.match(value)
        if not postgres_regex_supports_word_boundaries(value):
            pattern_body = (
                value[option_prefix.end() :]
                if option_prefix is not None
                else value.removeprefix("***=")
            )
            unsafe = _escaped_short_term(pattern_body)
            if unsafe is not None:
                raise SQLRejected(
                    f"short term {unsafe!r} cannot use ARE word boundaries in this regex mode"
                )
            continue
        for match in SHORT_REGEX_TERM.finditer(value):
            if option_prefix is not None and match.start() < option_prefix.end():
                continue
            prefix = value[: match.start()]
            suffix = value[match.end() :]
            left_bounded = r"\m" in prefix or r"\y" in prefix
            right_bounded = r"\M" in suffix or r"\y" in suffix
            if not left_bounded or not right_bounded:
                raise SQLRejected(
                    f"short term {match.group(1)!r} must use explicit regex word boundaries"
                )


def validate_sql(sql: str, maximum_limit: int = 200) -> ValidatedSQL:
    stripped = sql.strip()
    if not stripped:
        raise SQLRejected("empty SQL")
    if "--" in stripped or "/*" in stripped or "*/" in stripped:
        raise SQLRejected("SQL comments are not allowed")
    try:
        statements = sqlglot.parse(stripped, read="postgres")
    except SqlglotError as exc:
        raise SQLRejected(f"invalid PostgreSQL syntax: {exc}") from exc
    if len(statements) != 1:
        raise SQLRejected("exactly one SQL statement is required")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise SQLRejected("only SELECT or CTE queries are allowed")
    forbidden_nodes = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Command,
        exp.Copy,
        exp.Merge,
        exp.Transaction,
    )
    for node_type in forbidden_nodes:
        if statement.find(node_type) is not None:
            raise SQLRejected(f"forbidden SQL operation: {node_type.__name__}")
    if statement.find(exp.Into) is not None:
        raise SQLRejected("SELECT INTO is not allowed")
    if statement.args.get("locks"):
        raise SQLRejected("row locking is not allowed")
    if statement.find(exp.Escape) is not None:
        raise SQLRejected(
            "LIKE/SIMILAR TO ESCAPE clause is not allowed; use a plain %phrase% predicate"
        )
    for dot in statement.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            qualified_name = f"{dot.this.sql(dialect='postgres')}.{dot.expression.name}"
            raise SQLRejected(f"schema-qualified functions are not allowed: {qualified_name}")
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Func):
            continue
        name = _callable_name(table.this)
        if table.db or table.catalog:
            schema = ".".join(part for part in (table.catalog, table.db) if part)
            raise SQLRejected(
                f"schema-qualified functions are not allowed: {schema}.{name or table.this.key}"
            )
        if name in DANGEROUS_FUNCTIONS:
            raise SQLRejected(f"dangerous function is not allowed: {name}")
        if name not in APPROVED_FUNCTIONS:
            raise SQLRejected(f"function is not approved: {name}")
    relations = _scope_relations(statement)
    if not relations:
        raise SQLRejected("query must read an approved catalog view")
    unapproved = relations - APPROVED_RELATIONS
    if unapproved:
        raise SQLRejected(f"unapproved relation(s): {', '.join(sorted(unapproved))}")
    for function in statement.find_all(exp.Func):
        name = _callable_name(function)
        if name is None:
            continue
        if name in DANGEROUS_FUNCTIONS:
            raise SQLRejected(f"dangerous function is not allowed: {name}")
        if name not in APPROVED_FUNCTIONS:
            raise SQLRejected(f"function is not approved: {name}")
    _reject_unsafe_short_term_matches(statement)
    limit = statement.args.get("limit")
    if limit is None:
        statement = statement.limit(maximum_limit)
    else:
        expression = limit.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            raise SQLRejected("LIMIT must be an integer literal")
        if int(expression.this) > maximum_limit:
            limit.set("expression", exp.Literal.number(maximum_limit))
    normalized = statement.sql(dialect="postgres", pretty=False)
    return ValidatedSQL(original=sql, normalized=normalized)


class PostgreSQLAdapter:
    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def execute(self, query: ValidatedSQL) -> SQLResult:
        async with self.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": f"{self.settings.sql_timeout_ms}ms"},
                )
                await connection.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": f"{self.settings.sql_lock_timeout_ms}ms"},
                )
                # exec_driver_sql bypasses SQLAlchemy's :name parsing; no_parameters
                # prevents Psycopg from parsing literal percent signs as placeholders.
                await connection.execution_options(no_parameters=True)
                explain = await connection.exec_driver_sql(
                    "EXPLAIN (FORMAT JSON) " + query.normalized
                )
                explain_value = explain.scalar_one()
                if isinstance(explain_value, str):
                    explain_value = json.loads(explain_value)
                cost = float(explain_value[0]["Plan"]["Total Cost"])
                if cost > self.settings.sql_explain_cost_ceiling:
                    raise SQLRejected(
                        f"query cost {cost:.2f} exceeds ceiling "
                        f"{self.settings.sql_explain_cost_ceiling:.2f}"
                    )
                cursor = await connection.exec_driver_sql(query.normalized)
                columns = list(cursor.keys())
                rows = [
                    dict(row._mapping)  # pyright: ignore[reportPrivateUsage]
                    for row in cursor.fetchall()
                ]
                serialized = json.dumps(rows, default=str, separators=(",", ":")).encode()
                if len(serialized) > self.settings.sql_payload_limit_bytes:
                    raise SQLRejected("SQL result payload exceeds configured limit")
                return SQLResult(
                    columns=columns,
                    rows=rows,
                    explain_cost=cost,
                    payload_bytes=len(serialized),
                    result_hash=hashlib.sha256(serialized).hexdigest(),
                )
            finally:
                await transaction.rollback()
