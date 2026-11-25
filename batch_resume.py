"""已有批次的案例选择与安全归档；不读取数据集标签、不调用模型。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time

from filesystem import filesystem_path


_ACTIVE = {"running", "interrupted"}
_TERMINAL = {"completed", "partial", "insufficient_evidence", "technical_failure",
             "budget_exhausted", "investigation_stopped"}
_FAILED = {"technical_failure", "budget_exhausted", "investigation_stopped", "failed", "error"}
_RESERVED = {"_attempt_history", "_case_locks", "_sessions", ".batch.lock", "prompt_snapshot",
             "evaluation", "summary.json", "run.log", "run_metadata.json", "effective_config.json"}


def _read(path):
    try:
        return json.loads(filesystem_path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取批次记录 {path}: {exc}") from exc


def _is_reparse(path):
    path = filesystem_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_component(value, label):
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or Path(value).name != value or "/" in value or "\\" in value or ":" in value):
        raise ValueError(f"无效的{label}：{value!r}")
    return value


def _root(run_dir):
    path = Path(run_dir).absolute()
    if any(_is_reparse(part) for part in (path, *path.parents)) or not filesystem_path(path).is_dir():
        raise ValueError(f"批次目录不存在或是链接/重解析点：{path}")
    return path.resolve()


def _child(root, *parts):
    """只允许批次内的普通路径，拒绝中间目录和目标为链接或重解析点。"""
    target = root
    for part in parts:
        target = target / _safe_component(part, "路径名")
        if _is_reparse(target):
            raise ValueError(f"不能操作链接或重解析点：{target}")
    resolved = target.resolve(strict=False)
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"目标不在指定批次目录内：{target}")
    return target


def safe_child(run_dir, *components):
    """返回经过批次范围和重解析点检查的路径，用于会话日志及快照。"""
    return _child(_root(run_dir), *components)


def load_batch(run_dir):
    """读取原计划和汇总；验证唯一性和稳定编号，不对磁盘作任何修改。"""
    root = _root(run_dir)
    metadata = _read(_child(root, "run_metadata.json"))
    summary = _read(_child(root, "summary.json"))
    if not isinstance(metadata, dict) or metadata.get("pipeline") != "hierarchical_v1":
        raise ValueError("不是可恢复的 hierarchical_v1 批次")
    names = metadata.get("planned_cases")
    if (not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names)
            or len({n.casefold() for n in names}) != len(names)):
        raise ValueError("批次原始 planned_cases 为空、无效或重复")
    for name in names:
        _safe_component(name, "案例名")
        if name.lower() in _RESERVED:
            raise ValueError(f"案例名与批次内部路径冲突：{name}")
    if not isinstance(summary, dict) or not isinstance(summary.get("cases"), list):
        raise ValueError("summary.json 的 cases 不合法")
    if summary.get("planned") != len(names):
        raise ValueError("批次计划数量与原始案例列表不一致")
    rows = summary["cases"]
    seen = set()
    expected = {name: f"case_{i:04d}" for i, name in enumerate(names, 1)}
    handles = {handle.casefold(): name for name, handle in expected.items()}
    for name in names:
        if name.casefold() in handles and handles[name.casefold()] != name:
            raise ValueError(f"案例名与其他案例编号冲突：{name}")
    for row in rows:
        if not isinstance(row, dict) or row.get("case") not in expected:
            raise ValueError("汇总包含不属于原批次的案例")
        name = row["case"]
        if name in seen or row.get("case_handle") != expected[name]:
            raise ValueError(f"汇总案例重复或编号不一致：{name}")
        seen.add(name)
    return metadata, summary


def _case_state(root, name, handle, row):
    folder = _child(root, name)
    exists = filesystem_path(folder).exists()
    if exists and not filesystem_path(folder).is_dir():
        raise ValueError(f"案例路径不是目录：{folder}")
    manifest_path = _child(root, name, "run_manifest.json")
    process_path = _child(root, name, "process.json")
    manifest = _read(manifest_path) if filesystem_path(manifest_path).is_file() else None
    process = _read(process_path) if filesystem_path(process_path).is_file() else None
    if manifest is not None and (not isinstance(manifest, dict) or
            manifest.get("case") != name or manifest.get("case_handle") != handle):
        raise ValueError(f"案例 manifest 身份不一致：{name}")
    if process is not None and not isinstance(process, dict):
        raise ValueError(f"案例 process.json 非法：{name}")
    # 结案写入和汇总写入之间可能崩溃：仅在 manifest 与 process 一致时恢复结案记录。
    if manifest and manifest.get("status") in _TERMINAL and process:
        if (manifest.get("status"), manifest.get("has_answer")) != (process.get("status"), process.get("has_answer")):
            if row and row.get("status") in _ACTIVE:
                # 结案文件分步写入时被中断，按正在运行的尝试从头重跑。
                return row, manifest, exists
            raise ValueError(f"案例 manifest 与 process 状态不一致：{name}")
        if row is None:
            row = dict(manifest)
        elif row.get("status") in _ACTIVE:
            # 刚标记新尝试但尚未归档旧目录时，旧 manifest 不能冒充新尝试的结果。
            session_id = row.get("session_id")
            if not session_id or manifest.get("session_id") == session_id:
                row = dict(manifest)
        elif (row.get("status"), row.get("has_answer")) != (manifest.get("status"), manifest.get("has_answer")):
            raise ValueError(f"案例汇总与 manifest 状态不一致：{name}")
    elif manifest and row and row.get("status") not in _ACTIVE and manifest.get("status") in _ACTIVE:
        raise ValueError(f"案例汇总与运行中的 manifest 冲突：{name}")
    if row and row.get("has_answer") is True:
        if not manifest or not process:
            raise ValueError(f"案例标记有答案，但 manifest 或 process 缺失：{name}")
        answer = _child(root, name, "agent_answer.json")
        prediction = _child(root, name, "prediction.json")
        if not filesystem_path(answer).is_file() or not filesystem_path(prediction).is_file():
            raise ValueError(f"案例标记有答案，但答案或预测文件缺失：{name}")
    return row, manifest, exists


def select_cases(run_dir, metadata, summary, *, resume=False, cases=None, status=None):
    """按原计划排序返回 (案例目录名, 原编号)。可靠结案在内存汇总中恢复。"""
    root = _root(run_dir)
    names = metadata["planned_cases"]
    expected = {name: f"case_{i:04d}" for i, name in enumerate(names, 1)}
    by_handle = {handle: name for name, handle in expected.items()}
    rows = {row["case"]: row for row in summary["cases"]}
    if sum((bool(resume), bool(cases), bool(status))) != 1:
        raise ValueError("请选择断点继续、指定案例或指定状态中的一种模式")
    wanted = set()
    if cases:
        for item in cases:
            name = by_handle.get(item, item)
            if name not in expected:
                raise ValueError(f"指定案例不属于该批次：{item}")
            if name in wanted:
                raise ValueError(f"重复指定案例：{item}")
            wanted.add(name)
    selected = []
    for name in names:
        row = rows.get(name)
        if not resume and cases and name not in wanted:
            continue
        recovered, manifest, exists = _case_state(root, name, expected[name], row)
        if recovered is not row and recovered is not None:
            if row is not None:
                summary["cases"][summary["cases"].index(row)] = recovered
            else:
                summary["cases"].append(recovered)
            rows[name] = recovered
            row = recovered
        if status and (not row or row.get("status") != status):
            continue
        if resume:
            if row and row.get("has_answer") is True:
                continue
            state = (row or {}).get("status") or (manifest or {}).get("status")
            if state and state not in _ACTIVE | _FAILED | {"insufficient_evidence"}:
                raise ValueError(f"案例无正式答案且状态无法自动恢复：{name} ({state})")
        selected.append((name, expected[name]))
    return selected


def archive_case(run_dir, name, session_id):
    """派发前仅移动该案例的目录，目标保留在同一批次内；绝不递归删除。"""
    root = _root(run_dir)
    _safe_component(name, "案例名")
    if name.lower() in _RESERVED:
        raise ValueError(f"案例名与批次内部路径冲突：{name}")
    source = _child(root, name)
    if not filesystem_path(source).exists():
        return None
    if not filesystem_path(source).is_dir():
        raise ValueError(f"案例路径不是目录：{source}")
    history = _child(root, "_attempt_history")
    session = _child(root, "_attempt_history", _safe_component(session_id, "尝试编号"))
    destination = _child(root, "_attempt_history", session_id, name)
    if filesystem_path(destination).exists():
        raise FileExistsError(f"历史归档已存在：{destination}")
    filesystem_path(history).mkdir(exist_ok=True)
    filesystem_path(session).mkdir(exist_ok=True)
    # 再次验证解析路径，确保两个目标都严格留在该批次内。
    source = _child(root, name)
    destination = _child(root, "_attempt_history", session_id, name)
    os.replace(filesystem_path(source), filesystem_path(destination))
    return destination


@contextmanager
def _file_lock(path, *, blocking=False):
    """系统锁在持有进程结束时自动释放；锁文件永久保留以保持同一锁对象。"""
    with filesystem_path(path).open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    # Windows 锁冲突通常是 EACCES/EDEADLK；其他错误及时报告。
                    if not blocking or getattr(exc, "errno", None) not in (13, 36):
                        raise RuntimeError(f"无法获取锁，可能已有运行进程：{path}") from exc
                    time.sleep(0.1)
                    stream.seek(0)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except OSError as exc:
                raise RuntimeError(f"无法获取锁，可能已有运行进程：{path}") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def batch_lock(run_dir):
    """同一批次只允许一个调度进程。"""
    path = _child(_root(run_dir), ".batch.lock")
    with _file_lock(path):
        yield


@contextmanager
def case_lock(run_dir, handle, *, blocking=False):
    """案例锁位于被归档目录之外，跨调度器与执行进程保持稳定。"""
    if not isinstance(handle, str) or not re.fullmatch(r"case_\d{4,}", handle):
        raise ValueError(f"案例编号无效：{handle!r}")
    root = _root(run_dir)
    lock_dir = _child(root, "_case_locks")
    filesystem_path(lock_dir).mkdir(exist_ok=True)
    path = _child(root, "_case_locks", handle + ".lock")
    with _file_lock(path, blocking=blocking):
        yield
