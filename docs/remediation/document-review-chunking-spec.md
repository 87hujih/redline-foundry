# 文档审查结构化切块修改规格

**状态：** 待批准实施

**规格日期：** 2026-08-17

**目标配置档：** `docreview-review-structure-2026-08-17`

**交付原则：** 一个完整变更集直接实现目标状态；不交付临时 splitter、中间版本或功能残缺的过渡实现。

## 1. 决策

当前项目必须将固定字符、单层 chunk 改为以下唯一目标策略：

> 结构感知的父子双层切块：canonical AST 提供稳定结构和来源事实；父窗口保存完整审查语义；子块负责精准召回；检索命中子块后，在 ContextManifest 组装阶段扩展同一父窗口的有序来源节点。

最终实现必须同时完成：

1. 结构化解析和标题/条款层级保留；
2. tokenizer-aware 父窗口与子块生成；
3. 表格、列表、条款和普通正文的类型专用切块；
4. 完整 chunk metadata、稳定身份和来源映射；
5. lexical/semantic/rerank 的子块召回；
6. 有预算、可审计的父窗口扩展；
7. citation、Evidence、Patch 授权和 Workspace/Resource/Version 隔离；
8. 历史当前版本的受控重导入/重投影工具和完整离线验证。

只修改 `MAX_CHUNK_CHARS`、替换为通用 `RecursiveCharacterTextSplitter`、增加全局 overlap，或只改 embedding 输入，均不满足本规格。

## 2. 当前缺陷

当前链路存在以下确定性问题：

- `src/docreview/knowledge/chunking.py` 使用固定 `800` 字符，不按 embedding tokenizer 计算最终输入长度。
- 超限内容仅按 `\n\n` 拆分，但 `document/normalize.py` 使用单换行合并 block，正常导入的长 section 可能完全不切分。
- `document/ingestion.py` 将 section 内容折叠为单个 paragraph 节点，标题层级、列表、表格和原始 block 边界不足。
- Tika 客户端只请求 `text/plain`，DOCX/PDF 的标题样式、列表、表格和版面信息在进入 AST 前已经丢失。
- chunker 只记录当前标题，不保留完整 heading path，也不按 `TABLE`、`LIST`、`LIST_ITEM` 等类型处理。
- canonical commit 将 chunk `section_type` 固定为 `canonical`，并将 `metadata_json` 固定为 `{}`。
- evidence lexical SQL 只搜索 chunk content，未统一使用标题路径、条款编号和角色信息。
- `window_group_id` 当前等同整个 section，无法表达一个超长 section 中多个有界父窗口。
- 现有 golden 只覆盖短 Markdown，不覆盖单段超长文本、中文条款、表格、列表、页映射、局部 overlap 和父窗口扩展。

## 3. 范围

### 3.1 包含

- Markdown、TXT、DOC、DOCX、PDF、RTF、ODT 的导入结构归一化；
- canonical AST 的标题层级、条款、段落、列表、表格和 page/source mapping；
- canonical commit 派生的 `resource_sections` 与 `resource_chunks`；
- embedding 文本构造和 profile 绑定；
- Resource Search 与 EvidenceService 的召回、重排、窗口扩展和 citation；
- ContextManifest 中的窗口内容和逐节点 provenance；
- database-free 单元、golden、SQL contract 和行为评估；
- 经单独授权后执行的历史结构审计、重导入和 chunk 重投影能力。

### 3.2 不包含

- 修改公开 HTTP method/path、错误 DTO 或 SSE event/replay；
- 修改 canonical Patch operation 或 Commit/Outbox 事务边界；
- 使用 LLM 推断标题、生成摘要或决定 chunk 边界；
- 改写现有数据库 migration；
- 在 HTTP 读取请求中进行 lazy backfill、embedding 或 chunk 写入；
- 连接生产 PostgreSQL，或绕过数据库测试 fuse。

## 4. 不变量

最终实现必须保持：

