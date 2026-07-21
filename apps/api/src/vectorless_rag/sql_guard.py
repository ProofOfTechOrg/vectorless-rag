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
from sqlglot.errors import ParseError

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
        "array_to_string",
        "avg",
        "cardinality",
        "coalesce",
        "count",
        "date_trunc",
        "lower",
        "max",
        "min",
        "sum",
        "unnest",
        "upper",
    }
)
FUNCTION_NAME_ALIASES = {
    "arraytostring": "array_to_string",
    "timestamptrunc": "date_trunc",
}
SHORT_REGEX_TERM = re.compile(r"(?<![A-Za-z\\])([A-Za-z]{2,3})(?![A-Za-z])")


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


def _reject_unsafe_short_term_matches(statement: exp.Query) -> None:
    for predicate in statement.find_all(exp.Like, exp.ILike):
        pattern = predicate.expression
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            continue
        value = str(pattern.this)
        token = value[1:-1] if value.startswith("%") and value.endswith("%") else ""
        if token.isalnum() and len(token) <= 3:
            raise SQLRejected(
                f"short term {token!r} cannot use an unbounded substring match; "
                "use a whole-token predicate"
            )
    for predicate in statement.find_all(exp.RegexpLike, exp.RegexpILike):
        pattern = predicate.expression
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            continue
        value = str(pattern.this)
        for match in SHORT_REGEX_TERM.finditer(value):
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
    except ParseError as exc:
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
    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    relations = {
        _relation_name(table)
        for table in statement.find_all(exp.Table)
        if table.name not in cte_names
    }
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
                explain = await connection.execute(
                    text("EXPLAIN (FORMAT JSON) " + query.normalized)
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
                cursor = await connection.execute(text(query.normalized))
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
