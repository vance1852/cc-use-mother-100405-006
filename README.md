# 裁定跨区域农业试验结论协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

`src/agri_trial_adjudication/` 是在这些边界上建成的多地点农业试验与推广裁定服务：

- **可验证证据链**：登记种子批次、地块条件、试验方案（播种/观测窗口、最少有效试验数、确认人数）、农事操作、气象区间、观测设备校准和排除规则，全部写入哈希串联审计；
- **离线观测归并**：观测按设备序列归并，重复上报不增加样本；越序、超窗或校准失效的记录进入待审而不是丢弃，由审查人逐项采纳或驳回；
- **冻结的分析版本**：区域结论必须引用最新冻结版本（含有效/排除试验集合与输入摘要），偏离方案的试验凭灾害性气象豁免方可保留；
- **后继裁定**：迟到更正、灾害豁免或批次撤回只能形成后继裁定，生效时原子取代旧版本，已发布范围保留在裁定历史中；
- **并发确认唯一生效**：审查人按方案要求的人数确认，`BEGIN IMMEDIATE` 事务加唯一部分索引保证同一区域品种组合只有一个生效版本；
- **结论解释接口**：`GET /regional-verdict` 与 `GET /verdict-explanation` 说明某区域为何推荐、限制或拒绝一个品种，并追到每项结论使用及排除的地块。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/agri_trial_adjudication/`：农业试验裁定的表结构、领域规则、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
PYTHONPATH=src python3 -m agri_trial_adjudication.acceptance
```

基础服务验收在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，核对幂等回执与审计链。农业裁定验收完整走一遍业务故事：登记证据链、暴雨致偏离、灾害豁免、断网补录归并、待审处理、冻结分析、双人确认发布推荐裁定，最后批次撤回以后继裁定取代。两者成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m agri_trial_adjudication.api --database agri_trial.sqlite3 --host 127.0.0.1 --port 8081
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

农业裁定服务的主要接口：`POST /seed-batches`、`/plots`、`/protocols`、`/exclusion-rules`、`/device-calibrations`、`/trials`、`/farming-operations`、`/weather-intervals`、`/trial-deviations` 登记证据链；`POST /observation-batches` 归并离线观测，`GET /pending-observations` 与 `POST /observation-reviews` 处理待审；`POST /disaster-exemptions`、`/analysis-versions`、`/verdicts`、`/verdict-confirmations` 完成豁免、冻结与裁定；`GET /regional-verdict`、`/verdict-explanation`、`/verdict-history`、`/trial-evaluation`、`/trial-summary` 供专家组解释与追溯。
