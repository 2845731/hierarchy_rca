# V004 Lite Again

基于同级 `v004_from_original_baseline_lite_again` 的独立优化副本。源码、提示词、客户端、配置和运行结果均使用本目录；旧 V004 源文件不修改，旧 Result 未复制。

当前保留 MiniMax-M3.1-Flash、原主备配置、推理强度，以及全部调查、审核、补查和综合预算。默认仍按原配置运行全部案例、8 个案例进程、4 个共享 HTTP 请求槽。

## 运行

在本目录打开 PowerShell：

```powershell
python -B run.py
```

从任意目录运行：

```powershell
python -B "D:\GitHubMyReasearch\CausalRCA_Move_Edition\openrca_v2_causal hierarchy_RCA_codex\versions\v005_from_original_baseline_lite_again\run.py"
```

结果自动写入本目录 `Result/`，不会写入原 V004 的结果目录。日常设置修改本目录 `settings.py`；模型参数在本目录 `llm_config.py`，认证配置沿用本目录复制的配置文件。

离线评分：

```powershell
python -B evaluate.py
```

断点续跑仍沿用原功能，将路径替换为**本新项目**需要继续的批次目录：

```powershell
python -B run.py --resume --run-dir "本项目 Result 下的具体批次目录"
```

## 本次优化

| 项目 | 行为 | 作用 |
| --- | --- | --- |
| 同请求精确去重 | 完全相同的大对象保留一份完整内容，其他位置引用；完整对象仍在本次请求中 | 减少重复输入，不删证据、不做摘要 |
| 前缀缓存布局 | 稳定字段前置，变化状态和角色编号后置 | 争取提高服务端缓存命中；实际收益由接口返回量确认 |
| SQL 错误反馈 | 失败时直接附相关原表字段/类型和修正提示 | 减少猜列名、重复纠错；不会自动改写 SQL |
| 请求计量 | 分开记录排队时间、接口时间、服务端缓存和本地缓存 | 便于确认瓶颈和费用来源 |
| 审计写盘 | 完整提示词在开始和结束保存，中间只更新小状态记录 | 减少本地写盘及重复序列化 |

去重只修改模型输入的表示形式。状态和落盘证据仍使用原结构；模型输出仍必须通过原有证据、版本和语义校验。没有降低候选数、分支数、跳数、审核次数或补查预算，没有合并 Agent，没有删除停止前复核、family_review 或 SQL 证据重放。

## 对照开关

`settings.py` 新增：

```python
context_exact_dedup: bool = True
context_prefix_cache: bool = True
http_max_concurrent_requests: int = 4
```

前两个开关可分别关闭，以比较同模型、同案例、同预算的结果。关闭后恢复对应的原输入表示/字段顺序；SQL 错误提示和审计计量仍保留。HTTP 请求槽位使用本版本 `.runtime/request_slots`，不同版本同时运行时，各版本并发上限分别生效。

## 验证与限制

- `verification/offline_tests.txt`：离线单元/集成检查记录，真实 HTTP 被拦截。
- `verification/context_replay.json`：读取旧批次的已完成案例请求，逐条检查输入能否完整还原，并统计字符及 UTF-8 字节长度。
- `verification/source_integrity.json`：原源码和配置的完整性检查、核心流程未变检查。
- `LITE_AGAIN_CHANGES.md`：改动范围、计量定义和验证边界。

已有请求的离线回放可以用下列命令重做；它不调用模型、不执行遥测查询、不修改源运行结果：

```powershell
python -B verify_lightweight.py --run-dir "旧批次或原 V004 的运行目录"
```

输入还原检查不等于真实模型准确率评测。当前未发起付费模型调用，不能声明 EM/F1 持平、确定的 token 节省率或实际加速比例。正式比较时需纳入失败案例，保持相同模型/数据/预算，并单独检查根因、传播指标和失败率。

新项目独立于原 V004 目录；使用研究工作区的 `OpenRCA_v2_dataset` 和已安装 Python 依赖，未复制数据集或第三方环境。输出契约、离线评分模块、评分运行配置与时间网格配置已归入本版本。运行模型时读取本版本 `.env` 优先，缺失时回退到相应专用环境变量；模型参数来自本版本 `llm_config.py`。历史 `LITE_CHANGES.md` 是继承的旧版记录，本次变化以 `LITE_AGAIN_CHANGES.md` 为准。
