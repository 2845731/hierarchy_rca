"""原项目适配边界。模型参数在本项目配置，复用已有重试、审计和输出契约。"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from settings import BASELINE, PROJECT


def prepare_imports():
    """优先用当前环境；本机默认 Anaconda 与参考 .venv 同版本，可复用已装包。"""
    for root in (PROJECT,):
        if str(root) not in sys.path:
            sys.path.append(str(root))
    venv = BASELINE / ".venv"
    cfg = venv / "pyvenv.cfg"
    if os.name == "nt" and cfg.is_file():
        fields = dict(line.split(" = ", 1) for line in cfg.read_text().splitlines() if " = " in line)
        same_version = fields.get("version", "").split(".")[:2] == list(map(str, sys.version_info[:2]))
        if same_version:
            # 只追加同一 Python 主次版本的包目录，不修改全局环境，也不自动安装。
            packages = venv / "Lib/site-packages"
            if str(packages) not in sys.path:
                sys.path.append(str(packages))
            if Path(sys.prefix) == venv and not importlib.util.find_spec("pandas"):
                sys.path.append(str(Path(sys.base_prefix) / "Lib/site-packages"))


prepare_imports()


def use_installed_runtime(script):
    """供两个无参数入口使用。缺少分析库时切换到已有环境，不安装或修改任何包。"""
    if all(importlib.util.find_spec(name) for name in ("duckdb", "sqlglot", "pyarrow", "pandas")):
        return
    runtime = BASELINE / ".venv/Scripts/python.exe"
    if runtime.is_file() and runtime.resolve() != Path(sys.executable).resolve():
        raise SystemExit(subprocess.call([str(runtime), "-B", str(Path(script).resolve()), *sys.argv[1:]]))
    raise SystemExit("缺少分析依赖，请使用 README 指定的本机 Python 或安装 requirements.txt。")


def load_project_client():
    """用本项目参数组装原客户端，不复制重试循环，也不加载旧实验的模型配置。"""
    from openai import OpenAI
    from llm_config import PROVIDERS, load_credentials
    from clients import minimax_client

    secrets = load_credentials()
    # 原客户端的日志和审计共用这个脱敏集合；绕过旧加载器后仍须在此注册密钥。
    # 这两处内部接口的使用集中在本适配边界，不向 Agent 或其他模块扩散。
    with minimax_client._LOCK:
        minimax_client._SECRETS.update(secrets.values())
    clients = []
    for row in PROVIDERS:
        sdk = OpenAI(
            api_key=secrets[row["provider"]],
            base_url=row["base_url"],
            timeout=row["timeout_seconds"],
            max_retries=0,  # 只使用原客户端的显式重试，避免两层重试叠加。
        )
        clients.append(minimax_client.MiniMaxClient(
            sdk, row["model"], provider=row["provider"],
            extra_body=row["extra_body"], settings=row,
        ))
    return minimax_client.FallbackClient(clients, max_rounds=1)


class Model:
    """每案例创建独立客户端；请求槽默认仍跨实验共享，避免同账号并发叠加。"""

    def __init__(self, output_dir, case_name, attempt_id, cfg=None):
        if cfg is None:
            from settings import CONFIG
            cfg = CONFIG
        limit = getattr(cfg, "http_max_concurrent_requests", 4)
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("http_max_concurrent_requests 必须是 1 至 64 的整数")
        self.context_prefix_cache = getattr(cfg, "context_prefix_cache", False)
        os.environ.update(
            PEARLRCA_CASE_NAME=case_name,
            PEARLRCA_RUN_ATTEMPT_ID=attempt_id,
            PEARLRCA_LLM_AUDIT_DIR=str(output_dir / "llm_calls"),
            PEARLRCA_LLM_CACHE_DIR=str(output_dir / "llm_cache"),
            PEARLRCA_LLM_SLOT_DIR=str(PROJECT / ".runtime/request_slots"),
            PEARLRCA_LLM_MAX_CONCURRENT_REQUESTS=str(limit),
        )
        self.client = load_project_client()

    def call(self, system: str, user: str, *, agent_id: str) -> dict:
        from clients.json_output import parse_model_json

        # 仲裁只在各调查线程返回后执行；原客户端据阶段选用输出长度。
        os.environ["PEARLRCA_RUN_STAGE"] = "meta" if agent_id.startswith(("meta", "synthesis")) else "investigation"
        try:
            role = f"当前角色编号：{agent_id}"
            request_user = f"{user}\n{role}" if self.context_prefix_cache else f"{role}\n{user}"
            text = self.client.chat(system, request_user)
        except RuntimeError as exc:
            # 原客户端会拒绝残缺的 <think> 标签。缺失最终答案属于本轮输出格式错误：
            # 让该角色在自己的调用预算内纠正，不读思考正文，也不把它升级成通道故障。
            malformed = {
                "LLM 思考内容未完整结束；不能将其当成最终答案",
                "LLM 只返回了思考内容，没有最终答案",
                "LLM 返回了空内容",
            }
            if str(exc) in malformed:
                raise ValueError("未得到完整最终 JSON，请只返回一个完整对象") from exc
            raise  # 接口耗尽、拒绝和其他执行故障仍按原客户端政策处理。
        result = parse_model_json(text)
        if not isinstance(result, dict):
            raise ValueError("模型必须返回单个 JSON 对象")
        json.dumps(result, allow_nan=False)  # JSON 不允许 NaN/Infinity；交回角色按格式纠正。
        return result
