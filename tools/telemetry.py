"""案例只读工具：只挂载两窗十份遥测，保存原表 SQL、结果与可重放证据编号。"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from copy import deepcopy
import decimal
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

from filesystem import filesystem_path, atomic_write_text

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .trace_graph import TraceGraph, ident, lit, service
from .features import metric_sql, log_sql

TOOL_HELP = {
    "inspect_telemetry": "查看目录：action=inventory（十表行数、覆盖）、columns（另传 table）、components（观测服务名）、rows（table必填；可传fields字段列表、component、start/end、filters={原字段:标量}、limit，返回原行编号和证据）。表名为 normal/abnormal 加 _metrics、_metrics_sum、_metrics_histogram、_traces、_logs。",
    "query_telemetry": "自由只读 DuckDB 查询：sql=一条 SELECT 或 WITH…SELECT，只引用上述原表或合法裸文件名；可选 parameters=[按?顺序的标量]、kind=metric|trace|log（混合来源必填）、limit=1..100。支持聚合、连接、窗口函数；注明指标单位及样本数。先查列名；含点字段用双引号。",
    "analyze_metrics": (
        "指标：operation=rank（每服务一项普通线索，观察到uptime重置或新实例时另附一项生命周期线索）|compare（各完整指标序列）|series（需component和metric）。"
        "source=metrics|metrics_sum|metrics_histogram，component、metric、attrs={真实属性列:值}可选。"
        "method=robust|zscore，threshold默认3，min_normal默认4，consecutive默认2。transform=auto；普通数值可level，"
        "Sum可level/rate，Histogram可snapshot_mean/interval_mean/observation_rate。normal_range、abnormal_range={start?,end?}是UTC半开区间；"
        "series可传window=normal|abnormal|both，给出每点转换后数值减正常中心。compare给出正常/异常样本数、均值、中位数、样本方差、最小/最大、"
        "转换后观测值P95（不是Histogram事件分位数），以及中位数差、差的绝对值和相对变化（差除以正常中位数的绝对值，基线为零则空）。"
        "limit默认24；rank不是根因概率，未上榜指标仍可查，生命周期线索也可能因limit被截断。默认rank不将k8s.pod.uptime正常增长当作异常；"
        "无其他指标时保留生命周期行。lifecycle_signal=possible_reset只表示同实例窗口内观察到下降，需进一步核实；"
        "possible_new_instance表示正常窗有可识别实例且异常窗观察到新实例，可能是扩容而非重启；"
        "instance_identity_unknown表示实例身份不足，compare/series仍可查看原始数值。"
    ),
    "analyze_traces": "调用：operation=operations（分服务、操作及span类型统计）、operations_compare（两窗按服务/操作/span类型先完整聚合对齐再排序截断）、request（给trace_id展开同一请求）、pairs（实际跨服务父子）。operations_compare的window须为both或省略，可用normal_range/abnormal_range={start?,end?}、component、span_operation、span_kind、limit；返回两窗样本数、耗时、Span状态错误占比及变化、comparison_status和可回放证据。错误占比分母为状态可观测的Span，不是请求失败率；缺少对照、低样本、零基线不能直接判为根因；limit是对比结果总上限并按服务轮次展示。其他operation的window=normal|abnormal|both；both仅用于不指定trace_id的operations/pairs，两窗分别返回结果和证据编号，limit为每窗上限。duration_ms、mean_ms、p95_ms、parent_ms、child_ms均已经是毫秒，原始duration为纳秒。",
    "analyze_logs": "日志：operation=search（原文、时间、操作上下文）|compare（比较两个窗口模板频率）；可按component、trace_id、text、level、template查询，包括普通等级。search可选window=normal|abnormal|both、start/end。compare可选normal_range和abnormal_range。limit默认24。日志同时出现并不证明实际调用。",
    "query_graph": "查邻居事实：component必填，window=abnormal|normal|both，direction=incoming（谁调它）|outgoing（它调谁）|both；可选start/end（子记录开始时间）、limit（每窗口1..100，默认80）。edges每行有caller、callee、window、evidence_refs。它只报告观测调用；传播可同向或反向。未查询另一窗口不等于另一窗口无边。",
    "replay_evidence": "按证据编号实际重跑原始十表上的SQL：evidence_ref=此前返回的E-…编号；返回kind,sql,rows和截断标记。证据引用只属于当前案例；SQL不引用内部缓存或临时索引。",
}

KINDS = ("metrics", "metrics_sum", "metrics_histogram", "traces", "logs")
TABLES = tuple(f"{w}_{k}" for w in ("normal", "abnormal") for k in KINDS)
VERSION = "readonly-tools-2"


class DataChanged(RuntimeError):
    """案例输入在一次运行内发生变化，旧证据不能静默指向新数据。"""


class EvidenceValidationError(RuntimeError):
    """已保存证据无法通过 SQL 检查；不是 Agent 的格式错误或因果反证。"""

    def __init__(self, evidence_ref, reason):
        self.evidence_ref = evidence_ref
        self.reason = str(reason)
        super().__init__(f"已保存证据 {evidence_ref} 的 SQL 校验失败：{self.reason}")

    def details(self):
        return {"error_type": "evidence_sql_validation_failed", "evidence_ref": self.evidence_ref}


def clean(value):
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        # 独立 DuckDB 若显示当地时区，仍将同一时刻按 UTC 写入证据。
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def save(path, data):
    # 同目录原子替换；审核永远不会读到只写了一半的 JSON。
    atomic_write_text(path, json.dumps(clean(data), ensure_ascii=False, indent=2, allow_nan=False))


# 只开放分析所需的标量/聚合方法，不用“危险函数黑名单”代替只读边界。
SAFE_FUNCTIONS = set(
    """ABS AVG MIN MAX SUM COUNT STDDEV STDDEV_SAMP STDDEV_POP VAR_SAMP VAR_POP VARIANCE VARIANCE_POP
    MEDIAN MAD QUANTILE_CONT QUANTILE_DISC APPROX_QUANTILE PERCENTILE_CONT PERCENTILE_DISC
    CORR COVAR_SAMP COVAR_POP REGR_SLOPE REGR_INTERCEPT ROUND FLOOR CEIL CEILING SQRT
    EXP LN LOG LOG10 POWER POW SIGN MOD GREATEST LEAST COALESCE IFNULL NULLIF CASE IF
    CAST TRY_CAST ISFINITE ISNAN COUNT_IF COUNTIF FIRST LAST ARG_MAX ARG_MIN BOOL_AND BOOL_OR
    ANY_VALUE ROW_NUMBER RANK DENSE_RANK LAG LEAD NTILE PERCENT_RANK FIRST_VALUE LAST_VALUE
    EXTRACT DATE_TRUNC TIMESTAMP_TRUNC DATETIME_TRUNC TIME_TO_UNIX EPOCH EPOCH_MS EPOCH_US EPOCH_NS
    DATE_DIFF DATEDIFF TIME_BUCKET STRFTIME TIME_TO_STR STR_TO_TIME STRPTIME
    LOWER UPPER TRIM LTRIM RTRIM LENGTH CHAR_LENGTH SUBSTRING SUBSTR REPLACE REGEXP_REPLACE
    REGEXP_MATCHES REGEXP_LIKE REGEXP_EXTRACT CONTAINS STRPOS STARTS_WITH ENDS_WITH CONCAT
    CONCAT_WS IF_NULL J_S_O_N_OBJECT JSON_OBJECT STRFTIME ARRAY_TO_STRING LIST_VALUE
    ARRAY ARRAY_SIZE ARRAY_LENGTH LOGICAL_AND LOGICAL_OR AND OR""".split()
)


class Telemetry:
    def __init__(self, case_dir: Path, output_dir: Path):
        self.case_dir = Path(case_dir).resolve()
        self.out = Path(output_dir).resolve()
        if self.out == self.case_dir or self.case_dir in self.out.parents:
            raise ValueError("分析结果必须保存在原始案例目录之外")
        self.lock = threading.RLock()
        filesystem_path(self.out).mkdir(parents=True, exist_ok=True)
        self.evdir = self.out / "evidence"
        filesystem_path(self.evdir).mkdir(exist_ok=True)
        self._files = {}
        tables = {}
        snapshot = {}
        windows = {}
        for table in TABLES:
            p = self.case_dir / (table + ".parquet")
            if not p.is_file() or p.is_symlink() or p.resolve() != p:
                raise ValueError(f"缺少允许的本案例文件或路径越界：{table}.parquet")
            self._files[table] = p
            stat = p.stat()
            snapshot[table] = [stat.st_size, stat.st_mtime_ns]
            file = pq.ParquetFile(p)
            try:
                schema = file.schema_arrow
                meta = file.metadata
                fields = {c.name: str(c.type) for c in schema}
                bounds = []
                if "time" in fields and pa.types.is_timestamp(schema.field("time").type):
                    i = schema.names.index("time")
                    scale = {"s": 1, "ms": 1000, "us": 1000000, "ns": 1000000000}[
                        schema.field("time").type.unit
                    ]
                    for rg in range(meta.num_row_groups):
                        st = meta.row_group(rg).column(i).statistics
                        if st and st.has_min_max and isinstance(st.min_raw, int):
                            bounds.extend([st.min_raw / scale, st.max_raw / scale])
                start = (
                    dt.datetime.fromtimestamp(min(bounds), dt.timezone.utc).isoformat() if bounds else None
                )
                end = dt.datetime.fromtimestamp(max(bounds), dt.timezone.utc).isoformat() if bounds else None
                tables[table] = {
                    "columns": fields,
                    "row_count": meta.num_rows,
                    "start": start,
                    "end_inclusive": end,
                }
                w = table.split("_")[0]
                b = windows.setdefault(w, {"start": None, "end_inclusive": None})
                if start and (not b["start"] or start < b["start"]):
                    b["start"] = start
                if end and (not b["end_inclusive"] or end > b["end_inclusive"]):
                    b["end_inclusive"] = end
            finally:
                file.close()
        self.snapshot = snapshot
        manifest = self.out / "telemetry_version.json"
        old = json.loads(filesystem_path(manifest).read_text("utf-8")) if filesystem_path(manifest).is_file() else {}
        self.version = (old.get("data_version") if old.get("files") == snapshot else None) or uuid.uuid4().hex
        save(manifest, {"data_version": self.version, "tool_version": VERSION, "files": snapshot})
        self.catalog = {
            "data_version": self.version,
            "tables": tables,
            "windows": windows,
            "note": "覆盖来自Parquet文件头，非连续采样保证；原始日期为UTC。工具不读取标签、答案、注入配置或案例名称。",
        }
        self.db = duckdb.connect(
            ":memory:",
            config={
                "enable_external_access": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "allow_unsigned_extensions": "false",
                "threads": "1",
                "memory_limit": "768MB",
                "max_temp_directory_size": "0B",
            },
        )
        self.db.execute("SET TimeZone='UTC'")
        for table, p in self._files.items():
            self.db.register(table, ds.dataset(p, format="parquet"))
        self.graph = TraceGraph(self)
        self._services = None
        self._cache = {}
        self._results = OrderedDict()
        # 仅记录内置指标/日志查询已返回的服务与证据。自由 SQL 的别名或模型解释不自动升级成事实。
        self._service_facts = {}
        if old.get("data_version") == self.version:
            for path in sorted(filesystem_path(self.evdir).glob("E-*.json")):
                r = json.loads(path.read_text("utf-8"))
                if r.get("data_version") == self.version and r.get("tool_version") == VERSION:
                    self.remember(
                        r["kind"], r["rows"], r["truncated"], r.get("details", {}), r["evidence_ref"]
                    )

    def close(self):
        with self.lock:
            self.db.close()

    def check_current(self):
        """复用已取得的查询结果前，沿用工具执行时的原始文件版本检查。"""
        with self.lock:
            self._fresh()

    def columns(self, table):
        return set(self.catalog["tables"][table]["columns"])

    @staticmethod
    def limit(a, default=24):
        value = int(a.get("limit", a.get("max_rows", default)))
        if not 1 <= value <= 100:
            raise ValueError("limit 范围 1 至 100")
        return value

    @staticmethod
    def time_filter(a, column):
        if not isinstance(a, dict):
            raise ValueError("时间范围需要 {start,end} 对象")
        parts = []
        times = {}
        for k, op in [("start", ">="), ("end", "<")]:
            if not a.get(k):
                continue
            raw = str(a[k]).replace("Z", "+00:00")
            time = dt.datetime.fromisoformat(raw)
            if time.tzinfo is None:
                time = time.replace(tzinfo=dt.timezone.utc)
            times[k] = time
            parts.append(
                f"{column}{op}CAST({lit(time.astimezone(dt.timezone.utc).isoformat())} AS TIMESTAMPTZ)"
            )
        if len(times) == 2 and times["start"] >= times["end"]:
            raise ValueError("start 必须早于 end")
        return " AND ".join(parts) or "TRUE"

    def _fresh(self):
        for table, p in self._files.items():
            s = p.stat()
            if self.snapshot[table] != [s.st_size, s.st_mtime_ns]:
                raise DataChanged("原始案例文件已变化，不能混用旧索引或证据")

    @staticmethod
    def _checked_query(sql):
        """解析并检查同一套只读规则；允许的裸文件名统一替换为本案例表名。"""
        if not isinstance(sql, str) or not (1 <= len(sql) <= 160000):
            raise ValueError("SQL 为空或超长")
        statements = sqlglot.parse(sql, read="duckdb")
        if len(statements) != 1 or not isinstance(
            statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
        ):
            raise ValueError("只接受单条 SELECT 或 WITH…SELECT")
        tree = statements[0]
        if any(
            isinstance(
                n,
                (
                    exp.Insert,
                    exp.Update,
                    exp.Delete,
                    exp.Copy,
                    exp.Into,
                    exp.Command,
                    exp.Create,
                    exp.Alter,
                    exp.Drop,
                    exp.Merge,
                    exp.Set,
                ),
            )
            for n in tree.walk()
        ):
            raise ValueError("拒绝写入、导出或配置命令")
        if any(n.args.get("recursive") for n in tree.find_all(exp.With)):
            raise ValueError("不开放递归查询")
        for table in list(tree.find_all(exp.Table)):
            if table.args.get("db") or table.args.get("catalog"):
                raise ValueError("只允许当前案例的裸表名")
            src = table.this
            if isinstance(src, exp.Func):
                name = src.name.lower() if isinstance(src, exp.Anonymous) else src.sql_name().lower()
                argv = list(src.iter_expressions())
                if not (
                    name == "read_parquet"
                    and len(argv) == 1
                    and isinstance(argv[0], exp.Literal)
                    and argv[0].is_string
                    and argv[0].this in {k + ".parquet" for k in TABLES}
                ):
                    raise ValueError("拒绝表函数、通配符、路径或未授权数据源")
                table.set("this", exp.to_identifier(argv[0].this[:-8]))
            elif isinstance(src, exp.Identifier) and src.name in {k + ".parquet" for k in TABLES}:
                table.set("this", exp.to_identifier(src.name[:-8]))
            elif not isinstance(src, exp.Identifier):
                raise ValueError("不支持的SQL数据源")
        # 每个查询块独立解析来源。不能在另一子查询伪造同名 CTE，偷读系统表。
        used = set()
        for scope in traverse_scope(tree):
            for _, (_, source) in scope.selected_sources.items():
                if isinstance(source, exp.Table):
                    name = source.name.lower()
                    if name not in TABLES:
                        raise ValueError("不是本案例的白名单原表：" + name[:70])
                    used.add(name)
        if not used:
            raise ValueError("证据查询必须读取本案例至少一张原始遥测表")
        for n in tree.walk():
            if isinstance(n, exp.Func):
                nm = n.name.upper() if isinstance(n, exp.Anonymous) else n.sql_name().upper()
                if nm not in SAFE_FUNCTIONS:
                    raise ValueError("该函数不在只读分析清单：" + nm[:60])
        return tree, used

    def validate(self, sql, params=()):
        tree, _ = self._checked_query(sql)
        if not isinstance(params, (list, tuple)):
            raise ValueError("parameters 必须为按?顺序排列的标量列表")
        # 按 SQL 文本出现顺序绑定，避免遍历语法树时错绑嵌套查询中的参数。
        if params or tree.find(exp.Placeholder):
            pieces = []
            last = 0
            count = 0
            for tok in sqlglot.Dialect.get_or_raise("duckdb").tokenize(sql):
                if tok.text == "?" and sql[tok.start : tok.end + 1] == "?":
                    if count >= len(params):
                        raise ValueError("SQL参数数量不一致")
                    v = params[count]
                    if (
                        not isinstance(v, (str, int, float, bool, type(None)))
                        or isinstance(v, float)
                        and not math.isfinite(v)
                    ):
                        raise ValueError("参数必须为有限的标量")
                    literal = exp.convert(v).sql(dialect="duckdb")
                    pieces.extend([sql[last : tok.start], literal])
                    last = tok.end + 1
                    count += 1
            if count != len(params):
                raise ValueError("SQL参数数量不一致")
            pieces.append(sql[last:])
            return self.validate("".join(pieces))
        public = tree.sql(dialect="duckdb")
        # 转换库可能把 DATE_DIFF 改写为 EPOCH_NS 等函数。执行前检查实际返回的 SQL，
        # 不在这里递归调用 validate，也不因 SQL 来自转换库而跳过只读规则。
        _, used = self._checked_query(public)
        return public, used

    def fetch(self, sql, limit, *, allow_cache=True):
        # 同一个案例内工具调用串行，超时仅中断持有锁的当前查询。
        key = (sql, limit)
        if allow_cache and key in self._results:
            self._results.move_to_end(key)
            return deepcopy(self._results[key])
        timer = threading.Timer(20, self.db.interrupt)
        timer.daemon = True
        timer.start()
        try:
            cursor = self.db.execute(sql)
            cols = [x[0] for x in cursor.description]
            if len(cols) != len(set(cols)):
                raise ValueError("返回列名重复，请为SELECT列表指定清晰且不同的别名")
            data = cursor.fetchmany(limit + 1)
            result = [clean(dict(zip(cols, r))) for r in data[:limit]], len(data) > limit
            if allow_cache:
                self._results[key] = deepcopy(result)
                if len(self._results) > 128:
                    self._results.popitem(last=False)
            return result
        finally:
            timer.cancel()
            timer.join()

    def record(self, kind, sql, rows, truncated, details):
        key = (kind, sql, len(rows), bool(truncated), details.get("tool"), details.get("component_filter"))
        if key in self._cache:
            return self._cache[key]
        # 内置工具也通过这里保存证据。只检查，不改 SQL 文本、不重复查询数据库。
        self.validate(sql)
        ref = "E-" + uuid.uuid4().hex[:20]
        payload = {
            "evidence_ref": ref,
            "data_version": self.version,
            "tool_version": VERSION,
            "kind": kind,
            "sql": sql,
            "rows": rows,
            "truncated": truncated,
            "details": details,
            "saved_limit": max(1, len(rows)),
        }
        save(self.evdir / (ref + ".json"), payload)
        self._cache[key] = ref
        self.remember(kind, rows, truncated, details, ref)
        return ref

    def remember(self, kind, rows, truncated, details, ref):
        if details.get("tool") not in ("analyze_metrics", "analyze_logs"):
            return
        present = {row.get("service") for row in rows if isinstance(row.get("service"), str)}
        names = set(present)
        if details.get("component_filter"):
            names.add(details["component_filter"])
        for name in names:
            bag = self._service_facts.setdefault((name, kind), {})
            bag[ref] = {
                "evidence_ref": ref,
                "row_filter": {"service": name},
                "status": (
                    "observed" if name in present else "incomplete" if truncated else "queried_no_match"
                ),
                "operation": details.get("operation"),
                "window": details.get("window", "both"),
            }

    def neighbor_context(self, component):
        result = {}
        for kind in ("metric", "log"):
            seen = list(self._service_facts.get((component, kind), {}).values())
            state = (
                "observed"
                if any(r["status"] == "observed" for r in seen)
                else (
                    "incomplete"
                    if any(r["status"] == "incomplete" for r in seen)
                    else "queried_no_match"
                    if seen
                    else "not_queried"
                )
            )
            result[kind] = {
                "status": state,
                "query_count": len(seen),
                "recent_evidence": seen[-3:],
                "inspect_with": "analyze_metrics" if kind == "metric" else "analyze_logs",
            }
        return result

    def result(self, kind, sql, rows, truncated, details):
        ref = self.record(kind, sql, rows, truncated, details)
        return {
            "status": "ok" if rows else "no_observations",
            "kind": kind,
            "rows": rows,
            "truncated": truncated,
            "returned_count": len(rows),
            "details": details,
            "evidence_refs": [ref],
        }

    def _query_result(self, kind, sql, limit, details):
        # 在独立执行 DuckDB 时也保持相同上界，同时多取一条检测截断。
        public = f"SELECT * FROM ({sql}) AS observations LIMIT {limit}"
        rows, trunc = self.fetch(f"SELECT * FROM ({sql}) AS observations LIMIT {limit + 1}", limit)
        return self.result(kind, public, rows, trunc, details)

    def evidence(self, evidence_ref):
        """读取此案例登记过的工具结果，核对版本；不改内容、不重复统计。"""
        with self.lock:
            if not isinstance(evidence_ref, str) or not re.fullmatch(r"E-[a-f0-9]{20}", evidence_ref):
                raise ValueError("无效证据编号")
            path = filesystem_path(self.evdir / (evidence_ref + ".json"))
            if not path.is_file():
                raise ValueError("此案例不存在该证据编号")
            self._fresh()
            record = json.loads(path.read_text("utf-8"))
            if record.get("data_version") != self.version or record.get("tool_version") != VERSION:
                raise DataChanged("该证据不属于本次数据或工具版本")
            return record

    def validated_evidence(self, evidence_ref, cache=None):
        """读取已保存证据并检查 SQL；数据变化、文件读取错误保留原有异常类型。"""
        record = self.evidence(evidence_ref)
        sql = record.get("sql")
        try:
            if cache is not None and isinstance(sql, str) and sql in cache:
                public, sources = deepcopy(cache[sql])
            else:
                public, sources = self.validate(sql)
                if cache is not None and isinstance(sql, str):
                    cache[sql] = deepcopy((public, sources))
        except (ValueError, sqlglot.errors.SqlglotError) as exc:
            raise EvidenceValidationError(evidence_ref, exc) from exc
        return record, public, sources

    def verify(self, evidence_ref):
        with self.lock:
            record, public, _ = self.validated_evidence(evidence_ref)
            rows, trunc = self.fetch(public, record["saved_limit"], allow_cache=False)
            return {
                "status": "ok" if rows else "no_observations",
                "kind": record["kind"],
                "sql": record["sql"],
                "rows": rows,
                "truncated": trunc or record["truncated"],
                "data_version": self.version,
                "evidence_refs": [evidence_ref],
            }

    def service_names(self):
        with self.lock:
            if self._services is None:
                # 服务目录仅采集真实服务身份；内容、路径及故障标签不作为候选来源。
                queries = [
                    f"SELECT DISTINCT {service(self.columns(tbl))} AS svc FROM {tbl}" for tbl in TABLES
                ]
                vals = self.db.execute(
                    "SELECT DISTINCT svc FROM (" + " UNION ALL ".join(queries) + ") WHERE svc IS NOT NULL"
                ).fetchall()
                self._services = {v for (v,) in vals}
            return set(self._services)

    @staticmethod
    def _diagnostic_tables(sql):
        """仅识别无效 SQL 中 FROM/JOIN 后的白名单表名；不执行或修复查询。"""
        if not isinstance(sql, str) or not (1 <= len(sql) <= 160000):
            return []
        try:
            tokens = sqlglot.tokenize(sql, read="duckdb")
        except sqlglot.errors.SqlglotError:
            return []
        found = set()
        for pos, token in enumerate(tokens[:-1]):
            if token.token_type.name not in {"FROM", "JOIN"}:
                continue
            source = tokens[pos + 1]
            name = source.text
            if (source.token_type.name == "VAR" and name.lower() == "read_parquet"
                    and pos + 4 < len(tokens)
                    and tokens[pos + 2].token_type.name == "L_PAREN"
                    and tokens[pos + 3].token_type.name == "STRING"
                    and tokens[pos + 4].token_type.name == "R_PAREN"):
                name = tokens[pos + 3].text
            elif source.token_type.name not in {"VAR", "IDENTIFIER"}:
                continue
            if name.endswith(".parquet"):
                name = name[:-8]
            if name.lower() in TABLES:
                found.add(name.lower())
        return sorted(found)

    def _query_feedback(self, name, args, exc, used=()):
        """失败时附已有字段元数据，帮助下一轮纠错；不产生观测或额外工具调用。"""
        if not isinstance(args, dict):
            return None
        hints = []
        exact_sources = bool(used)
        tables = sorted(set(used) & set(TABLES))
        if name == "query_telemetry":
            if not isinstance(exc, (duckdb.BinderException, duckdb.ParserException,
                                    duckdb.ConversionException, sqlglot.errors.ParseError)):
                return None
            if not tables:
                tables = self._diagnostic_tables(args.get("sql"))
            if not tables:
                return None
            hints.append('核对下列真实字段和类型；含点字段须作为整体加双引号，如 "attr.span_kind"。')
            message = str(exc).lower()
            if (isinstance(exc, (duckdb.ParserException, sqlglot.errors.ParseError))
                    and re.search(r"\bwindow\b", str(args.get("sql", "")), re.IGNORECASE)):
                hints.append('若 window 用作别名，定义及引用均加双引号（AS "window"、GROUP BY "window"），或使用非保留字别名。')
            if "timestamp" in message or "interval" in message:
                hints.append("时间戳不能直接当数值除以 1000；按所需单位显式使用 epoch_ns/epoch_ms，或使用 date_diff。保留原时间范围和单位含义。")
        elif name == "inspect_telemetry" and isinstance(exc, (ValueError, KeyError, TypeError)):
            table = args.get("table")
            tables = [table] if isinstance(table, str) and table in TABLES else []
            exact_sources = True
            if not tables:
                return None
            hints.append("fields 必须是该表真实字段的列表；filters 以真实字段为键，值只接受标量。")
            if args.get("action") not in ("inventory", "columns", "components", "rows"):
                hints.append("action 只接受 inventory、columns、components、rows。")
        else:
            return None
        shown = tables[:2]
        return {
            "metadata_only": True,
            "note": "以下是已加载的原表字段元数据和语法提示，不是查询结果，不构成故障证据；原查询未被自动修改或重试。",
            "source_detection": "validated_tables" if exact_sources else "names_in_invalid_sql_not_validated",
            "schemas": {table: dict(self.catalog["tables"][table]["columns"]) for table in shown},
            "schema_table_count": len(tables),
            "omitted_schema_tables": tables[2:],
            "hints": hints,
        }

    def call(self, name, args):
        with self.lock:
            used = ()
            try:
                if not isinstance(args, dict):
                    raise ValueError("参数应为一个对象")
                self._fresh()
                if args.get("data_version", self.version) != self.version:
                    raise DataChanged("请求指定的数据版本与本案例不符")
                if name == "inspect_telemetry":
                    mode = args.get("action", "inventory")
                    if mode == "inventory":
                        return {"status": "ok", "catalog": self.catalog, "evidence_refs": []}
                    if mode == "components":
                        return {"status": "ok", "services": sorted(self.service_names()), "evidence_refs": []}
                    table = args.get("table")
                    if table not in TABLES:
                        raise ValueError("columns/rows 需要指定合法 table")
                    if mode == "columns":
                        return {
                            "status": "ok",
                            "table": table,
                            **self.catalog["tables"][table],
                            "evidence_refs": [],
                        }
                    if mode != "rows":
                        raise ValueError("action 应为 inventory/columns/components/rows")
                    cols = self.columns(table)
                    fields = args.get("fields") or list(self.catalog["tables"][table]["columns"])
                    if (
                        not isinstance(fields, list)
                        or not fields
                        or not all(isinstance(c, str) and c in cols for c in fields)
                    ):
                        raise ValueError("fields 必须是原始表字段的列表")
                    where = [self.time_filter(args, "time")]
                    if args.get("component"):
                        where.append(service(cols) + "=" + lit(args["component"]))
                    filters = args.get("filters", {})
                    if not isinstance(filters, dict) or not set(filters) <= cols:
                        raise ValueError("filters 需要使用原始字段")
                    for k, v in filters.items():
                        if isinstance(v, (list, dict)):
                            raise ValueError("精确过滤仅接受标量")
                        where.append(ident(k) + (" IS NULL" if v is None else "=" + lit(v)))
                    sql = (
                        "SELECT source_row,"
                        + ",".join(ident(c) for c in fields)
                        + f" FROM (SELECT *,row_number() OVER ()-1 AS source_row FROM {table}) AS original_rows WHERE "
                        + " AND ".join(where)
                        + " ORDER BY source_row"
                    )
                    kind = (
                        "trace"
                        if table.endswith("_traces")
                        else "log"
                        if table.endswith("_logs")
                        else "metric"
                    )
                    return self._query_result(
                        kind,
                        sql,
                        self.limit(args),
                        {"source_file": table + ".parquet", "row_numbering": "original file, zero based"},
                    )
                if name == "query_telemetry":
                    public, used = self.validate(args.get("sql"), args.get("parameters", ()))
                    kinds = {
                        "trace" if k.endswith("_traces") else "log" if k.endswith("_logs") else "metric"
                        for k in used
                    }
                    kind = args.get("kind") or (next(iter(kinds)) if len(kinds) == 1 else None)
                    if kind not in kinds:
                        raise ValueError("混合查询须指定来源中实际存在的 kind：metric、trace 或 log")
                    out = self._query_result(kind, public, self.limit(args), {"tables": sorted(used)})
                elif name == "analyze_metrics":
                    sql, details = metric_sql(self, args)
                    details.update(
                        tool="analyze_metrics",
                        component_filter=args.get("component"),
                        window=(
                            args.get("window", "abnormal") if args.get("operation") == "series" else "both"
                        ),
                    )
                    out = self._query_result("metric", sql, self.limit(args), details)
                elif name == "analyze_logs":
                    out = self._query_result(
                        "log",
                        log_sql(self, args),
                        self.limit(args),
                        {
                            "tool": "analyze_logs",
                            "component_filter": args.get("component"),
                            "operation": args.get("operation", "compare"),
                            "window": (
                                args.get("window", "abnormal")
                                if args.get("operation") == "search"
                                else "both"
                            ),
                            "note": "优先使用数据自带模板；无模板时只替换数字。频率按各窗口日志覆盖时长计算；无记录不等于没有发生。",
                        },
                    )
                elif name == "query_graph":
                    out = self.graph.graph(args)
                elif name == "analyze_traces":
                    out = self.graph.traces(args)
                elif name == "replay_evidence":
                    out = self.verify(args.get("evidence_ref"))
                else:
                    raise ValueError("工具名不在允许清单")
                out.setdefault("status", "ok")
                out["data_version"] = self.version
                return out
            except DataChanged as exc:
                return {"status": "stale_data", "error": str(exc), "evidence_refs": []}
            except (ValueError, KeyError, TypeError, sqlglot.errors.SqlglotError) as exc:
                result = {"status": "invalid_query", "error": str(exc)[:700], "evidence_refs": []}
                feedback = self._query_feedback(name, args, exc, used)
                if feedback is not None:
                    result["query_feedback"] = feedback
                return result
            except (duckdb.Error, RuntimeError, OSError) as exc:
                # 错误文字可能含本机真实案例路径；agent 不得从案例名称获得注入提示。
                msg = (
                    str(exc)
                    .replace(str(self.case_dir), "[当前案例]")
                    .replace(self.case_dir.name, "[当前案例]")
                )
                result = {"status": "technical_error", "error": msg[:850], "evidence_refs": []}
                if isinstance(exc, EvidenceValidationError):
                    result.update(exc.details())
                feedback = self._query_feedback(name, args, exc, used)
                if feedback is not None:
                    result["query_feedback"] = feedback
                return result
