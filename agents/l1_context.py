"""共用工具查询记录，以及 L1 的持久调查摘要；只保存事实，不决定下一跳。"""

import copy

from state import refs_in


def short_text(value, limit=240):
    """长 SQL/日志只作带省略标记的预览；原文仍保存在工具结果和事件日志中。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…[已省略]"
    if isinstance(value, dict):
        return {k: short_text(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [short_text(v, limit) for v in value]
    return value


class QueryHistory:
    """各会话独立保存实际工具结果；成功结果可复用，首次工具失败允许一次重试。"""

    def __init__(self):
        self.queries = {}
        self._summary_cache = {}

    @staticmethod
    def _cacheable(value):
        if type(value) in (str, int, float, bool, type(None)):
            return True
        if type(value) is list:
            return all(QueryHistory._cacheable(item) for item in value)
        if type(value) is dict:
            return all(
                type(key) in (str, int, float, bool, type(None)) and QueryHistory._cacheable(item)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def _derived(cache, key, compute, source):
        if key in cache:
            return copy.deepcopy(cache[key])
        value = compute()
        try:
            cacheable = QueryHistory._cacheable(value) and QueryHistory._cacheable(source())
        except RecursionError:
            cacheable = False
        if cacheable:
            cache[key] = copy.deepcopy(value)
        return value

    def remember(self, signature, tool, args, result, agent_id, question=""):
        """仅在工具实际执行后登记。失败也保留原状态，不改写成空观测。"""
        old = self.queries.pop(signature, None)
        self._summary_cache.pop(signature, None)
        self.queries[signature] = {
            "query_no": old["query_no"] if old else len(self.queries) + 1,
            "source_agent": agent_id,
            "tool": tool,
            "args": copy.deepcopy(args),
            "result": copy.deepcopy(result),
            "attempts": old["attempts"] + 1 if old else 1,
            "reuse_count": old["reuse_count"] if old else 0,
            "agent_question": question if isinstance(question, str) else "",
        }

    def reusable(self, signature):
        record = self.queries.get(signature)
        if record and (
            record["result"].get("status") in {"ok", "no_observations"} or record["attempts"] >= 2
        ):
            return record
        return None  # 工具首次失败时仍允许一次实际重试。

    def query_summary(self):
        """保留全部查询索引与近期少量样本，模型笔记不视为遥测事实。"""
        queries, samples = [], []
        for signature, record in self.queries.items():
            result = record["result"]
            cache = self._summary_cache.setdefault(signature, {})
            item = {
                "query_no": record["query_no"], "tool": record["tool"],
                "args_preview": self._derived(
                    cache, "args_preview", lambda: short_text(record["args"]), lambda: record["args"]
                ),
                "status": result.get("status"),
                "evidence_refs": self._derived(cache, "evidence_refs", lambda: sorted(refs_in(result)), lambda: result),
                "reuse_count": record["reuse_count"],
                "agent_question": self._derived(
                    cache, "agent_question", lambda: short_text(record["agent_question"]),
                    lambda: record["agent_question"]
                ),
                "truncated": bool(result.get("truncated", False)),
            }
            for field in ("rows", "edges", "services", "columns"):
                if isinstance(result.get(field), list):
                    item[field + "_count"] = len(result[field])
            if result.get("error"):
                item["error"] = self._derived(cache, "error", lambda: short_text(result["error"]), lambda: result)
            queries.append(item)
            if result.get("rows"):
                def rows_preview():
                    if result.get("details", {}).get("window") == "both" and "windows" in result:
                        rows = [{**r, "window": window} for window, w in result["windows"].items()
                                for r in w.get("rows", [])[:2]]
                    else:
                        rows = result["rows"][:2]
                    return short_text(rows)

                samples.append({"query_no": record["query_no"], "evidence_refs": item["evidence_refs"],
                                "rows_preview": self._derived(cache, "seed_rows_preview", rows_preview, lambda: result),
                                "preview_only": True})
        return {"queries": queries, "recent_observations": samples[-6:],
                "note": "查询索引保留全部已执行范围；样本不是完整结果。agent_question 是调查问题，不是已核实事实。"}


class L1Context(QueryHistory):
    def __init__(self):
        super().__init__()
        self.notes = []

    def summary(self, chain, neighbors):
        """每次模型调用都重建当前链尾摘要；完整查询索引不随最近八条历史被移走。"""
        tail = chain["nodes"][-1]
        calls = [copy.deepcopy(fact) for pair, facts in neighbors.items() if tail in pair for fact in facts]
        index, graph_queries, samples = [], [], []
        for signature, record in self.queries.items():
            result, args = record["result"], record["args"]
            cache = self._summary_cache.setdefault(signature, {})
            query = {
                "query_no": record["query_no"],
                "source_agent": record["source_agent"],
                "tool": record["tool"],
                "args_preview": self._derived(cache, "args_preview", lambda: short_text(args), lambda: args),
                "status": result.get("status"),
                "evidence_refs": self._derived(cache, "evidence_refs", lambda: sorted(refs_in(result)), lambda: result),
                "reuse_count": record["reuse_count"],
                "agent_question": self._derived(
                    cache, "agent_question", lambda: short_text(record["agent_question"]),
                    lambda: record["agent_question"]
                ),
            }
            if "rows" in result:
                query["returned_rows"] = len(result["rows"])
            if "edges" in result:
                query["returned_edges"] = len(result["edges"])
            query["result_truncated"] = bool(result.get("truncated", False))
            if result.get("error"):
                query["error"] = self._derived(cache, "error", lambda: short_text(result["error"]), lambda: result)
            index.append(query)

            # 负观测绑定查询范围：小窗口查不到，不能解释成整个异常期不存在调用。
            if record["tool"] == "query_graph" and (
                args.get("component") == tail
                or any(tail in (edge["caller"], edge["callee"]) for edge in result.get("edges", []))
            ):
                graph_queries.append(
                    {
                        "query_no": record["query_no"],
                        "scope": copy.deepcopy(args),
                        "status": result.get("status"),
                        "windows": {
                            window: {k: info[k] for k in ("returned_edges", "truncated") if k in info}
                            for window, info in result.get("windows", {}).items()
                        },
                        "evidence_refs": self._derived(
                            cache, "evidence_refs", lambda: sorted(refs_in(result)), lambda: result
                        ),
                    }
                )
            if result.get("rows"):
                preview = self._derived(
                    cache, "l1_rows_preview", lambda: short_text(result["rows"][:2]), lambda: result
                )
                samples.append(
                    {
                        "query_no": record["query_no"],
                        "evidence_refs": self._derived(
                            cache, "evidence_refs", lambda: sorted(refs_in(result)), lambda: result
                        ),
                        "rows_preview": preview,
                        "preview_truncated": (
                            len(result["rows"]) > 2
                            or bool(result.get("truncated", False))
                            or preview != result["rows"][:2]
                        ),
                    }
                )
        return {
            "chain_id": chain["chain_id"],
            "revision": chain["revision"],
            "nodes": list(chain["nodes"]),
            "current_component": tail,
            "observed_calls": calls,
            "graph_queries": graph_queries,
            "queries": index,
            "recent_observations": samples[-6:],
            "observation_sample_query_count": len(samples),
            "agent_notes": copy.deepcopy(self.notes[-4:]),
            "note_count": len(self.notes),
            "note": (
                "observed_calls 是实际调用方向，不是已确认的传播方向。graph_queries 保留查询范围与截断状态；"
                "未查询、查询未匹配、查询失败不能混同。queries 是全部已执行查询的索引；"
                "recent_observations 只展示最近六份非空查询各两行，不能视作完整数据。"
                "agent_question、agent_notes 是 L1 提出的问题和解释，不是程序核实的观测。"
            ),
        }