- Workspace、Resource、Version 和 Principal scope 在 SQL 边界内生效；
- canonical AST、node hash、document hash 和 Patch hash 规则不变；
- `resource_chunks.content_hash` 继续表示 canonical source node hash，不改成 fragment hash；
- fragment 自身 hash 写入 `metadata_json.fragment_hash`；
- Canonical Commit 仍在原 Serializable transaction 中写完整 bundle 和 Outbox；
- embedding/provider I/O 不进入 Canonical Commit transaction；
- EvidenceSet 的现有必填字段和公开 ToolRuntime envelope 不删除、不改名；
- ContextManifest 是扩窗后模型上下文的不可变事实，不能在 replay 时从当前 chunks 重建；
- citation 必须能回到准确 version、node、source span 和 page mapping；
- 同一 AST、同一 profile、同一 tokenizer 必须产生字节级稳定的 chunks 和 metadata。

## 5. 术语

| 术语 | 定义 |
| --- | --- |
| 结构节点 | canonical AST 中的 heading、paragraph、list、list item、table 或 page 节点。 |
| 逻辑 section | 一个标题及其作用域内的内容，对应 `resource_sections`。 |
| 检索单元节点 | 不跨 canonical node 的最小可切分正文单元；一个 child chunk 只能绑定一个检索单元节点。 |
| 父窗口 | 同一逻辑 section 内、有界且语义完整的一组有序 child chunks。 |
| 子块 | 写入 `resource_chunks`、用于 lexical/semantic recall 的最小检索记录。 |
| embedding text | 标题路径、角色上下文和 child 原文的确定性序列化，仅用于 embedding/rerank。 |
| citation text | child 的原始来源文本，不包含为 embedding 添加的标题前缀或 overlap 前缀。 |
| 窗口扩展 | 命中 child 后，按 `window_group_id` 读取其有序兄弟 child，并作为独立 provenance item 加入 ContextManifest。 |

## 6. 目标数据流

```text
Upload bytes
  -> bounded format parser
  -> structured ParsedElement stream
  -> deterministic normalization
  -> hierarchical canonical AST + exact source/page mapping
  -> deterministic section/parent-window/child projection
  -> canonical transaction writes pending chunk facts
  -> projection worker computes embedding_text and embeddings
  -> child lexical/semantic recall
  -> fusion and child rerank
  -> bounded parent-window expansion
  -> immutable ContextManifest with per-node provenance
  -> review / Patch validation / citation
```

任何步骤都不得把模型输出作为文档结构事实。

## 7. 结构化解析契约

### 7.1 ParsedElement

平面 `Block(type, text, level)` 必须扩展为可表达以下字段的内部结构，名称可按现有风格调整，但语义不得缺失：

```text
element_type
text
level
attributes
children
source_start_offset
source_end_offset
start_line
end_line
page_mappings[]
quality_flags[]
```

支持的元素至少包括：`heading`、`paragraph`、`list`、`list_item`、`table`、`page_break`。

### 7.2 Markdown/TXT

- Markdown 必须保留 `#` 到 `######` 标题层级；超过 6 级不得当作合法 heading。
- 保留段落空行边界、连续列表、表格和 fenced code block；code block 可作为带 `block_type=code` 的 paragraph。
- TXT 先按显式空行形成 paragraph，再执行确定性的条款标题识别。
- 不得再把每个非空文本行直接等价为独立 paragraph。

### 7.3 中文条款识别

只有行首、长度和上下文同时满足规则时，才把纯文本行提升为 heading/clause。至少覆盖：

- `第...编`、`第...章`、`第...节`、`第...条`、`第...款`；
- `1`、`1.`、`1、`、`1.2`、`1.2.3` 等数字层级；
- `一、`、`（一）`、`(一)`、`1)`、`(1)` 等项级结构。

优先级固定为：显式 parser 样式/outline > Markdown heading > 编章节条款规则 > 数字层级 > 项级列表。项级结构默认是 list item，只有样式、频率和后续正文证明其为标题时才升级。

识别不得依赖 LLM、embedding 相似度、当前时间或外部服务。

### 7.4 Tika

- 现有 `HTTPXTikaClient.parse()` 的 `text/plain` 行为保留为 legacy compatibility method。
- 新增独立的 bounded structured parse method，单次请求使用 Tika XHTML 输出；生产结构化导入使用该方法，不对同一文件同时发 text/plain 和 XHTML 两次请求。
- XHTML parser 必须拒绝 DTD、ENTITY、外部资源和超出深度/元素/文本预算的输入。
- 映射 `h1-h6`、`p`、`ul/ol/li`、`table/thead/tbody/tr/th/td` 和可用 page metadata。
- parser 没有提供页码或版面时必须保留为空并添加 quality flag，不得推测页码。
- PDF 只有纯文本结构时仍执行确定性条款识别；不能把每一行当作独立 chunk。

