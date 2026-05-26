"""SQL linter — first layer of the safety gate.

Uses sqlglot to parse SQL into an AST, then walks it to enforce the
forbidden-pattern policy. The linter is the cheap gate that runs before
any dry-run or human-approval step.

Policy:
- Single statement only (no multi-statement payloads)
- No DDL (CREATE, ALTER, DROP, TRUNCATE)
- No DML write operations on the read path (INSERT, UPDATE, DELETE, MERGE)
- Top-level statement must be SELECT (or WITH + SELECT)
- No SELECT INTO (would create a table)
- No PRAGMA statements
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class SqlSafetyViolation(Exception):
    """Raised when a SQL statement violates the safety policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_FORBIDDEN_TOP_LEVEL = (
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Pragma,
)


@dataclass(frozen=True)
class LintResult:
    statement: exp.Expression
    parameters_referenced: tuple[str, ...]


class SqlLinter:
    """Parse a SQL string and enforce the safety policy.

    The `dialect` parameter is passed to sqlglot — pick the closest match
    to your backend (e.g., 'sqlite' for the PoC, 'bigquery' for production).
    Some patterns are dialect-specific so picking accurately matters.
    """

    def __init__(self, dialect: str = "sqlite") -> None:
        self._dialect = dialect

    def lint(self, sql: str) -> LintResult:
        try:
            statements = sqlglot.parse(sql, dialect=self._dialect)
        except sqlglot.errors.ParseError as exc:
            raise SqlSafetyViolation("PARSE_ERROR", str(exc)) from exc

        non_null = [s for s in statements if s is not None]
        if not non_null:
            raise SqlSafetyViolation("EMPTY", "no statements parsed from input")
        if len(non_null) > 1:
            raise SqlSafetyViolation(
                "MULTI_STATEMENT",
                f"only a single statement is permitted, got {len(non_null)}",
            )

        statement = non_null[0]
        self._check_top_level(statement)
        self._check_no_into(statement)

        parameters = self._extract_parameters(statement)
        return LintResult(statement=statement, parameters_referenced=tuple(sorted(parameters)))

    def _check_top_level(self, statement: exp.Expression) -> None:
        if isinstance(statement, _FORBIDDEN_TOP_LEVEL):
            raise SqlSafetyViolation(
                "FORBIDDEN_OPERATION",
                f"{type(statement).__name__} is not permitted on the read path",
            )
        # The root must be a SELECT (or CTE-headed SELECT).
        if not isinstance(statement, (exp.Select, exp.With, exp.Union)):
            raise SqlSafetyViolation(
                "FORBIDDEN_OPERATION",
                f"top-level statement {type(statement).__name__} is not a SELECT",
            )
        # WITH must terminate in a SELECT body.
        if isinstance(statement, exp.With):
            body = statement.this
            if not isinstance(body, (exp.Select, exp.Union)):
                raise SqlSafetyViolation(
                    "FORBIDDEN_OPERATION",
                    f"WITH body is {type(body).__name__}, not SELECT",
                )

    @staticmethod
    def _check_no_into(statement: exp.Expression) -> None:
        # SELECT ... INTO would create a table.
        for select in statement.find_all(exp.Select):
            if select.args.get("into") is not None:
                raise SqlSafetyViolation(
                    "FORBIDDEN_OPERATION", "SELECT INTO is not permitted"
                )

    @staticmethod
    def _extract_parameters(statement: exp.Expression) -> set[str]:
        params: set[str] = set()
        for node in statement.find_all(exp.Placeholder, exp.Parameter):
            name = node.name or node.this
            if isinstance(name, str) and name:
                params.add(name)
        return params
