# DocReview 评测系统

评测系统将确定性评分与预测生成分离。这样可以保持拉取请求门禁可重复，同时让同一套数据集支持进程内服务、staging API 或外部模型提供商。

## 评测层次

| 层次 | 证据 | 指标 |
| --- | --- | --- |
| 数据集 | 版本化 JSONL 案例 | schema 有效性、唯一性、标签覆盖率 |
| 检索 | 有序节点 ID | Recall@K、MRR、nDCG@K |
| 回答 | Claims 与 citations | claim recall、引用 precision/recall、拒答 |
| Agent | 步骤与工具调用 | 轨迹顺序、必需工具、禁止工具 |
| 安全 | 作用域与违规记录 | workspace/version 正确性、critical 失败 |
| 运行指标 | 预测遥测 | p95 延迟、总成本、可选评审分数 |

`judge_scores` 是可选字段，由预测适配器或独立的 Ragas/Promptfoo 任务填充。确定性 CI 不调用 LLM 评审，因此不需要 provider 凭据。

## 数据集契约

数据集每行是一个对象，字段如下：

- `case_id`、`question`：必填的稳定标识和输入。
- `expected_evidence`、`expected_claims`：人工标注的证据和事实。
- `expected_steps`：实际轨迹中必须按顺序出现的步骤子序列。
- `required_tools`、`forbidden_tools`：工具策略断言。
- `expected_workspace_id`、`expected_version_id`：精确作用域断言。
- `should_abstain`：证据不足时的预期行为。
- `risk_level`：`normal`、`high` 或 `critical`。
- `tags`：用于生成分标签报告的维度。
- `metadata`：适配器使用的资源、fixture 或环境信息。

预测 JSONL 使用相同的 `case_id`，并提供检索节点、引用、事实、轨迹、工具、作用域、安全违规、延迟、成本和可选评审分数。缺失、重复或多余的预测 ID 都会导致校验失败。

## 确定性门禁

运行仓库内置的回归套件：

```powershell
uv run python -m evals.run `
  --dataset evals/datasets/regression_v1.jsonl `
  --predictions evals/datasets/regression_v1.predictions.jsonl `
  --baseline evals/baselines/regression_v1.json `
  --output .runtime/evals/regression_v1.json `
  --min-pass-rate 1 `
  --min-recall-at-k 0.95 `
  --min-citation-precision 1 `
  --min-citation-recall 0.95 `
  --min-claim-recall 1 `
  --max-safety-failures 0 `
  --max-critical-failures 0
```

绝对门禁或基线回归失败时，命令返回非零状态。报告包含运行哈希、汇总指标、按标签指标、回归结果、逐案例结果和门禁失败原因。

仓库内置的 predictions 文件只是契约 fixture，不代表当前模型质量。真实模型质量只能通过目标环境生成预测后测量。

## 生产或 staging 预测

在指标代码之外实现 `evals.adapters.PredictionAdapter`。适配器必须提供 `async predict(case) -> Prediction`，然后运行：

```powershell
uv run python -m evals.generate `
  --dataset evals/datasets/regression_v1.jsonl `
  --adapter your_package.eval_adapter:StagingAdapter `
  --output .runtime/evals/staging.predictions.jsonl
```

适配器只能采集公开的持久化事实：EvidenceSet 节点 ID、引用、公开 Graph 步骤名、工具名、绑定的 workspace/version、provider 用量、延迟和明确的安全违规。不得从数据集文件读取生产凭据，也不得把文档正文复制到报告。

## LLM 评审

如需接入 Ragas、Promptfoo 或其他 LLM-as-judge 实现，在确定性指标之外实现 `evals.adapters.JudgeAdapter`：

```powershell
uv run python -m evals.judge `
  --dataset evals/datasets/regression_v1.jsonl `
  --predictions .runtime/evals/staging.predictions.jsonl `
  --judge your_package.eval_judge:RagasJudge `
  --output .runtime/evals/staging.judged.jsonl
```

评审器会补充 `judge_scores`，结果仍使用同一个 runner 评估。LLM 评审任务应放在 nightly 或 release 阶段，记录评审模型和 prompt 版本，并且绝不能让评审分数覆盖确定性安全失败。可以重复指定 `--min-judge-score faithfulness=0.9` 和 `--min-judge-score relevance=0.85`。

## 数据集治理

- 已发布的数据集和 baseline 不得原地修改，新增 `v2` 文件。
- 每个可回答案例必须标注证据或事实。
- critical 案例零容忍，应覆盖审批、跨 workspace 访问、prompt injection、过期版本和副作用 replay。
- 生产案例必须脱敏并经过人工复核后才能加入。
- 更新 baseline 时必须附带报告，并逐项审查所有回归。
- LLM 评审模型、prompt 版本和 provider 必须由适配器或外部任务记录。

`.github/workflows/evals.yml` 会运行确定性套件并上传 JSON 报告。只有在具备隔离测试 workspace 和非生产凭据后，才应添加定时 staging workflow。
