"""Read-only queries over ten explicitly registered Arrow datasets.

SQL never opens a path: validated read_parquet calls are translated into registered
relations. DuckDB external access and extension autoloading are disabled.
"""
from pathlib import Path
import threading
import duckdb
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp
from data.loader import PARQUET_FILES
from storage.results import dumps, json_safe


class ParquetStore:
    def __init__(self, directory: Path, settings: dict):
        self.directory = directory.resolve()
        self.settings = settings
        self.paths = {}
        for name in PARQUET_FILES:
            path = self.directory / name
            if not path.is_file() or path.resolve().parent != self.directory:
                raise ValueError(f"Missing or non-local parquet: {name}")
            self.paths[Path(name).stem] = path

    def list_tables(self):
        result = []
        for table, path in self.paths.items():
            metadata = pq.ParquetFile(path).metadata
            result.append({"file": path.name, "table": table,
                           "rows": metadata.num_rows, "columns": metadata.num_columns})
        return {"tables": result}

    def schema(self, filename: str):
        if filename not in PARQUET_FILES:
            raise ValueError("Only a bare filename from the current case is permitted")
        parquet = pq.ParquetFile(self.paths[Path(filename).stem])
        return {"file": filename, "rows": parquet.metadata.num_rows,
                "columns": [{"name": f.name, "type": str(f.type)} for f in parquet.schema_arrow]}

    def validate_sql(self, sql: str) -> str:
        statements = sqlglot.parse(sql, read="duckdb")
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise ValueError("Exactly one SELECT query is allowed")
        tree = statements[0]
        forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command,
                     exp.Into, exp.Copy, exp.Merge, exp.Alter)
        if any(isinstance(node, forbidden) for node in tree.walk()):
            raise ValueError("Only read-only SELECT is allowed")
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table in list(tree.find_all(exp.Table)):
            if table.args.get("db") or table.args.get("catalog"):
                raise ValueError("Catalog and schema access is not allowed")
            source = table.this
            if isinstance(source, exp.Func):
                function = source.name.lower() if isinstance(source, exp.Anonymous) else source.sql_name().lower()
                if function != "read_parquet":
                    raise ValueError("Only registered tables or read_parquet with a bare case filename are allowed")
                args = list(source.iter_expressions())
                if len(args) != 1 or not isinstance(args[0], exp.Literal) or not args[0].is_string:
                    raise ValueError("read_parquet needs exactly one literal bare filename")
                filename = args[0].this
                if filename not in PARQUET_FILES:
                    raise ValueError("The file is not in the current case telemetry allowlist")
                table.set("this", exp.to_identifier(Path(filename).stem))
            elif isinstance(source, exp.Identifier):
                if table.name.lower() not in self.paths and table.name.lower() not in ctes:
                    raise ValueError(f"Table is not an allowed telemetry table: {table.name}")
            else:
                raise ValueError("Unsupported table source")
        # Ban dynamic SQL and system/environment inspection even in scalar expressions.
        banned = {"query", "query_table", "getenv", "current_setting", "read_blob", "read_text",
                  "glob", "read_csv", "read_csv_auto", "read_json", "read_json_auto", "sqlite_scan",
                  "postgres_scan", "http_get", "read_parquet", "parquet_scan"}
        for node in tree.find_all(exp.Func):
            name = node.name.lower() if isinstance(node, exp.Anonymous) else node.sql_name().lower()
            if name in banned or name.startswith(("duckdb_", "pragma_")):
                raise ValueError(f"Function is not permitted: {name}")
        return tree.sql(dialect="duckdb")

    def query(self, sql: str, *, evidence_only: bool = False):
        executable_sql = self.validate_sql(sql)
        connection = duckdb.connect(":memory:", config={
            "enable_external_access": "false", "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false", "allow_unsigned_extensions": "false",
            "threads": str(self.settings["duckdb_threads"]),
            "memory_limit": self.settings["duckdb_memory_limit"],
        })
        timer = None
        try:
            for name, path in self.paths.items():
                connection.register(name, ds.dataset(path, format="parquet"))
            timer = threading.Timer(self.settings["query_timeout_seconds"], connection.interrupt)
            timer.daemon = True
            timer.start()
            cursor = connection.execute(executable_sql)
            columns = [column[0] for column in cursor.description]
            limit = 1 if evidence_only else self.settings["query_max_rows"]
            fetched = cursor.fetchmany(limit + 1)
            rows = json_safe([list(row) for row in fetched[:limit]])
            truncated = len(fetched) > limit
            if not evidence_only:
                # Visible truncation, never silently trim a cell or rewrite the user's SQL.
                while rows and len(dumps({"columns": columns, "rows": rows})) > self.settings["query_max_characters"]:
                    rows.pop()
                    truncated = True
            return {"columns": columns, "rows": rows, "returned_rows": len(rows),
                    "has_rows": bool(fetched), "truncated": truncated,
                    "row_limit": limit, "sql": sql}
        finally:
            if timer:
                timer.cancel()
                timer.join()
            connection.close()
