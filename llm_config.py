"""本项目的主备模型配置。公开参数放这里，密钥放本项目 .env 或专用环境变量。"""

import os
from urllib.parse import urlsplit, urlunsplit

from settings import PROJECT


# 顺序就是调用优先级；备用只接管失败的这一次调用，下次仍从主接口开始。
# max_retries 是首次失败后额外重试的次数，不包含首次请求。
PROVIDERS = (
    {
        "provider": "minimax_ye",
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "RCA_MINIMAX_API_KEY_ye",
        "timeout_seconds": 600,
        "max_retries": 3,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 32,
        "temperature": None,
        "investigation_max_tokens": 131072,  # Seed、L1、L2 单次输出上限。
        "synthesis_max_tokens": 131072,  # Meta 仲裁单次输出上限，字段名沿用现有客户端。
        "extra_body": {
            "reasoning_split": True,
            "reasoning_effort": "low",
        },
    },
    {
        "provider": "minimax",
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "RCA_MINIMAX_API_KEY",
        "timeout_seconds": 600,
        "max_retries": 3,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 32,
        "temperature": None,
        "investigation_max_tokens": 131072,  # Seed、L1、L2 单次输出上限。
        "synthesis_max_tokens": 131072,  # Meta 仲裁单次输出上限，字段名沿用现有客户端。
        "extra_body": {
            "reasoning_split": True,
            "reasoning_effort": "low",
        },
    },
        {
        "provider": "minimax",
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "RCA_MINIMAX_API_KEY",
        "timeout_seconds": 600,
        "max_retries": 3,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 32,
        "temperature": None,
        "investigation_max_tokens": 131072,  # Seed、L1、L2 单次输出上限。
        "synthesis_max_tokens": 131072,  # Meta 仲裁单次输出上限，字段名沿用现有客户端。
        "extra_body": {
            "reasoning_split": True,
            "reasoning_effort": "low",
        },
    },
    {
        "provider": "minimax_ye",
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "RCA_MINIMAX_API_KEY_ye",
        "timeout_seconds": 600,
        "max_retries": 3,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 32,
        "temperature": None,
        "investigation_max_tokens": 131072,  # Seed、L1、L2 单次输出上限。
        "synthesis_max_tokens": 131072,  # Meta 仲裁单次输出上限，字段名沿用现有客户端。
        "extra_body": {
            "reasoning_split": True,
            "reasoning_effort": "low",
        },
    },
    {
        "provider": "minimax",
        "model": "MiniMax-M3.1-Flash",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "RCA_MINIMAX_API_KEY",
        "timeout_seconds": 600,
        "max_retries": 3,
        "retry_initial_seconds": 2,
        "retry_max_seconds": 32,
        "temperature": None,
        "investigation_max_tokens": 131072,  # Seed、L1、L2 单次输出上限。
        "synthesis_max_tokens": 131072,  # Meta 仲裁单次输出上限，字段名沿用现有客户端。
        "extra_body": {
            "reasoning_split": True,
            "reasoning_effort": "low",
        },
    },
)


def load_credentials():
    """只读本项目专用变量与 .env；不读取旧实验的密钥，不执行测试脚本。"""
    from clients.errors import LLMConfigurationError

    try:
        from dotenv import dotenv_values
    except ImportError:
        raise LLMConfigurationError("缺少 python-dotenv，无法读取本项目密钥；尚未调用接口。") from None
    try:
        local = dotenv_values(PROJECT / ".env", encoding="utf-8-sig", interpolate=False)
    except OSError:
        raise LLMConfigurationError("无法读取本项目 .env；请检查文件权限，尚未调用接口。") from None
    # 本版本 .env 优先；仅在本地未配置时回退到专用环境变量。
    secrets = {
        row["provider"]: (local.get(row["api_key_env"]) or os.environ.get(row["api_key_env"]) or "").strip()
        for row in PROVIDERS
    }
    missing = [row["api_key_env"] for row in PROVIDERS if not secrets[row["provider"]]]
    if missing:
        raise LLMConfigurationError(
            "主备接口必须都有密钥；请在本项目 .env 或环境变量中配置 " + "、".join(missing)
            + "；尚未调用接口。"
        )
    return secrets


def public_config_summary(config=None):
    """目录名称和运行记录共用实际请求的配置；此函数不读取或返回密钥。"""
    if config is None:
        from settings import CONFIG
        config = CONFIG
    rows = []
    for row in PROVIDERS:
        url = urlsplit(row["base_url"])
        host = url.hostname or ""
        if url.port:
            host += f":{url.port}"
        rows.append({
            "provider": row["provider"],
            "model": row["model"],
            "base_url": urlunsplit((url.scheme, host, url.path, "", "")),
            "timeout_seconds": row["timeout_seconds"],
            "max_retries": row["max_retries"],
            "temperature": row["temperature"],
            "investigation_max_tokens": row["investigation_max_tokens"],
            "synthesis_max_tokens": row["synthesis_max_tokens"],
            "extra_body": row["extra_body"],
            "retry_wait_seconds": [
                min(row["retry_initial_seconds"] * 2 ** n, row["retry_max_seconds"])
                for n in range(row["max_retries"])
            ],
        })
    return {
        "config_file": str(PROJECT / "llm_config.py"),
        "providers": rows,
        "routing_source": "project_llm_config",
        "fallback_policy": "only_current_call_then_return_to_primary",
        "api_error_policy": "stop_batch",
        "maximum_concurrent_http_requests": config.http_max_concurrent_requests,
        "context_exact_dedup": config.context_exact_dedup,
        "context_prefix_cache": config.context_prefix_cache,
        "price_note": "仅统计接口返回的用量，不推算费用",
    }