### 7.5 Canonical AST

- Heading 节点按 level 构成父子层级，不再全部挂到 document root。
- 一个逻辑 clause/section 的连续普通正文保存在一个 paragraph 检索单元节点中，同时在 metadata 保存内部 block/source spans。
- List、ListItem、Table 使用现有 `NodeType`，不得降级成普通 paragraph。
- Page 是来源映射，不是默认语义边界；跨页条款可以保持同一节点。
- 每个 node 的 source offsets、line 和 page mapping 必须覆盖其真实来源范围。

## 8. 唯一 Chunk Profile

最终实现只提供一个新的生产目标 profile：

| 参数 | 固定值 | 语义 |
| --- | ---: | --- |
| `profile_id` | `docreview-review-structure-2026-08-17` | 持久化配置档身份。 |
| `child_target_tokens` | 384 | 正常 child 的 soft target。 |
| `child_hard_max_tokens` | 512 | 最终 embedding text 的不可突破上限。 |
| `child_min_tokens` | 96 | 低于该值时尝试与同源相邻片段合并。 |
| `parent_target_tokens` | 960 | 父窗口 soft target。 |
| `parent_hard_max_tokens` | 1440 | 父窗口上下文硬上限。 |
| `overflow_overlap_tokens` | 48 | 只用于同一 oversized atom 的连续拆分。 |
| `heading_context_max_tokens` | 96 | embedding text 中标题路径预算。 |
| `metadata_max_bytes` | 16384 | 单 chunk metadata JSON 上限。 |

这些限制必须使用与 `embedding_profile` 绑定的 tokenizer 计算，而不是字符数。tokenizer 的名称、版本和词表 hash 是 profile 的一部分。

生产装配在 tokenizer 缺失、词表 hash 不匹配、返回负数或无法满足 embedding profile 时必须 fail closed。`ModelTokenEstimator` 可以用于数据库无关测试和明确标记的兼容模式，但不能伪装成未知 embedding 模型的精确 tokenizer。

本规格不提供多个可切换的新策略；参数变更意味着新的正式规格和重新评估，不允许运行时任意调参。

## 9. 切块算法

### 9.1 标题路径

遍历 AST 时维护从文档标题到当前节点的 heading stack。每个 child 保存完整 `heading_path`，不得只保存最后一个 heading。

超出 `heading_context_max_tokens` 时从最上层中间标题开始压缩，但必须保留文档标题和最接近正文的 leaf heading。压缩规则必须确定性且记录 `heading_path_truncated=true`。

### 9.2 检索单元边界

child 不得跨越：

- canonical node；
- 逻辑 section/clause；
- table 与非 table；
- list 与非 list；
- 不同父窗口；
- 不同 source version。

页边界本身不是强制切块边界，但 page mapping 必须随片段正确裁剪。

### 9.3 普通正文和条款

对单个 paragraph/clause node 按以下优先级生成 atom：

1. parser 保存的原始 block 边界；
2. 段落边界；
3. 句号、问号、感叹号、分号及对应中英文标点；
4. 逗号或空白；
5. tokenizer boundary 强制拆分。

使用顺序 packing：

- 在不超过 `child_target_tokens` 时吸收下一个 atom；
- 加入下一个 atom 会超过 target 但不超过 hard max 时，如果当前 chunk 小于 `child_min_tokens`，允许吸收；
- 单个 atom 超过 hard max 时按上述下一级边界递归拆分；
- 最终回退必须按 tokenizer boundary 拆分并标记 `forced_token_split`；
- hard max 计算对象是完整 embedding text，不是 raw content。

### 9.4 小块合并

只允许在以下条件全部成立时合并：

- 相同 canonical node；
- 相同 heading path；
- 相同 chunk role；
- 相同父窗口候选；
- 合并后的 embedding text 不超过 hard max；
- 两块之间没有 table/list/type boundary。

