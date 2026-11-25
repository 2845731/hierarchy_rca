"""只读回放已有请求；不调用模型或 SQL。字符/字节统计不能代替 token 和准确率评测。"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from agents.context_transport import expand_context, is_encoded_context
from agents.model_input import context_system_instruction, model_context
from filesystem import filesystem_path
from settings import CONFIG, PROJECT


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":"))


def replay(run_dir):
    run_dir = Path(run_dir).resolve()
    summary = json.loads(filesystem_path(run_dir / "summary.json").read_text(encoding="utf-8-sig"))
    completed = [row for row in summary.get("cases", [])
                 if row.get("status") == "completed" and row.get("has_answer")]
    totals, roles = defaultdict(int), defaultdict(lambda: defaultdict(int))
    skipped, cases = [], []
    for case in completed:
        folder = (run_dir / case["case"]).resolve()
        if folder.parent != run_dir:
            raise ValueError("案例目录越过批次边界")
        cases.append(case.get("case_handle", case["case"]))
        for path in sorted(filesystem_path(folder / "llm_calls").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            user, system = record.get("user", ""), record.get("system", "")
            if not user.startswith("当前角色编号：") or "\n" not in user:
                skipped.append({"file": path.name, "reason": "not_source_v004_request_layout"})
                continue
            role_line, body = user.split("\n", 1)
            role = role_line.split("：", 1)[1].split(":", 1)[0]
            start = body.find("{")
            if start < 0:
                raise ValueError(f"{path.name}: missing context JSON")
            payload, end = json.JSONDecoder().raw_decode(body[start:])
            prepared = model_context(payload, CONFIG)
            encoded = is_encoded_context(prepared)
            serialized = dump(prepared)
            restored = expand_context(json.loads(serialized), encoded=encoded)
            # 最外层字段可重排，嵌套键序、数组顺序及所有字段值必须保持。
            if set(restored) != set(payload) or any(dump(restored[k]) != dump(v) for k, v in payload.items()):
                raise AssertionError(f"{case.get('case_handle')}/{path.name}: payload changed")
            rendered = body[:start] + serialized + body[start + end:]
            new_user = rendered + "\n" + role_line if CONFIG.context_prefix_cache else role_line + "\n" + rendered
            new_system = system + context_system_instruction(CONFIG, prepared)
            row = {
                "calls": 1, "encoded_calls": int(encoded), "roundtrip_matches": 1,
                "before_input_chars": len(system) + len(user),
                "after_input_chars": len(new_system) + len(new_user),
                "before_input_utf8_bytes": len(system.encode("utf-8")) + len(user.encode("utf-8")),
                "after_input_utf8_bytes": len(new_system.encode("utf-8")) + len(new_user.encode("utf-8")),
            }
            for key, value in row.items():
                totals[key] += value
                roles[role][key] += value

    def finish(row):
        row = dict(row)
        for unit in ("chars", "utf8_bytes"):
            before, after = row.get(f"before_input_{unit}", 0), row.get(f"after_input_{unit}", 0)
            row[f"input_{unit}_reduction_percent"] = round(100 * (before - after) / before, 3) if before else None
        return row

    return {
        "created_utc": datetime.now(timezone.utc).isoformat(), "source_run": str(run_dir),
        "case_snapshot": cases, "completed_cases": len(cases),
        "settings": {"context_exact_dedup": CONFIG.context_exact_dedup, "context_prefix_cache": CONFIG.context_prefix_cache},
        "totals": finish(totals), "roles": {role: finish(row) for role, row in sorted(roles.items())},
        "skipped": skipped,
        "limitations": [
            "Replays existing successful-case requests only; no new model or SQL calls.",
            "Sizes include system/user text and added instruction, not API wrappers.",
            "Character/UTF-8 byte reduction is not token, cost, latency or accuracy measurement.",
            "Roundtrip proves representation equality, not subsequent model behavior.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "verification/context_replay.json")
    args = parser.parse_args()
    result = replay(args.run_dir)
    target = filesystem_path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"completed_cases": result["completed_cases"], **result["totals"],
                      "skipped": len(result["skipped"]), "report": str(args.output)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
