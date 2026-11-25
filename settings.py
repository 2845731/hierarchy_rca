"""日常设置只改本文件，然后运行 python run.py。路径均相对于工程所在的研究目录。"""

from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
# 版本快照位于 项目/versions/版本名；原项目仍位于工作区的直接子目录。
WORKSPACE = PROJECT.parents[2] if PROJECT.parent.name == "versions" else PROJECT.parent
REFERENCE = PROJECT  # 保留兼容名称，运行资源和槽目录属于当前版本。
BASELINE = PROJECT  # 契约、评测代码和配置已复制到当前版本。
# 本轮使用过程指标错题集；case_names 为空时运行该数据集 cases 下的全部案例。
# DATASET = WORKSPACE / "OpenRCA_v2_dataset_process_level_case"
DATASET = WORKSPACE / "OpenRCA_v2_dataset"
# 每批预测及其离线评分保存在本工程的 Result 下，与启动时的工作目录无关。
RESULTS = PROJECT / "Result"
TRACE_BUCKET_SECONDS = 5  # 原参考配置的网格间隔，按版本独立维护。


@dataclass(frozen=True)
class Settings:
    # 指定案例文件夹名；空元组表示不按名称筛选。
    case_names: tuple[str, ...] = ()
    # 未指定 case_names 时，最多选取多少个案例；None 表示不限制。
    case_limit: int | None = None
    incident_description: str = ""  # 用户提供的故障现象；留空表示不额外提供文字描述。
    # 同时运行的案例数；每个案例都有自己独立的调查次数和预算。
    case_workers: int = 5

    # Lite Again 的传输优化可分别关闭，便于保持同模型、同预算进行对照。
    # 只去重同一次请求内完全一致的 JSON 对象；不删证据、不做模型摘要。
    context_exact_dedup: bool = True
    # 稳定内容前置、动态状态后置，帮助服务端自动前缀缓存；不省略字段。
    context_prefix_cache: bool = True
    # HTTP 请求槽总上限，与案例内 L1 并发无关。保持原值 4，不自动增加接口压力。
    http_max_concurrent_requests: int = 12

    # 首次 Seed 调查最多产生多少个候选起点；实际数量可能更少。
    initial_candidates: int = 4
    # 每个案例的候选起点总上限，包含首次 Seed 和后续补种。
    max_candidates: int = 8
    # 单个案例同时运行的 L1 调查任务数；不是整个批次的并发总数。
    l1_workers: int = 4
    # 每个 L1 调查族的累计链数上限，包含初始链、fork 分支及补查新建的子链。
    chains_per_l1: int = 10
    branch_frontier_enabled: bool = True  # 是否共享同一调查族发现的分支线索。
    branch_frontier_summary_limit: int = 12  # 每次呈现给 L1 的分支线索摘要条目上限。
    # 每条调查链最多延伸的边数，即服务之间最多走多少跳；起点本身不算一跳。
    max_hops: int = 8
    # 每条调查链最多交给 L2 审核多少次；包含首次审核及后续重新审核。
    reviews_per_chain: int = 300
    # L1 的 review 送审动作首次校验失败后，最多额外纠错多少次；不是 L2 审核次数。
    max_l1_review_retries: int = 5
    # 每个案例首次 Seed 之外，最多补种多少轮；例如 2 表示最多 1 次首次 + 2 轮补种。
    seed_rounds: int = 5

    # 每个案例所有阶段共用的工具调用总上限；预留额度也包含在此上限内。
    max_tool_calls: int = 400
    # 每个案例所有阶段共用的模型调用总上限；预留额度也包含在此上限内。
    max_model_calls: int = 200
    # 从模型总预算中预留给 Meta 和 Synthesis 的额度，包含下方综合预留，不是额外次数。
    meta_reserve: int = 40
    # Meta 的 select 结果首次校验失败后，最多额外纠错多少次；受剩余模型预算限制。
    max_meta_selection_retries: int = 8
    # Meta 决策时为 Synthesis 回查和正式答案预留的模型调用额度，属于 meta_reserve 的一部分。
    # 须至少等于 synthesis_lookup_max + synthesis_max_attempts；meta_reserve 还须多留至少 1 次给 Meta。
    meta_synthesis_reserve: int = 24

    # Synthesis 正式答案最多生成多少次，包含首次生成及校验失败后的重试；格式错误也计入。
    synthesis_max_attempts: int = 8
    # 每一轮 Synthesis 回查最多调用模型多少次，所有轮次仍共享案例总预算。
    synthesis_lookup_max: int = 8
    # 综合缺少根因时，最多额外将具体问题交回 Meta 多少次；每次都包含原始 reason。
    synthesis_max_meta_returns: int = 3
    # 每次综合回流最多派发多少项 Meta 补查；与常规 meta_followups 分开计数。
    synthesis_recovery_followups_per_round: int = 3
    # 最终答案首次提交校验失败后，最多额外纠错多少次；3 表示最多提交 4 次。
    synthesis_final_max_retries: int = 3
    # 以下三个值是 token 额度，不是调用次数；综合输入预算由上下文上限减去输出预留及余量。
    synthesis_context_limit: int = 1_000_000  # 模型上下文总上限，包含输入与输出。
    synthesis_output_reserve: int = 131_072  # 为单次综合输出预留的 token 数。
    synthesis_context_margin: int = 32_768  # 为消息封装和计数误差预留的 token 数。
    synthesis_tokenizer_file: str | None = None  # 可选 tokenizer.json 路径；None 表示不指定。

    # 从案例总预算中为补种及其后续 L1 调查预留额度；不是额外预算，0 表示不预留。
    reseed_model_reserve: int = 24  # 预留的模型调用次数。
    reseed_tool_reserve: int = 24  # 预留的工具调用次数。
    # 每个案例 Meta 最多派发多少次 followup；L1 补查和 Seed 补种共用此上限。
    # 派发给 Seed 时还受 seed_rounds、max_candidates 和剩余预算约束。
    meta_followups: int = 8
    progress_interval_seconds: int = 15  # 模型或工具长时间无进展时，终端提示的间隔秒数。
    # 非 Synthesis 阶段的模型回复 JSON 首次格式错误后，最多额外重试多少次；受模型预算限制。
    # Synthesis 的格式错误由 synthesis_max_attempts 或 synthesis_lookup_max 分别计数。
    max_json_retries: int = 8
    # 连续重复读取达到该次数后，停止继续查询，要求 Agent 根据已有证据作阶段决定。
    max_consecutive_repeated_queries: int = 5

CONFIG = Settings()