优先向后合并；最后一个小块无法向后合并时才尝试向前合并。规则必须稳定，禁止根据并发完成顺序决定。

### 9.5 Overlap

Overlap 只应用于“同一个 atom 因超过 hard max 被迫拆分”的相邻 fragment：

- overlap 上限为 48 tokens；
- 不跨 paragraph、node、section、table row、list item 或父窗口；
- overlap 仅进入 embedding text 和 rerank text，不进入 citation raw content；
- metadata 记录来源 fragment order 和 overlap token count；
- 正常相邻 child 不使用 overlap。

### 9.6 父窗口

先生成 child，再在同一逻辑 section 内按顺序形成父窗口：

- 尽量不超过 `parent_target_tokens`；
- 不得超过 `parent_hard_max_tokens`；
- table、完整短 list、完整短 clause 优先保持在一个父窗口；
- 一个 child 自身已接近 parent hard max 时可以单独成窗；
- 父窗口不产生另一份不可追溯的摘要文本，其内容由有序 child 原文在 ContextManifest 组装时重建。

`window_group_id` 必须由以下输入确定性生成：

```text
document_id + canonical section node id + parent window order + chunk profile id
```

推荐格式为 `win_` 加 SHA-256 前 32 个十六进制字符。不得使用数据库随机 UUID 或当前时间。

### 9.7 表格

- Table 与相邻正文隔离，但共享所属 section 和 heading path。
- 表头、caption、row 顺序和 source/page mapping 必须保留。
- 按完整 row packing；一个 row 不得因普通 target 被拆开。
- table 跨 child 时，每个 embedding text 重复 caption/header；citation raw content 保存实际 rows，窗口组装时只输出一次 header。
- 单 row 超过 hard max 时，优先按 cell 拆分并重复 row key/header；最后才按 tokenizer boundary 强制拆分。
- `chunk_role=table`，metadata 记录 row start/end 和 header hash。

### 9.8 列表

- ListItem 是默认原子单元。
- 短的连续 list items 可以在同一 list node 内 packing。
- 单个 list item 超过 hard max 时按其内部句子拆分，重复稳定的 list marker 只用于 embedding text。
- `chunk_role=list`，metadata 保存 item start/end order。

### 9.9 Heading-only 与专用结构

- 有正文的 heading 不单独产生 chunk，标题通过 heading path 进入 embedding text。
- 没有任何正文且没有子 section 的 leaf heading 产生一个 `heading_only` child，避免目录项消失。
- 现有 project normalization 的 `project_name`、`tech_stack`、`project_description`、`project_work` 语义必须保留；它们使用同一 token/window/profile 规则，不走另一套 splitter。

## 10. 文本序列化

### 10.1 持久化 content

`resource_chunks.content` 保存 citation raw content：

- 不添加文档标题；
- 不添加 heading path；
- 不添加 overlap；
- 不添加模型生成摘要；
- 保持来源顺序和规范化后的标点/换行。

### 10.2 Embedding/Rerank text

确定性序列化为：

```text
<document title, if present>
<heading path, one heading per line>

<role-specific context such as table caption/header, if present>
<overlap prefix, only for forced split>
<citation raw content>
```

序列化器名称和版本属于 chunk profile。embedding token hard max 对这段完整文本生效。

Reranker 使用同一序列化器，但不得把 metadata JSON、source path、Workspace ID 或内部 hash 拼入模型文本。

## 11. 持久化映射

现有 Schema 足以实现目标状态，不新增表，不改写历史 migration。

### 11.1 `resource_sections`

- 每个逻辑 section/clause 一行；
- `section_key` 使用稳定 canonical section identity；
- `section_type` 使用真实类型，如 `document`、`section`、`clause`、`project`；
- `section_order` 为全局稳定顺序；
- `title` 为 leaf heading；完整路径进入 metadata；
- `content` 为 section 原始有序正文，不由 chunks 反向拼接产生；
- `summary` 只允许确定性来源摘要，例如显式 lead/abstract；没有则为空，禁止调用 LLM；
- `canonical_node_id` 指向 section heading 或无标题文档 root。

### 11.2 `resource_chunks`

