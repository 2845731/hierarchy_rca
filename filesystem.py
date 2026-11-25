"""结果文件的 Windows 长路径访问和同目录原子写入。"""
import os
from pathlib import Path
import uuid


def filesystem_path(path):
    """保持逻辑路径不变，仅在实际文件访问时使用扩展路径。"""
    path = Path(path)
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def atomic_write_text(path, text, encoding="utf-8"):
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (".write-" + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def preflight_result_path(run_dir, longest_case):
    """在本次批次目录内测试深层路径；只删除本次测试生成的空目录和文件。"""
    base = filesystem_path(Path(run_dir) / (".path-check-" + uuid.uuid4().hex))
    deep = base / ("c" * max(1, len(longest_case))) / "stages" / "l1" / "S99-C99"
    target = deep / "closure_999_insufficient_evidence.json"
    marker = "long-path-check"
    try:
        atomic_write_text(target, marker)
        if target.read_text(encoding="utf-8") != marker or not target.is_file():
            raise OSError("长路径检查写入内容不一致")
        if target.name not in [item.name for item in deep.iterdir()]:
            raise OSError("长路径检查目录遍历失败")
    except OSError as exc:
        raise OSError(f"结果目录长路径检查失败，尚未调用模型：{target}；{exc}") from exc
    finally:
        try:
            target.unlink(missing_ok=True)
            current = deep
            while current != base.parent:
                if current.exists():
                    current.rmdir()
                current = current.parent
        except OSError:
            pass
