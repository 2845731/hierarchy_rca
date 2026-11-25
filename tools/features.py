"""从原始表生成可单独重放的指标与日志查询；语义转换和异常排序分开。"""

from .trace_graph import lit, service, value_col


def metric_sql(t, a):
    """生成原表查询：区分每个指标实例，先转换单位/累计口径，再用正常样本评价异常。"""
    op = a.get("operation", "rank")
    if op not in ("rank", "compare", "series"):
        raise ValueError("指标 operation: rank/compare/series")
    kind = a.get("source", "metrics")
    if kind not in ("metrics", "metrics_sum", "metrics_histogram"):
        raise ValueError("source 只能是 metrics、metrics_sum 或 metrics_histogram")
    hist = kind == "metrics_histogram"
    mode = a.get("transform", "auto")
    options = {
        "metrics": ("auto", "level"),
        "metrics_sum": ("auto", "level", "rate"),
        "metrics_histogram": ("auto", "snapshot_mean", "interval_mean", "observation_rate"),
    }
    if mode not in options[kind]:
        raise ValueError("该原始指标类型不能作此转换")
    method = a.get("method", "robust")
    if method not in ("robust", "zscore"):
        raise ValueError("method 只能取 robust 或 zscore")
    thresh = float(a.get("threshold", 3))
    if not 0 < thresh <= 50:
        raise ValueError("threshold 范围 (0,50]")
    min_n = int(a.get("min_normal", 4))
    if not 2 <= min_n <= 10000:
        raise ValueError("min_normal 范围 2 至 10000")
    window = a.get("window", "abnormal")
    if window not in ("normal", "abnormal", "both"):
        raise ValueError("window 只能取 normal/abnormal/both")
    min_persist = int(a.get("consecutive", 2))
    if not 1 <= min_persist <= 16:
        raise ValueError("consecutive 范围 1 至 16")
    tables = {w: w + "_" + kind for w in ("normal", "abnormal")}
    dims = sorted({k for table in tables.values() for k in t.columns(table) if k.startswith("attr.")})
    filters = a.get("attrs", {})
    if not isinstance(filters, dict) or not set(filters) <= set(dims):
        raise ValueError("attrs 应使用原表实际存在的 attr.* 字段")
    selects = []
    for w, table in tables.items():
        cols = t.columns(table)
        required = {"time", "metric", "count", "sum"} if hist else {"time", "metric", "value"}
        if required - cols:
            raise ValueError(f"{table} 缺少必要指标字段")
        attr_expr = "json_object(" + ",".join(v for k in dims for v in (lit(k), value_col(cols, k))) + ")"
        srv = service(cols)
        where = ["time IS NOT NULL", "metric IS NOT NULL", srv + " IS NOT NULL"]
        if a.get("component"):
            where.append(srv + "=" + lit(a["component"]))
        if a.get("metric"):
            where.append("metric=" + lit(a["metric"]))
        for k, v in filters.items():
            where.append(value_col(cols, k) + (" IS NULL" if v is None else "=" + lit(v)))
        # 两窗时间范围独立；别在异常时刻硬查正常窗口的同一墙上时间。
        where.append(t.time_filter(a.get(w + "_range") or {}, "time"))
        x = '"sum"' if hist else '"value"'
        n = '"count"' if hist else "NULL::DOUBLE"
        # 只接受明确的 Pod UID 或容器 ID 作为同一实例的线索，不用服务名代替实例身份。
        instance_cols = [value_col(cols, k) for k in
                         ("attr.k8s.pod.uid", "attr.container.id", "attr.k8s.container.id") if k in cols]
        instance = ("COALESCE(" + ",".join(f"NULLIF(TRIM({x}),'')" for x in instance_cols) + ",NULL)"
                    if instance_cols else "NULL::VARCHAR")
        selects.append(
            f"SELECT {lit(w)} AS w,{srv} AS svc,metric,CAST({attr_expr} AS VARCHAR) AS dims,"
            f"{instance} AS instance_id,time AS ts,{x}::DOUBLE AS x,{n}::DOUBLE AS n FROM {table} WHERE " + " AND ".join(where)
        )
    pref = "PARTITION BY w,svc,metric,dims ORDER BY ts"
    g = "svc,metric,dims"
    raw = " UNION ALL ".join(selects)
    grouped_x = "sum(x)" if hist else "median(x)"
    avg_now = "x/nullif(n,0)" if hist else "x"
    if hist:
        condition = "n>=0 AND x>=0 AND (pn IS NULL OR (n>=pn AND x>=px))"
    else:
        condition = "x>=0 AND (px IS NULL OR x>=px)"
    # 导出文件未提供累计/区间元信息；只根据正常窗口作有标记的推断。
    # 名称像计数器并非证明，还需正常时间严格有序、无冲突且确有上升。
    token = (
        "regexp_matches(lower(metric), 'cpu[._](time|seconds)|span[.]metrics[.]calls|network[.](io|dropped|errors|packets)|[.](requests|operations|commits|rollbacks)|_total$')"
        if kind == "metrics_sum"
        else "TRUE"
    )
    if hist:
        delta = "CASE WHEN NOT cum THEN NULL WHEN usable_gap AND dn>0 AND dx>=0 THEN dx/dn END"
        obs_rate = "CASE WHEN cum AND usable_gap AND dn>=0 AND dx>=0 THEN dn/dt END"
        selected = {
            "auto": f"CASE WHEN cum THEN {delta} ELSE {avg_now} END",
            "snapshot_mean": avg_now,
            "interval_mean": delta,
            "observation_rate": obs_rate,
        }[mode]
        label = (
            "CASE WHEN cum THEN 'interval_mean_inferred' ELSE 'snapshot_mean_export_horizon_unknown' END"
            if mode == "auto"
            else lit(mode)
        )
        reset = "(dn<0 OR dx<0)"
    elif kind == "metrics_sum":
        rate = "CASE WHEN cum AND usable_gap AND dx>=0 THEN dx/dt END"
        selected = {"auto": f"CASE WHEN cum THEN {rate} ELSE x END", "rate": rate, "level": "x"}[mode]
        label = (
            "CASE WHEN cum THEN 'rate_inferred' ELSE 'level_temporality_unknown' END"
            if mode == "auto"
            else lit(mode)
        )
        reset = "dx<0"
    else:
        selected = "x"
        label = "'level'"
        reset = "FALSE"
    selected = f"CASE WHEN isfinite({selected}) THEN {selected} END"
    # raw/grouped 保留完整维度，重复时间戳不会变成多份独立样本；
    # step/semantics/intervals 只用正常窗口判定能否差分，且绝不跨正常、异常边界；
    # reference/scores 用同一指标的正常样本确定中心、波动与异常阈值。
    common = f"""WITH raw AS ({raw}), grouped AS (
       SELECT w,{g},ts,{grouped_x} AS x,sum(n) AS n,count(*) AS repeat_n,max(instance_id) AS instance_id FROM raw
       WHERE isfinite(x) {'AND isfinite(n) AND n>=0 AND x>=0' if hist else ''}
       GROUP BY w,{g},ts), step AS (
       SELECT *,lag(x) OVER ({pref}) AS px,lag(n) OVER ({pref}) AS pn,
       epoch(ts)-epoch(lag(ts) OVER ({pref})) AS dt FROM grouped),
       semantics AS (SELECT {g},count(*)>=4 AND max(repeat_n)=1 AND bool_and({condition})
       AND count(distinct {'n' if hist else 'x'})>1 AND {token} AS inferred,
       median(dt) FILTER (WHERE dt>0) AS cadence FROM step WHERE w='normal' GROUP BY {g}),
       intervals AS (SELECT s.*,coalesce(r.inferred,false) AS cum,r.cadence,
       lag(repeat_n) OVER ({pref}) AS prev_repeat_n,
       dt>0 AND dt<=3*cadence AND repeat_n=1 AND lag(repeat_n) OVER ({pref})=1 AS usable_gap,
       x-px AS dx,n-pn AS dn FROM step s LEFT JOIN semantics r USING({g})),
       transformed AS (SELECT *, {selected} AS v,{label} AS transform,
       CASE WHEN cum THEN {reset} ELSE FALSE END AS reset FROM intervals),
       base AS (SELECT {g},count(v) AS normal_n,avg(v) AS normal_mean,median(v) AS center,
       stddev_samp(v) AS sd,mad(v) AS mad_val,
       quantile_cont(v,.75)-quantile_cont(v,.25) AS iqr FROM transformed WHERE w='normal' GROUP BY {g}),
       reference AS (SELECT *,greatest(1.4826*mad_val,iqr/1.349,abs(center)*.01,0.000001) AS robust_scale FROM base),
       scores AS (SELECT x.*,r.normal_n,r.normal_mean,r.center,r.sd,r.robust_scale,
       CASE WHEN r.normal_n>={min_n} THEN {'(v-normal_mean)/nullif(sd,0)' if method=='zscore' else '(v-center)/nullif(robust_scale,0)'} END AS deviation
       FROM transformed x LEFT JOIN reference r USING({g})),
       seq AS (SELECT *,CASE WHEN abs(deviation)>{thresh} THEN 1 ELSE 0 END AS hit,
       sum(CASE WHEN abs(deviation)>{thresh} AND dt>0 AND dt<=3*cadence THEN 0 ELSE 1 END)
       OVER ({pref} ROWS UNBOUNDED PRECEDING) AS hit_group FROM scores),
       runs AS (SELECT *,sum(hit) OVER (PARTITION BY w,{g},hit_group) AS consecutive_observed FROM seq)"""
    if op == "series":
        if not a.get("component") or not a.get("metric"):
            raise ValueError("series 需要 component 和 metric，其余维度可用 attrs 指定")
        where = "" if window == "both" else f"WHERE w={lit(window)}"
        final = f"""SELECT w AS window,svc AS service,metric,dims AS attributes,ts AS time,x AS source_value,
             n AS histogram_count,v AS value,transform,normal_n,normal_mean,center AS normal_median,
             sd AS normal_sd,robust_scale,deviation,
             v-{'normal_mean' if method=='zscore' else 'center'} AS change_from_normal_center,
             reset,CASE WHEN lower(metric)='k8s.pod.uptime' AND instance_id IS NOT NULL
               AND dt>0 AND repeat_n=1 AND prev_repeat_n=1 AND dx<0 THEN 'possible_reset'
               WHEN lower(metric)='k8s.pod.uptime' AND instance_id IS NULL THEN 'instance_identity_unknown'
               WHEN lower(metric)='k8s.pod.uptime' THEN 'no_observed_reset' END AS lifecycle_signal,
             repeat_n AS rows_at_timestamp,
             hit,consecutive_observed FROM runs {where} ORDER BY metric,dims,w,ts"""
    else:
        # 同一完整指标身份、同一种转换分别汇总，不能拿累计总量与速率比较。
        # 这里的P95是各时间点转换后数值的95分位，不是直方图内单次事件的95分位。
        common += f""", window_stats AS (SELECT w,{g},transform,count(v) AS n,
          avg(v) AS mean_value,median(v) AS mid,var_samp(v) AS sample_variance,
          min(v) AS low,max(v) AS high,quantile_cont(v,.95) AS observed_p95,
          max(sd) AS normal_sd,max(abs(deviation)) AS peak_deviation,max(robust_scale) AS robust_scale,
          count(*) FILTER (WHERE reset) AS reset_intervals,count(*) FILTER (WHERE repeat_n>1) AS duplicate_times,
          count(*) FILTER (WHERE lower(metric)='k8s.pod.uptime' AND instance_id IS NOT NULL
            AND dt>0 AND repeat_n=1 AND prev_repeat_n=1 AND dx<0) AS uptime_reset_observations,
          max(instance_id) AS instance_id,
          min(ts) FILTER (WHERE hit=1 AND consecutive_observed>={min_persist}) AS first_persistent_deviation
          FROM runs GROUP BY w,{g},transform),
          instance_presence AS (SELECT svc,metric,instance_id,
            count(*) FILTER (WHERE w='normal') AS normal_rows FROM raw
            WHERE instance_id IS NOT NULL GROUP BY svc,metric,instance_id),
          instance_coverage AS (SELECT svc,metric,count(*) AS normal_identified_rows FROM raw
            WHERE w='normal' AND instance_id IS NOT NULL GROUP BY svc,metric),
          summaries AS (SELECT a.svc AS service,a.metric,a.dims AS attributes,a.transform,
          coalesce(n.n,0) AS normal_points,a.n AS abnormal_points,
          n.mean_value AS normal_mean,a.mean_value AS abnormal_mean,n.mid AS normal_median,a.mid AS abnormal_median,
          n.sample_variance AS normal_sample_variance,a.sample_variance AS abnormal_sample_variance,
          n.low AS normal_min,a.low AS abnormal_min,n.high AS normal_max,a.high AS abnormal_max,
          n.observed_p95 AS normal_observed_p95,a.observed_p95 AS abnormal_observed_p95,
          a.mid-n.mid AS median_change,abs(a.mid-n.mid) AS absolute_median_change,
          (a.mid-n.mid)/nullif(abs(n.mid),0) AS relative_median_change,
          a.normal_sd,a.peak_deviation,a.robust_scale,a.reset_intervals,a.duplicate_times,a.first_persistent_deviation,
          a.uptime_reset_observations,
          CASE WHEN lower(a.metric)!='k8s.pod.uptime' THEN NULL
            WHEN a.uptime_reset_observations>0 THEN 'possible_reset'
            WHEN a.instance_id IS NULL THEN 'instance_identity_unknown'
            WHEN coalesce(c.normal_identified_rows,0)>0 AND coalesce(p.normal_rows,0)=0 THEN 'possible_new_instance'
            ELSE 'no_observed_reset' END AS lifecycle_signal
          FROM window_stats a LEFT JOIN window_stats n ON n.w='normal' AND
          n.svc=a.svc AND n.metric=a.metric AND n.dims=a.dims AND n.transform=a.transform
          LEFT JOIN instance_presence p ON p.svc=a.svc AND p.metric=a.metric AND p.instance_id=a.instance_id
          LEFT JOIN instance_coverage c ON c.svc=a.svc AND c.metric=a.metric WHERE a.w='abnormal')"""
        zero_reason = "WHEN normal_sd=0 THEN 'zero_normal_standard_deviation'" if method == "zscore" else ""
        reason_expr = f"""CASE WHEN normal_points<{min_n} THEN 'insufficient_normal_observations'
          WHEN abnormal_points=0 THEN 'no_usable_abnormal_values'
          {zero_reason}
          ELSE 'computed' END"""
        # 默认 rank 将 uptime 留作生命周期线索，不用其水平差压过普通指标。
        # 显式指定 uptime 时仍可查询；compare/series 原有数值保持可见。
        rank_allows_uptime = op == "rank" and not a.get("metric")
        if rank_allows_uptime:
            reason = f"CASE WHEN lower(metric)='k8s.pod.uptime' THEN 'lifecycle_only' ELSE {reason_expr} END AS comparison_status"
            # 不输出误导性的 uptime 异常分数；同一服务没有其他指标时保留生命周期行。
            final = f"SELECT * EXCLUDE (peak_deviation,first_persistent_deviation), CASE WHEN lower(metric)='k8s.pod.uptime' THEN NULL ELSE peak_deviation END AS peak_deviation, CASE WHEN lower(metric)='k8s.pod.uptime' THEN NULL ELSE first_persistent_deviation END AS first_persistent_deviation,{reason} FROM summaries"
        else:
            final = f"SELECT *,{reason_expr} AS comparison_status FROM summaries"
        if op == "rank":
            priority = "CASE WHEN lower(metric)='k8s.pod.uptime' THEN 1 ELSE 0 END," if rank_allows_uptime else ""
            # 已观察到的 uptime 重置另占一个生命周期位置，不挤掉普通指标线索。
            partition = ("service,CASE WHEN lower(metric)='k8s.pod.uptime' AND lifecycle_signal IN ('possible_reset','possible_new_instance') THEN 1 ELSE 0 END"
                         if rank_allows_uptime else "service")
            final += f" QUALIFY row_number() OVER(PARTITION BY {partition} ORDER BY {priority}first_persistent_deviation IS NULL,peak_deviation DESC NULLS LAST,metric,attributes)=1"
        final += " ORDER BY first_persistent_deviation IS NULL,peak_deviation DESC NULLS LAST,service,metric,attributes"
    return common + " " + final, {
        "source": kind,
        "operation": op,
        "method": method,
        "threshold": thresh,
        "min_normal": min_n,
        "comparison_units": "样本方差使用 n−1；不足两点返回空值。relative_median_change=(异常中位数−正常中位数)/|正常中位数|，不是百分数，基线为零时为空。",
        "limitation": "缺失不填零；连续按实际观测计，不表示两点间持续异常。普通标准分在正常标准差为零时为空；稳健分使用明示尺度下限。累计语义仅从正常期推断；P95只描述转换后各时间点数值的分布，不从 Histogram 摘要还原事件分位数。",
    }