| 列 | 目标语义 |
| --- | --- |
| `chunk_index` | 当前 profile 内、当前 version 的全局连续顺序，从 0 开始。 |
| `section_title` | leaf heading；无标题为 `全文`。 |
| `content` | citation raw content。 |
| `section_id` | 所属逻辑 section。 |
| `section_type` | 真实 section type，不再固定为 `canonical`。 |
| `chunk_role` | `section_body`、`clause_body`、`table`、`list`、`heading_only` 或保留的 project role。 |
| `window_group_id` | 稳定父窗口 ID。 |
| `order_in_section` | section 内 child 的连续顺序，从 1 开始。 |
| `page_start/page_end` | 由该 child 的 source spans 得出。 |
| `canonical_node_id` | 唯一检索单元 node。 |
| `content_hash` | canonical node hash，保持兼容。 |
| `chunk_profile` | 固定目标 profile ID。 |
| `embedding_profile` | 当前 embedding 配置档。 |
| `embedding_status` | `pending`、`ready` 或既有约束允许的失败状态。 |

### 11.3 `metadata_json`

每个 chunk 至少保存：

```json
{
  "schema_version": "1.0",
  "profile_id": "docreview-review-structure-2026-08-17",
  "tokenizer_profile": "<name/version/hash>",
  "heading_path": [
    {"node_id": "...", "level": 1, "text": "..."}
  ],
  "parent_window_order": 1,
  "child_order": 1,
  "chunk_role": "clause_body",
  "raw_token_count": 0,
  "embedding_token_count": 0,
  "fragment_hash": "sha256:...",
  "embedding_text_hash": "sha256:...",
  "boundary_reason": "paragraph|sentence|table_row|list_item|forced_token_split",
  "overlap_prefix_tokens": 0,
  "source_spans": [
    {
      "node_id": "...",
      "start_offset": 0,
      "end_offset": 0,
      "page_start": null,
      "page_end": null
    }
  ],
  "quality_flags": []
}
```

metadata 必须使用 canonical JSON、受 16 KiB 上限约束，并拒绝秘密值、原始二进制、base64 image、provider response 和未界定的大对象。

## 12. Commit 与 Projection

- Canonical Commit transaction 内只做确定性 AST -> section/chunk facts 投影，chunk 初始为 `embedding_status=pending`。
- Commit 必须一次调用 chunk builder 得到不可变 projection，section insert 和 chunk insert 复用同一结果，禁止各自重新计算。
- embedding worker 在事务外构造 embedding text 并调用 provider。
- 写回 embedding 时必须在事务内重新检查 workspace/resource/version、canonical content hash、chunk profile、embedding profile 和 fragment hash；任一变化都丢弃旧结果。
- 同一 profile 重放必须幂等；不同 profile 不能覆盖成同一事实而不留下 operator/projection 审计。
- projection failure 不能破坏已提交 canonical version；状态和可重试原因必须可观测。
- 对历史版本进行批准后的重投影时，只允许变更投影控制列：`canonical_documents.chunk_profile`、`canonical_documents.embedding_profile`、`canonical_documents.projection_status` 和 `resource_versions.embedding_profile`。这些列与目标 chunk 激活必须在同一短事务内切换，并写入既有 operator/projection 审计；`ast_json`、canonical schema/source format、document/node content hash、ResourceVersion content/source/version number 均不可变。

历史当前版本先执行结构完整性审计：

- AST 已保留足够 heading/list/table/source mapping，只是 chunk profile 过旧时，可以在同一 ResourceVersion 上执行 projection-only rebuild；
- AST 已在旧 text/plain/flat normalization 中丢失结构时，禁止修改原 `ast_json` 或 node facts。必须从受信原始上传文件重新运行 structured ingestion，并创建一个新的 ResourceVersion/canonical document；
- 新版本的幂等 identity 必须绑定 workspace、resource、旧 current version、原始文件 content hash、parser profile 和目标 chunk profile；
- 原始文件不存在、hash 不匹配或无法证明来源归属时，该 Resource 必须报告 `source_artifact_unavailable` 并停止，不能从旧 chunks、当前 retrieval 结果或 LLM 输出伪造结构；
- 重导入只增加新版本并保留旧版本、Patch Commit 和 citation 历史，不覆盖或删除旧事实。

## 13. 检索契约

### 13.1 Scope 和 Profile

lexical 与 semantic SQL 都必须同时限制：

