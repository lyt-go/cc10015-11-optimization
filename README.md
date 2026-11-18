# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 | 模型编号、预测对象、历史窗口 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 评估看板聚合快照（消费范围裁剪 + V011 联动）

评估看板不再跨消费范围合并实验数据。快照链路集中在
`backend/app/services/board_snapshot/`：

| 文件 | 职责 |
| --- | --- |
| `registry.py` | 消费范围注册表（成员名单/获准实验）+ V011 指标口径登记表，提供归属核对 |
| `ab_source.py` | 带实验归属与指标版本标注的 AB 对比结果表，是快照的数据源 |
| `snapshot_store.py` | current/staging/archive 三段式存储，保证原子发布、失败回滚与留档 |
| `board_snapshot.py` | 裁剪聚合、V011 校验、版本冲突两种策略、只读消费等核心规则 |

关键约束：

- **按消费范围裁剪**：刷新时读取看板配置与 AB 对比结果表，只聚合获准实验；越权
  实验进 `excluded_experiments` 留档，不进任何指标卡，成员聚合值与卡片成员数一致。
- **归属核对/只读**：绑定、刷新、调整范围等写操作先核对操作者是否在范围成员内，
  越权返回 403；消费端读取只返回已发布快照，不修改受控内容。
- **版本同步发布**：消费范围版本 `scope_version` 与聚合版本 `snapshot_version` 在
  同一把锁内原子提交，避免同一看板混入另一范围的成员。
- **失败保留旧快照**：候选快照先入 staging，裁剪/校验/发布任一步失败即回滚，
  current 保持上一份获准范围结果，绝不发布无范围限制的空壳。
- **V011 跨模块联动**：卡片指标版本必须在口径登记表内，未登记口径（如 V099）的
  卡片被拒绝并留痕；`/v011/verify` 按调用链逐卡核对，`/archive` 留档可查。
- **版本冲突两策略**：范围调整与刷新并发时，`rebuild` 全量重建（只显示新范围完整
  聚合）或 `incremental` 保留旧范围聚合并附待移除实验清单（`scope_consistent=false`）。

接口前缀 `/api/evaluate_board`，主要端点：`/scopes`、`/v011/calibers`、
`/boards`、`/boards/{id}`、`/boards/{id}/scope`、
`/boards/{id}/snapshot`（GET 只读 / POST refresh）、
`/boards/{id}/scope/adjust`、`/boards/{id}/archive`、`/boards/{id}/v011/verify`。
前端「评估看板」页的侧栏列表与详情共用 `/boards` 同一组数据。

领域：数据工程
子方向：数据分析与数据应用
