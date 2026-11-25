"""按窗口建调用记录索引，只有调查某服务时才提取与该服务相连的真实调用。"""

from __future__ import annotations


def lit(value):
    """SQL 标量字面量；表/字段名一律来自运行时目录，绝不从这里拼成标识符。"""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def service(columns):
    # 只有明确标为服务的字段参与跨服务调用。缺失名称不猜测成 pod、应用或部署名。
    available = [k for k in ("service_name", "attr.k8s.service.name") if k in columns]
    opts = [f"NULLIF(TRIM(CAST({ident(k)} AS VARCHAR)),'')" for k in available]
    return "COALESCE(" + ",".join(opts + ["NULL"]) + ")" if opts else "CAST(NULL AS VARCHAR)"


def value_col(columns, name, dtype="VARCHAR"):
    return f"CAST({ident(name)} AS {dtype})" if name in columns else f"CAST(NULL AS {dtype})"


class TraceGraph:
    def __init__(self, telemetry):
        self.t = telemetry
        self.windows = set()
        self.coverage = {}
        # 一次局部展开的范围与截断状态；不能把尚未展开的另一窗口/另一方向写成“没有边”。
        self.known_queries = {}

    def source_sql(self, window):
        table = window + "_traces"
        cols = self.t.columns(table)
        required = {"trace_id", "span_id", "parent_span_id", "time", "duration"}
        if missing := required - cols:
            raise ValueError("调用表缺字段：" + ", ".join(sorted(missing)))
        kind = value_col(cols, "attr.span_kind")
        op = value_col(cols, "span_name")
        status = value_col(cols, "attr.status_code")
        http = value_col(cols, "attr.http.response.status_code", "BIGINT")
        # 先在整窗检查唯一身份，再按服务/时间取邻居；否则过滤掉另一组件会造成错配。
        # 原始序号仅用于此版本文件内定位；公开 SQL 的证据同时带 trace/span 身份。
        return f"""SELECT trace_id,span_id,parent_span_id,file_row,{service(cols)} AS svc,
          time AS started,{op} AS op,{kind} AS span_kind,
          CASE WHEN duration>=0 THEN CAST(duration AS DOUBLE)/1000000.0 END AS ms,
          {status} AS status_code,{http} AS http_status,
          COUNT(*) OVER(PARTITION BY trace_id,span_id) AS identity_count
          FROM (SELECT *,row_number() OVER ()-1 AS file_row FROM {table}) AS raw_rows
          WHERE trace_id IS NOT NULL AND trim(trace_id)!=''
            AND span_id IS NOT NULL AND trim(span_id)!=''"""

    def index(self, window):
        if window not in ("normal", "abnormal"):
            raise ValueError("window 只能是 normal 或 abnormal")
        if window in self.windows:
            return
        name = "_idx_" + window
        self.t.db.execute(f"CREATE TEMP TABLE {name} AS " + self.source_sql(window))
        for n, cols in [("id", "trace_id,span_id"), ("parent", "trace_id,parent_span_id"), ("svc", "svc")]:
            self.t.db.execute(f"CREATE INDEX {name}_{n} ON {name}({cols})")
        # 记录“缺父”和“真正入口”的区别。没有找到父记录不能当作没有调用。
        summary = f"""SELECT count(*) AS indexed_rows,
          count(*) FILTER (WHERE identity_count>1) AS duplicate_identity_rows,
          count(*) FILTER (WHERE svc IS NULL) AS unknown_service_rows,
          count(*) FILTER (WHERE coalesce(parent_span_id,'') IN ('','0','0000000000000000')) AS root_rows,
          count(*) FILTER (WHERE identity_count=1 AND coalesce(parent_span_id,'') NOT IN ('','0','0000000000000000')
            AND NOT EXISTS(SELECT 1 FROM {name} parent WHERE parent.trace_id=s.trace_id
              AND parent.span_id=s.parent_span_id AND parent.identity_count=1)) AS missing_or_ambiguous_parent_rows
          FROM {name} s"""
        coverage = self.t.fetch(summary, 1)[0][0]
        coverage["raw_rows"] = self.t.catalog["tables"][window + "_traces"]["row_count"]
        coverage["empty_identity_rows"] = coverage["raw_rows"] - coverage["indexed_rows"]
        self.coverage[window] = coverage
        self.windows.add(window)

    @staticmethod
    def join(source, conditions):
        return f"""FROM {source} AS p JOIN {source} AS c
          ON p.trace_id=c.trace_id AND p.span_id=c.parent_span_id
          WHERE p.identity_count=1 AND c.identity_count=1 AND p.svc IS NOT NULL AND c.svc IS NOT NULL
            AND p.svc<>c.svc AND c.parent_span_id NOT IN ('','0','0000000000000000')
            AND ({conditions})"""

    @staticmethod
    def stats():
        return """p.svc AS caller,c.svc AS callee,COUNT(*) AS matched_pairs,
          COUNT(DISTINCT c.trace_id) AS independent_requests,COUNT(c.ms) AS valid_child_durations,
          AVG(c.ms) AS mean_child_ms,QUANTILE_CONT(c.ms,0.95) AS p95_child_ms,
          AVG(p.ms) AS mean_matched_parent_ms,
          COUNT(*) FILTER (WHERE lower(c.status_code) IN ('error','2','status_code_error') OR c.http_status>=500) AS child_error_records,
          FIRST(c.trace_id ORDER BY c.trace_id,c.span_id) AS example_trace_id,
          FIRST(p.span_id ORDER BY c.trace_id,c.span_id) AS example_parent_span_id,
          FIRST(c.span_id ORDER BY c.trace_id,c.span_id) AS example_child_span_id,
          FIRST(p.file_row ORDER BY c.trace_id,c.span_id) AS example_parent_file_row,
          FIRST(c.file_row ORDER BY c.trace_id,c.span_id) AS example_child_file_row"""

    def graph(self, a):
        component = a.get("component")
        if not isinstance(component, str) or not component.strip():
            raise ValueError("query_graph 需要当前组件 component")
        direction = a.get("direction", "both")
        lookup = {
            "incoming": f"c.svc={lit(component)}",
            "outgoing": f"p.svc={lit(component)}",
            "both": f"p.svc={lit(component)} OR c.svc={lit(component)}",
        }
        if direction not in lookup:
            raise ValueError("direction 只能取 incoming、outgoing 或 both")
        window = a.get("window", "abnormal")
        windows = ("normal", "abnormal") if window == "both" else (window,)
        refs, edges, reviews = [], [], {}
        for w in windows:
            self.index(w)
            source = "_idx_" + w
            conditions = lookup[direction]
            tfilter = self.t.time_filter(a.get(w + "_range") or a, "c.started")
            conditions = f"({conditions}) AND {tfilter}"
            group = " GROUP BY p.svc,c.svc ORDER BY p.svc,c.svc"
            sql = f"SELECT {self.stats()} " + self.join(source, conditions) + group
            rows, trunc = self.t.fetch(sql, self.t.limit(a, 80))
            query_key = (w, component, direction, tfilter)
            previous_edges, previous_cut = self.known_queries.get(query_key, (set(), True))
            self.known_queries[query_key] = (
                previous_edges | {(r["caller"], r["callee"]) for r in rows},
                previous_cut and trunc,
            )
            reviews[w] = {"returned_edges": len(rows), "truncated": trunc, "index_coverage": self.coverage[w]}
            public_source = None
            for row in rows:
                exact = f'p.svc={lit(row["caller"])} AND c.svc={lit(row["callee"])} AND {tfilter}'
                if public_source is None:
                    public_source = self.source_sql(w)
                public = (
                    f"WITH ix AS ({public_source}) SELECT {self.stats()} "
                    + self.join("ix", exact)
                    + group
                )
                ref = self.t.record(
                    "trace",
                    public,
                    [row],
                    False,
                    {"component": component, "window": w, "basis": "unique_cross_service_parent_child"},
                )
                refs.append(ref)
                neighbor = row["callee"] if row["caller"] == component else row["caller"]
                edges.append(
                    {
                        **row,
                        "window": w,
                        "neighbor": neighbor,
                        "basis": "observed_parent_child",
                        "evidence_refs": [ref],
                        "neighbor_evidence": self.t.neighbor_context(neighbor),
                    }
                )
        for edge in edges:
            key = (edge["caller"], edge["callee"])
            edge["window_status"] = {}
            for w in ("normal", "abnormal"):
                scope = self.t.time_filter(a.get(w + "_range") or a, "c.started")
                eligible = []
                for owner, d in ((key[0], "outgoing"), (key[1], "incoming")):
                    for orient in ("both", d):
                        q = self.known_queries.get((w, owner, orient, scope))
                        if q is not None:
                            eligible.append(q)
                state = (
                    "observed"
                    if any(key in pairs for pairs, _ in eligible)
                    else (
                        "queried_no_match"
                        if any(not cut for _, cut in eligible)
                        else "incomplete" if eligible else "not_queried"
                    )
                )
                edge["window_status"][w] = state
        return {
            "edges": edges,
            "evidence_refs": refs,
            "windows": reviews,
            "truncated": any(r["truncated"] for r in reviews.values()),
            "note": "caller 调用 callee；游走方向、等待时间和故障影响方向另作判断。未匹配记录不是不存在依赖的证明。",
        }

    def operations_compare(self, a):
        """先对完整窗口聚合并对齐，再按服务轮次和变化排序；证据 SQL 只引用原表。"""
        if a.get("trace_id") or a.get("start") or a.get("end"):
            raise ValueError("operations_compare 不支持 trace_id/start/end；请用两窗各自的 range")
        if a.get("window", "both") != "both":
            raise ValueError("operations_compare 需要 window=both")
        inputs = []
        for w in ("normal", "abnormal"):
            bounds = a.get(w + "_range", {})
            if bounds is None:
                bounds = {}
            if not isinstance(bounds, dict) or not set(bounds) <= {"start", "end"}:
                raise ValueError("normal_range/abnormal_range 只能包含 start/end")
            predicates = [self.t.time_filter(bounds, "started"), "svc IS NOT NULL"]
            for key, col in (("component", "svc"), ("span_operation", "op"), ("span_kind", "span_kind")):
                if a.get(key):
                    predicates.append(f"{col}={lit(a[key])}")
            inputs.append(f"SELECT {lit(w)} AS w,* FROM ({self.source_sql(w)}) AS source WHERE " + " AND ".join(predicates))
        raw = " UNION ALL ".join(inputs)
        sql = f"""WITH source AS ({raw}), stats AS (
          SELECT w,svc,op,span_kind,COUNT(*) AS span_rows,
            COUNT(DISTINCT trace_id) AS independent_requests,COUNT(ms) AS valid_durations,
            AVG(ms) AS mean_ms,QUANTILE_CONT(ms,.95) AS p95_ms,
            COUNT(*) FILTER (WHERE NULLIF(TRIM(status_code),'') IS NOT NULL OR http_status IS NOT NULL) AS status_observations,
            COUNT(*) FILTER (WHERE lower(status_code) IN ('error','2','status_code_error') OR http_status>=500) AS error_count
          FROM source GROUP BY w,svc,op,span_kind),
        normal_stats AS (SELECT * FROM stats WHERE w='normal'),
        abnormal_stats AS (SELECT * FROM stats WHERE w='abnormal'),
        aligned AS (
          SELECT COALESCE(n.svc,b.svc) AS service,COALESCE(n.op,b.op) AS operation,
            COALESCE(n.span_kind,b.span_kind) AS span_kind,
            n.span_rows AS normal_span_rows,b.span_rows AS abnormal_span_rows,
            n.independent_requests AS normal_independent_requests,b.independent_requests AS abnormal_independent_requests,
            n.valid_durations AS normal_valid_durations,b.valid_durations AS abnormal_valid_durations,
            n.mean_ms AS normal_mean_ms,b.mean_ms AS abnormal_mean_ms,
            n.p95_ms AS normal_p95_ms,b.p95_ms AS abnormal_p95_ms,
            n.status_observations AS normal_status_observations,b.status_observations AS abnormal_status_observations,
            n.error_count AS normal_error_count,b.error_count AS abnormal_error_count,
            CAST(n.error_count AS DOUBLE)/NULLIF(n.status_observations,0) AS normal_error_rate,
            CAST(b.error_count AS DOUBLE)/NULLIF(b.status_observations,0) AS abnormal_error_rate,
            CASE WHEN n.span_rows IS NULL THEN 'missing_normal'
                 WHEN b.span_rows IS NULL THEN 'missing_abnormal'
                 WHEN n.valid_durations<5 OR b.valid_durations<5 THEN 'low_duration_samples'
                 ELSE 'comparable' END AS comparison_status
          FROM normal_stats n FULL OUTER JOIN abnormal_stats b
            ON n.svc=b.svc AND n.op IS NOT DISTINCT FROM b.op
            AND n.span_kind IS NOT DISTINCT FROM b.span_kind),
        changes AS (
          SELECT *,abnormal_mean_ms-normal_mean_ms AS mean_ms_delta,
            abnormal_p95_ms-normal_p95_ms AS p95_ms_delta,
            (abnormal_p95_ms-normal_p95_ms)/NULLIF(ABS(normal_p95_ms),0) AS p95_relative_change,
            abnormal_error_rate-normal_error_rate AS error_rate_delta,
            CASE WHEN normal_valid_durations IS NULL OR abnormal_valid_durations IS NULL THEN 'missing_window'
                 WHEN normal_valid_durations<20 OR abnormal_valid_durations<20 THEN 'low_samples'
                 ELSE 'sufficient_samples' END AS p95_sample_status FROM aligned),
        ordered AS (
          SELECT *,ROW_NUMBER() OVER (PARTITION BY service ORDER BY
            CASE WHEN comparison_status='comparable' THEN 0 ELSE 1 END,
            error_rate_delta DESC NULLS LAST,p95_relative_change DESC NULLS LAST,
            mean_ms_delta DESC NULLS LAST,operation,span_kind) AS service_rank FROM changes)
        SELECT * FROM ordered ORDER BY service_rank,
          CASE WHEN comparison_status='comparable' THEN 0 ELSE 1 END,
          error_rate_delta DESC NULLS LAST,p95_relative_change DESC NULLS LAST,
          mean_ms_delta DESC NULLS LAST,service,operation,span_kind"""
        details = {
            "tool": "analyze_traces", "operation": "operations_compare", "window": "both",
            "component_filter": a.get("component"),
            "note": "先完整聚合对齐再截断；每服务先展示一条。错误率分母为有状态字段的 Span，"
                    "不是业务请求失败率；低样本、无对照和零基线不能直接判为根因。"
                    "服务端 Span 大幅减少后的少量幸存记录无错，不能证明服务健康；"
                    "应对照调用方失败、请求量与采集覆盖，区分停服、负载下降和采集缺失。",
        }
        return self.t._query_result("trace", sql, self.t.limit(a), details)

    def traces(self, a):
        op = a.get("operation", "operations")
        if op == "operations_compare":
            return self.operations_compare(a)
        w = a.get("window", "abnormal")
        if w == "both":
            if op not in ("operations", "pairs") or a.get("trace_id"):
                raise ValueError("两窗比较只用于 operations/pairs；指定 trace_id 需选择实际所属窗口")
            results = {}
            for window in ("normal", "abnormal"):
                args = {**a, "window": window}
                bounds = a.get(window + "_range")
                if bounds is not None:
                    if not isinstance(bounds, dict) or not set(bounds) <= {"start", "end"}:
                        raise ValueError("normal_range/abnormal_range 只能包含 start/end")
                    args.pop("start", None)
                    args.pop("end", None)
                    args.update(bounds)
                results[window] = self.traces(args)
            return {
                "status": "ok" if any(v["rows"] for v in results.values()) else "no_observations",
                "kind": "trace", "windows": results,
                "rows": [{**row, "window": window} for window, v in results.items() for row in v["rows"]],
                "evidence_refs": [ref for v in results.values() for ref in v["evidence_refs"]],
                "truncated": any(v["truncated"] for v in results.values()),
                "details": {"window": "both", "operation": op},
                "note": "两窗分别执行、分别保存证据；rows.window 仅标记展示来源，不写入原 SQL。limit 是每窗上限。",
            }
        if w not in ("normal", "abnormal"):
            raise ValueError("window 只能是 normal、abnormal 或 both")
        if op not in ("request", "pairs", "operations"):
            raise ValueError("operation 只能取 request、pairs 或 operations")
        if op == "request" and not a.get("trace_id"):
            raise ValueError("查看一条完整请求需要 trace_id")
        self.index(w)
        source = "_idx_" + w
        component = a.get("component")
        if op == "pairs":
            predicates = [self.t.time_filter(a, "c.started")]
            for field, key in [
                ("c.trace_id", "trace_id"),
                ("p.svc", "caller"),
                ("c.svc", "callee"),
                ("c.op", "span_operation"),
            ]:
                if a.get(key):
                    predicates.append(f"{field}={lit(a[key])}")
            if component:
                predicates.append(f"(p.svc={lit(component)} OR c.svc={lit(component)})")
            body = (
                """SELECT p.svc AS caller,c.svc AS callee,c.trace_id,p.span_id AS parent_span_id,
                  c.span_id AS child_span_id,p.op AS parent_operation,c.op AS child_operation,
                  p.span_kind AS parent_kind,c.span_kind AS child_kind,p.started AS parent_start,
                  c.started AS child_start,p.ms AS parent_ms,c.ms AS child_ms,c.status_code,c.http_status,
                  p.file_row AS parent_file_row,c.file_row AS child_file_row """
                + self.join("ix", " AND ".join(predicates))
                + " ORDER BY c.started,c.trace_id,c.span_id"
            )
        else:
            preds = [self.t.time_filter(a, "started")]
            for key, col in [
                ("trace_id", "trace_id"),
                ("component", "svc"),
                ("span_operation", "op"),
                ("span_kind", "span_kind"),
            ]:
                if a.get(key):
                    preds.append(f"{col}={lit(a[key])}")
            where = " WHERE " + " AND ".join(preds)
            if op == "request":
                body = (
                    """SELECT svc AS service,trace_id,span_id,parent_span_id,op AS operation,span_kind,
                   started,ms AS duration_ms,status_code,http_status,identity_count,file_row FROM ix"""
                    + where
                    + " ORDER BY started,span_id"
                )
            else:
                body = (
                    """SELECT svc AS service,op AS operation,span_kind,COUNT(*) AS span_rows,
                   COUNT(DISTINCT trace_id) AS independent_requests,COUNT(ms) AS valid_durations,
                   AVG(ms) AS mean_ms,QUANTILE_CONT(ms,.95) AS p95_ms,
                   COUNT(*) FILTER (WHERE lower(status_code) IN ('error','2','status_code_error') OR http_status>=500) AS error_count
                   FROM ix"""
                    + where
                    + " GROUP BY svc,op,span_kind ORDER BY span_rows DESC,svc,op,span_kind"
                )
        # 当前执行可读已建索引，交给评测器的 SQL 必须完全从原表重建同一关系。
        limit = self.t.limit(a)
        cold = f"WITH ix AS ({self.source_sql(w)}) " + body + f" LIMIT {limit}"
        hot = f"WITH ix AS (SELECT * FROM {source}) " + body + f" LIMIT {limit+1}"
        rows, trunc = self.t.fetch(hot, limit)
        return self.t.result("trace", cold, rows, trunc, {"window": w, "operation": op})