```text
workspace_id
resource_id
resolved exact version_id
chunk_profile
embedding_profile where applicable
embedding model/dimensions/index version where applicable
```

active chunk profile 必须由 resolved canonical version 的 `canonical_documents.chunk_profile` 得出，不能由 request body、模型输出或任意 query 参数选择。semantic profile 同时与 `resource_versions.embedding_profile` 和 `canonical_documents.embedding_profile` 对账。历史目标 rows 在投影控制列原子切换之前不得被任何读路径选中。

不得先跨 scope 召回再在 Python 内过滤。

### 13.2 Lexical Recall

lexical document 固定为标题路径的有界序列化加 raw content。SQL 至少使用 `section_title + content`，并对以下精确命中给予稳定优先级：

- 条款编号；
- 金额、百分比、日期、版本号；
- section title；
- raw content 子串。

不得把 metadata JSON 整体加入 trigram 文本。

### 13.3 Semantic Recall

- embedding 只来自目标 profile 的 embedding text；
- 只召回 `embedding_status=ready` 且 profile/model/dimensions/index 完全匹配的 child；
- profile mismatch 必须 fail closed 或按现有 degradation contract 明确降级到 lexical，不能混用旧向量。

### 13.4 Fusion 与 Rerank

- 保留现有 weighted-sum/RRF 和 degradation provenance；
- recall candidate 上限保持有界，默认最多 50；
- rerank 默认处理 fusion 后前 20 个 child；
- rerank 输入使用 child embedding/rerank text，不使用已扩展父窗口，避免大窗口掩盖精准命中；
- 同一 child 在 lexical/semantic 中按 source ID 去重。

### 13.5 父窗口扩展

父窗口扩展发生在 child 排序之后、ContextManifest 固化之前：

1. 按 rerank 后 child 顺序处理；
2. 使用 `(workspace_id, resource_id, version_id, window_group_id)` 查询有序兄弟；
3. 每个兄弟 child 保留自己的 chunk ID、canonical node ID、content/hash 和 source spans；
4. 已加入的 window/node/content hash 去重；
5. 按 ContextAssembler token budget 截断，不能突破 reserved output budget；
6. 不把多个兄弟伪装成命中 child 的单一 Evidence；
7. EvidenceSet 保持精确 child recall 事实，扩展内容作为独立 ContextManifest items 持久化；
8. replay 只加载已存 ContextManifest，禁止重新扩窗。

这样既能得到父级上下文，也不会扩大 Patch evidence 对其他 node 的授权。

### 13.6 Resource Search Citation

兼容 Resource Search 的 citation DTO 不新增必填字段。`window` 使用已有：

```json
{
  "group_id": "win_...",
  "start_order": 1,
  "end_order": 4
}
```

snippet 必须来自 raw content；标题和重复表头只作为有界上下文，不得伪装成来源正文。

## 14. 配置与生产装配

最终代码只允许以下状态：

- legacy parser/profile 仅用于读取兼容和明确的测试；
- production writer/retriever 使用本规格 profile；
- production assembly 缺 tokenizer、structured parser、embedding profile 或 window repository 时 fail closed；
- 不允许 production 自动回退到固定字符 splitter；
- 不允许同一次检索混合 legacy 和目标 profile rows。

新增配置名称只能记录名称和校验规则，不能在文档或日志中记录秘密值。建议配置边界：

```text
DOCUMENT_PARSER=structured
CHUNK_PROFILE=docreview-review-structure-2026-08-17
EMBEDDING_TOKENIZER_PROFILE=<non-secret profile name>
EMBEDDING_TOKENIZER_PATH=<local approved artifact path>
```

profile 参数本身固定在代码中的类型化对象，不通过大量环境变量动态调整。

## 15. 代码修改范围

一次性交付至少修改或新增以下边界：