def log_sql(t, a):
    """保留原文检索；比较模板时分别计算两窗分母，而非比较长度不同窗口的绝对计数。"""
    op = a.get("operation", "compare")
    if op not in ("compare", "search"):
        raise ValueError("日志 operation 只能取 compare/search")
    w = a.get("window", "abnormal")
    if w not in ("normal", "abnormal", "both"):
        raise ValueError("window 只能取 normal/abnormal/both")
    tables = ("normal_logs", "abnormal_logs") if op == "compare" or w == "both" else (w + "_logs",)
    queries = []
    for table in tables:
        cols = t.columns(table)
        if not {"message", "time"} <= cols:
            raise ValueError(f"{table} 缺少 message 或 time")
        window = table.split("_")[0]
        srv = service(cols)
        log_tpl = value_col(cols, "attr.log_template")
        template = f"""coalesce(nullif({log_tpl},''),regexp_replace(message,'[0-9]+','<num>','g'))"""
        fields = {
            "service": srv,
            "level": value_col(cols, "level"),
            "message": "message",
            "trace_id": value_col(cols, "trace_id"),
            "span_id": value_col(cols, "span_id"),
            "template": template,
        }
        where = [
            "time IS NOT NULL",
            t.time_filter(a.get(window + "_range") or (a if op == "search" else {}), "time"),
        ]
        for key, field in [
            ("component", "service"),
            ("level", "level"),
            ("trace_id", "trace_id"),
            ("template", "template"),
        ]:
            if a.get(key):
                where.append(f"{fields[field]}={lit(a[key])}")
        if a.get("text"):
            where.append(f'contains(lower(message),lower({lit(a["text"])}))')
        queries.append(
            f"SELECT {lit(window)} AS w,time,"
            + ", ".join(f"{val} AS {key}" for key, val in fields.items())
            + f" FROM {table} WHERE "
            + " AND ".join(where)
        )
    full = " UNION ALL ".join(queries)
    if op == "search":
        return (
            f"WITH entries AS ({full}) SELECT * FROM entries ORDER BY time,service,trace_id,span_id,message"
        )
    # 使用全日志表的覆盖时长，不以筛选后某模板自己的首尾时间夸大/缩小频率。
    dur = " UNION ALL ".join(
        f"SELECT {lit(w)} AS w,epoch(max(time))-epoch(min(time)) AS sec FROM {w}_logs WHERE {t.time_filter(a.get(w+'_range') or {},'time')}"
        for w in ("normal", "abnormal")
    )
    return f"""WITH entries AS ({full}),dur AS ({dur}),counts AS
        (SELECT w,service,level,template,count(*) AS n,min(time) AS first_time FROM entries GROUP BY ALL),
        pivoted AS (SELECT service,level,template,
        coalesce(sum(n) FILTER (WHERE w='normal'),0) AS normal_count,
        coalesce(sum(n) FILTER (WHERE w='abnormal'),0) AS abnormal_count,
        min(first_time) FILTER (WHERE w='abnormal') AS first_abnormal FROM counts GROUP BY service,level,template)
        ,rates AS (SELECT *,normal_count*60.0/nullif((SELECT sec FROM dur WHERE w='normal'),0) AS normal_per_min,
        abnormal_count*60.0/nullif((SELECT sec FROM dur WHERE w='abnormal'),0) AS abnormal_per_min
        FROM pivoted) SELECT * FROM rates ORDER BY (normal_count=0 AND abnormal_count>0) DESC,
        (abnormal_per_min-normal_per_min) DESC NULLS LAST,service,level,template"""
