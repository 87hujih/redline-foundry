# UI、交互与设计系统约束

**状态：** Draft  
**定位：** 安静、专业、内容优先的文档审阅工作台。

## 1. 体验原则

1. 文档、消息和运行状态是主角，装饰不得抢占阅读空间。
2. 界面为重复工作优化：可扫描、可比较、操作路径短。
3. 每次状态变化都明确表达“正在做什么、是否完成、如何恢复”。
4. 不用颜色单独表达状态，不用动效掩盖等待。
5. Desktop 优先保证高效工作，Mobile 保证核心流程可完成，而不是压缩桌面三栏。

禁止营销式 Hero、装饰性渐变球、玻璃拟态背景、卡片套卡片和过大的展示标题。

## 2. 信息架构

Desktop 的基础框架为：

```text
┌──────────────┬──────────────────────────────┬──────────────────┐
│ 主导航/会话  │ 主工作区                     │ 上下文侧栏       │
│              │ 消息、资源正文、列表或详情   │ 当前文档/Run     │
└──────────────┴──────────────────────────────┴──────────────────┘
```

- 左栏只承担顶级导航和会话选择，不放主要业务表单。
- 中栏是唯一主滚动区；避免页面与面板同时滚动。
- 右栏只在需要上下文时出现，不得挤压主工作区到不可读。
- `< 1024px` 时右栏改为 Drawer；`< 768px` 时左栏也改为 Drawer。
- 375px 宽度必须无页面级横向滚动；数据表在移动端改为分组列表或局部可控滚动。

## 3. 设计 token

采用三层结构，禁止组件跨层引用：

```text
Primitive 原始值 -> Semantic 用途 -> Component 局部映射
```

首版必须覆盖：

| 类别 | 必需 token |
| --- | --- |
| Color | background、surface、foreground、muted、border、primary、success、warning、danger、focus |
| Spacing | 4、8、12、16、24、32 |
| Type | 12、14、16、18、24；正文行高 1.5 至 1.75 |
| Radius | 4、6、8；业务卡片最大 8px |
| Elevation | none、overlay；普通页面区块不使用浮动阴影 |
| Motion | fast、normal；只用于反馈和层级变化 |
| Layer | base、sticky、drawer、modal、toast |

- 品牌未冻结前，颜色值只是实现决策，不属于产品契约；语义 token 名称属于契约。
- 业务组件不得出现 raw hex、任意像素间距和自创 z-index。
- 暗色模式只有在完成独立对比度验证后才能上线，不能简单反转亮色值。
- Letter spacing 保持 `0`；字体大小不随 viewport 连续缩放。

## 4. 排版与内容

- 正文默认不小于 14px；移动端输入框和长正文不小于 16px。
- 页面 H1 建议 24px，紧凑面板标题 16 至 18px，不使用 Hero 级标题。
- 长文阅读宽度控制在约 65 至 75 个字符；代码、URL、UUID 使用
  `overflow-wrap: anywhere`，正常中文和英文段落不得使用 `word-break: break-all`。
- 时间使用本地化展示，同时在 tooltip 或详情中保留完整时间。
- 状态名称使用统一中文词表；不得直接把后端枚举未经映射地显示给用户。
- 未知 message kind 必须以安全的“暂不支持的消息”占位展示，不能静默丢失整个会话。

## 5. 组件约束

### 操作控件

- 一个页面或弹窗只能有一个视觉主操作。
- 熟悉的工具动作优先使用 SVG 图标；陌生动作使用图标加文字。
- Icon-only button 必须有 tooltip 和 accessible name。
- Web 指针目标至少 24x24 CSS px；主要和移动端触控目标至少 44x44 CSS px。
- 所有 Button、Link、Input、Select、Dialog 必须使用原生语义或无障碍组件原语，禁止 clickable `div`。
- Disabled 必须同时具备语义属性和视觉区别，不能只降低颜色。

### 卡片、表格和状态

- 卡片只用于重复实体、弹窗或真正需要边界的工具，不把整页 section 包成浮动卡片。
- 禁止卡片嵌套卡片；嵌套信息使用 divider、section 或 definition list。
- Run/Approval 状态同时显示文字与图标，颜色只是辅助。
- 表格使用正确表头；可排序列暴露 `aria-sort`；ID、次数等数值使用 tabular figures。
- 超过 50 条且产生可测量性能问题后才引入虚拟列表。

### 表单和反馈

- 输入必须有可见 label，placeholder 不能代替 label。
- 校验在 blur 或 submit 后展示；错误紧邻字段并关联 `aria-describedby`。
- 异步提交期间按钮显示进行中且防重复提交。
- 删除 Session、拒绝 Approval 等破坏性操作需要确认；Approval reason 必须在提交前校验非空白。
- Toast 不抢焦点；一般反馈使用 `aria-live="polite"`，阻断错误使用明确错误区域。
- 空状态必须说明当前事实和一个合理下一步，不罗列产品功能说明。

## 6. Assistant 交互

- 消息顺序严格按 `sequence_no`，React key 使用 message `id`。
- 用户发送后可以显示 pending 投影，但必须能被持久化消息稳定替换，不能产生重复消息。
- Composer 在没有有效 `resource_id`、消息为空白或 Turn 正在接受时禁止发送，并说明原因。
- SSE 状态以紧凑状态行呈现，不为每个内部事件弹 Toast。
- 新消息到达时：用户位于底部才自动滚动；用户正在阅读历史内容时显示“回到最新”。
- 流式错误保留输入草稿和恢复动作，不清空已显示的持久化消息。
- 文档切换成功前保持原选择；失败时回滚控件并显示原因。
- Markdown 必须经过允许列表清理；外部链接明确标识并使用安全的新窗口属性。

## 7. 可访问性门槛

目标为 WCAG 2.2 AA：

- 正常文本对比度至少 4.5:1，大文本和非文本控件至少 3:1。
- 所有操作可仅用键盘完成，Tab 顺序与视觉顺序一致，焦点环始终可见。
- Sticky header、composer、drawer 和 toast 不得完全遮挡键盘焦点。
- Dialog 打开时焦点进入，关闭时返回触发器；Esc 可关闭非阻断弹窗。
- 路由变化后焦点移动到主标题，不把焦点强制移到普通数据更新处。
- 状态更新使用克制的 live region；不得逐 token 朗读流式内容。
- 尊重 `prefers-reduced-motion`，关闭非必要移动和淡入。
- 200% 文本缩放下不得丢失操作或遮挡内容。

## 8. 页面状态矩阵

每个数据页面必须设计并实现以下状态：

| 状态 | 必须表现 |
| --- | --- |
| Initial loading | 保留稳定布局的 skeleton 或进度提示 |
| Empty | 事实说明和一个可执行下一步 |
| Success | 数据、更新时间或状态语义清晰 |
| Recoverable error | 原因、重试按钮、保留已有内容 |
| Unauthorized | 交给统一登录恢复流程 |
| Forbidden | 明确无权限，不伪装成网络错误 |
| Not found | 不泄露跨 Workspace 对象是否存在 |
| Offline/interrupted | 保留草稿和 SSE 恢复入口 |

## 9. 视觉验收视口

至少验证：375x812、768x1024、1024x768、1440x900。每个视口检查：

- 无非预期横向滚动、文字遮挡和按钮溢出。
- Drawer、sticky 区域和虚拟键盘不会覆盖主要操作。
- 长标题、长 UUID、中文错误和英文 token 均可安全换行。
- Hover、focus-visible、active、disabled、loading 状态尺寸稳定，不引起布局跳动。