| 模块 | 必须完成的职责 |
| --- | --- |
| `document/parser.py` | Structured ParsedElement、Markdown/TXT 结构、条款识别。 |
| `document/tika.py` | 保留 legacy method，新增 bounded XHTML structured method。 |
| `document/normalize.py` | 标题树、section/clause 类型、block/source span 保留。 |
| `document/ingestion.py` | 构建嵌套 AST、List/Table/Page mapping 和稳定 metadata。 |
| `document/model.py` | 仅在现有 NodeType/metadata 无法表达时做兼容扩展，不改变 hash 编码。 |
| `knowledge/chunking.py` | 唯一 profile、tokenizer-aware child、parent window、类型专用逻辑。 |
| `storage/postgres/document_commit.py` | 单次 projection、真实 section type、完整 metadata 写入。 |
| `providers/embedding.py` / assembly | tokenizer/profile 绑定和批量 pending chunk embedding。 |
| `storage/postgres/builtin.py` | profile-scoped recall、child metadata、window sibling query。 |
| `storage/postgres/resources.py` | Resource Search profile filter和窗口 citation。 |
| `knowledge/evidence_service.py` | child 召回事实不变，提供扩窗所需稳定引用。 |
| `context/assembler.py` | 有界 parent-window expansion 和逐 child ContextManifest item。 |
| `config/settings.py` | fail-closed structured parser/tokenizer/profile 校验。 |
| `operations/` | dry-run/execute 分离的历史结构审计、原始文件重导入、重投影入口和审计摘要。 |

不得把新逻辑复制到 upload、commit、search 三处；parser、chunk builder、context window assembler 各自必须只有一个生产实现。

## 16. 测试规格

实现前先添加失败测试，最终一次性全部转绿。

### 16.1 Parser/AST Golden

至少覆盖：

- Markdown 1-6 级标题和非法 7 级标题；
- 中文“编/章/节/条/款/项”层级；
- 数字多级条款与普通数字文本的消歧；
- TXT 空行段落；
- DOCX XHTML heading/list/table；
- PDF 有页映射和无页映射；
- multi-page clause 不被页边界误切；
- Tika XHTML DTD/entity/depth/size 拒绝；
- stable node IDs、node hashes 和 document hash。

### 16.2 Chunk Golden

至少覆盖：

- 单段超过 512 tokens 且没有双换行；
- 每个最终 embedding text 不超过 hard max；
- child 不跨 node/section/type；
- 小块确定性向前/向后合并；
- overlap 只出现在同 atom forced split；
- overlap 不污染 citation content；
- heading path 截断规则；
- 超长 section 形成多个稳定 parent windows；
- table row packing、重复 header 和 oversized row；
- list item packing 和 oversized item；
- heading-only；
- project 专用 roles；
- fragment hash、embedding text hash、source spans、page range 和 metadata size；
- 相同输入重复 100 次结果完全相同。

### 16.3 Persistence/SQL Contract

fake cursor/transaction 测试必须证明：

- section/chunk 来自同一个已计算 projection；
- SQL 参数包含真实 section type、window、order、metadata 和 profile；
- canonical transaction 顺序、rollback、idempotency 和 Outbox 不变；
- lexical/semantic/window query 都含准确 Workspace/Resource/Version/Profile predicate；
- profile mismatch 不返回旧 embedding；
- window sibling 查询顺序稳定；
- 结构完整版本走 projection-only rebuild，结构丢失版本必须创建新 ResourceVersion；
- 原始 source artifact 缺失/hash mismatch 时 fail closed；
- 不连接数据库也能验证完整 SQL 形态。

### 16.4 Retrieval/Context

至少覆盖：

- lexical exact clause number、金额、日期；
- semantic child recall；
- lexical-only 与 semantic-only degradation；
- fusion/rerank 后才扩窗；
- 多个命中属于同一 window 时只扩展一次；
- 扩展兄弟保留独立 node provenance；
- ContextManifest budget 截断和稳定 hash；
- replay 不重新查询 chunks；
- Patch authorization 不因窗口扩展自动扩大；
- Workspace/Resource/Version 隔离失败路径。

### 16.5 回归

必须继续通过全部现有 API、SSE、Commit、Approval、ToolRuntime、ContextManifest、contract tests、
ruff、format、pyright、compileall 和 `uv lock --check`。

数据库 round-trip、真实 Tika/provider 和 staging 证据在没有授权时必须明确 skip，不得伪造通过。

## 17. 质量验收门槛

建立包含 Markdown、TXT、DOCX、PDF、中文合同/制度、表格和列表的标注文档审查集。交付不得只依赖随机文本测试。

最低验收：

