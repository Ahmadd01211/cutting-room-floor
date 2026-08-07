"""Database access with two interchangeable backends.

  * `ClickHouseBackend` — clickhouse-connect against a real server. What runs
    in Docker, in ClickHouse Cloud, and behind the agent.
  * `ChdbBackend` — chdb, an embedded ClickHouse. Same engine, same SQL, no
    server. Lets `pytest` run with no Docker and no network, which means a judge
    can verify the ASOF logic in one command.

Both speak ClickHouse's `{name:Type}` parameter syntax. The server binds those
natively; the embedded backend substitutes them with ClickHouse-correct quoting
before execution.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Protocol

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

_PARAM = re.compile(r"\{(\w+):([A-Za-z0-9_()]+)\}")


def _quote(value: Any, ch_type: str) -> str:
    """Render a Python value as a ClickHouse literal."""
    if value is None:
        return "NULL"
    if ch_type.startswith(("UInt", "Int")):
        return str(int(value))
    if ch_type.startswith(("Float", "Decimal")):
        return repr(float(value))
    if ch_type.startswith("Array"):
        inner = ch_type[6:-1] or "String"
        return "[" + ",".join(_quote(v, inner) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def inline_params(sql: str, params: dict[str, Any] | None) -> str:
    params = params or {}

    def sub(m: re.Match) -> str:
        name, ch_type = m.group(1), m.group(2)
        if name not in params:
            raise KeyError(f"query needs parameter {name!r}")
        return _quote(params[name], ch_type)

    return _PARAM.sub(sub, sql)


def split_statements(script: str) -> list[str]:
    """Split a .sql file on semicolons, ignoring those inside string literals
    and `--` comments."""
    out, buf, in_str, in_comment = [], [], False, False
    prev = ""
    for ch in script:
        if in_comment:
            buf.append(ch)
            if ch == "\n":
                in_comment = False
        elif in_str:
            buf.append(ch)
            if ch == "'" and prev != "\\":
                in_str = False
        elif ch == "-" and prev == "-":
            buf.append(ch)
            in_comment = True
        elif ch == "'":
            buf.append(ch)
            in_str = True
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        prev = ch
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [s for s in out if not all(l.strip().startswith("--") or not l.strip()
                                      for l in s.splitlines())]


class Backend(Protocol):
    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None: ...
    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]: ...
    def insert(self, table: str, rows: Iterable[tuple], columns: list[str]) -> None: ...


class ClickHouseBackend:
    def __init__(self, host: str, port: int, user: str, password: str,
                 secure: bool = False, database: str = "default"):
        import clickhouse_connect

        self.client = clickhouse_connect.get_client(
            host=host, port=port, username=user, password=password,
            secure=secure, database=database,
        )

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.client.command(sql, parameters=params or {})

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        r = self.client.query(sql, parameters=params or {})
        return [dict(zip(r.column_names, row)) for row in r.result_rows]

    def insert(self, table: str, rows: Iterable[tuple], columns: list[str]) -> None:
        rows = list(rows)
        if rows:
            db, _, tbl = table.partition(".")
            self.client.insert(tbl, rows, column_names=columns, database=db or "default")


_CHDB_SESSION = None
_CHDB_PATH: str | None = None


class ChdbBackend:
    """Embedded ClickHouse. Same engine; no server, no network.

    chdb permits exactly ONE embedded server per process — a second
    `Session` with a different path raises `EmbeddedServer already
    initialized`. So the session is a process-level singleton here, and
    constructing another `ChdbBackend` hands back the same engine rather
    than exploding halfway through a test run.
    """

    def __init__(self, path: str | None = None):
        global _CHDB_SESSION, _CHDB_PATH
        import tempfile

        import chdb.session as chs

        if _CHDB_SESSION is None:
            _CHDB_PATH = path or tempfile.mkdtemp()
            _CHDB_SESSION = chs.Session(_CHDB_PATH)
        elif path is not None and path != _CHDB_PATH:
            raise RuntimeError(
                f"chdb is already running at {_CHDB_PATH!r}; it allows one "
                f"embedded server per process, so {path!r} cannot be opened. "
                f"Share the backend instead of constructing a second one."
            )
        self.session = _CHDB_SESSION

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.session.query(inline_params(sql, params))

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        sql = inline_params(sql, params).rstrip().rstrip(";")
        raw = str(self.session.query(sql + " FORMAT JSONEachRow")).strip()
        if not raw:
            return []
        import json

        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def insert(self, table: str, rows: Iterable[tuple], columns: list[str]) -> None:
        rows = list(rows)
        if not rows:
            return
        # chunked to keep the generated statement a sane size
        for i in range(0, len(rows), 20_000):
            chunk = rows[i : i + 20_000]
            values = ",".join(
                "(" + ",".join(
                    str(v) if isinstance(v, (int, float))
                    else "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"
                    for v in row
                ) + ")"
                for row in chunk
            )
            self.session.query(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES {values}"
            )


# ----------------------------------------------------------------- migrations


def load_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def load_query(name: str) -> str:
    return (SQL_DIR / "queries" / name).read_text(encoding="utf-8")


def migrate(backend: Backend, verbose: bool = True) -> None:
    """Apply schema and rollups. Idempotent — every statement is IF NOT EXISTS."""
    for script in ("001_schema.sql", "002_rollups.sql", "004_feedback.sql"):
        for stmt in split_statements(load_sql(script)):
            backend.execute(stmt)
        if verbose:
            print(f"  applied {script}")

    # The HNSW index is applied separately and tolerated as optional: it is
    # compiled out of some builds (notably chdb), and brute-force
    # cosineDistance stays correct without it — just slower at corpus scale.
    try:
        for stmt in split_statements(load_sql("003_vector_index.sql")):
            backend.execute(stmt)
        if verbose:
            print("  applied 003_vector_index.sql")
    except Exception as exc:  # noqa: BLE001 - genuinely optional
        if verbose:
            print(f"  skipped 003_vector_index.sql ({str(exc).splitlines()[0][:90]})")