- hard max 违规：0；
- 跨 section/node/type 污染：0；
- source span/page/version 错配：0；
- 相同输入/profile 非确定性差异：0；
- 标注条款、金额、日期和表格问题的 Recall@5：至少 95%；
- 需要父级上下文的问题，正确 window 覆盖率：至少 95%；
- citation 指向正确 version/node/source span：100%；
- 重复扩展 window 比例：不超过 5%；
- ContextManifest 永不超过既有 token/reserved-output budget；
- 与当前策略相比，任何标注类别不得出现 Recall@5 回退。

未达到门槛时不能通过调整公开 API、扩大无限上下文或关闭 isolation 来掩盖问题。

## 18. 一次性交付和切换

“一次改到位”在本规格中的含义是：代码、测试、profile、结构化 parser、chunk projection、embedding、retrieval、扩窗、历史结构审计、重导入/重投影能力和文档在同一个可审查变更集中完成；不发布只能切正文、不能扩窗的中间产品状态。

生产数据切换仍必须遵守持久化安全：

1. 在无数据库环境完成全部离线测试和标注评估；
2. 经明确批准后，在 `_test` 数据库验证 transaction、profile filter、重投影和 rollback；
3. 受控历史操作按 Workspace/Resource/Version 显式 scope 运行，默认 dry-run；先判定 projection-only rebuild 或 source-backed re-ingestion，禁止由操作者手工跳过结构审计；
4. provider I/O 在事务外完成，最终替换/激活在有 hash/profile recheck 的短事务内完成；
5. 读路径只在目标 profile 完整 ready 后切换，不读取半完成 rows；
6. projection-only rebuild 不改写历史 canonical AST、Patch Commit、Outbox 以及 ResourceVersion 的 content/source/version number，只重建可派生 projection；source-backed re-ingestion 必须创建新 ResourceVersion 并保留全部旧事实；
7. 未通过 reconciliation/canary 时恢复旧读 profile，不删除 canonical facts；
8. 旧 projection 的清理由单独批准的操作执行，不能包含在普通部署启动中。

这是一套目标实现和一次受控激活，不是多版算法路线。

## 19. 安全与失败行为

- 文档正文、标题、表格和 metadata 全部视为 untrusted content；不能解释为系统指令。
- 日志不得记录完整文档、chunk content、embedding text、Tika body 或 provider body。
- 错误只记录 request/trace、profile、计数、hash、类别和有界位置，不记录秘密或正文。
- structured parser、tokenizer、chunk builder 和 metadata serializer 都必须有输入大小、深度、元素数和输出数量上限。
- 单文档 chunk 数超限时导入/投影必须明确失败，不得静默截断并标记 ready。
- cancellation 必须传播；provider/Tika retry 继续由既有上层策略控制。
- 任何 profile、tokenizer、canonical hash 或 embedding metadata mismatch 必须 fail closed。

## 20. 外部实现依据

- Docling：先使用 document structure 生成 hierarchical chunks，再按 tokenizer 拆分超长块、合并同 heading/caption 的小块，并为跨块表格重复 header。[概念说明](https://github.com/docling-project/docling/blob/main/docs/concepts/chunking.md)；[HybridChunker 实现](https://github.com/docling-project/docling-core/blob/main/docling_core/transforms/chunker/hybrid_chunker.py)。
- Unstructured：标题和可选 page boundary 是语义边界，区分 soft/hard max，隔离 table，并警告不要对所有正常 chunk 使用 overlap。[实现](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/chunking/title.py)。
- RAGFlow：child 用于精准 recall，parent 提供完整语义上下文，并支持标题层级树。[父子切块说明](https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/configure_child_chunking_strategy.md)；[层级标题实现](https://github.com/infiniflow/ragflow/blob/main/rag/flow/chunker/title_chunker/hierarchy_chunker.py)。
- LlamaIndex：显式维护多层 node 的 parent/child relationship，并支持 sentence window。[层级节点实现](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/relational/hierarchical.py)；[句子窗口实现](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/text/sentence_window.py)。

## 21. 实施批准门

本文件只冻结修改规格。用户明确批准实施前，不修改 Python 业务代码、不连接 PostgreSQL、不运行
migration/DDL/backfill/reindex，也不调用真实 Tika/provider。
