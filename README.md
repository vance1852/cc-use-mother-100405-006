# 多地点农业试验与推广裁定服务

省级农技推广场景下，对耐旱（等）品种的多地点试验建立**可验证证据链**，并给出可追溯、版本化、并发安全的区域推广裁定。

本服务在科技创新协作基础能力（机构/操作者/场所、角色权限、请求幂等、SQLite 事务、哈希串联审计）之上扩展，解决以下问题：

- 连续暴雨等灾害性天气使部分试验田偏离原方案；
- 断网期间基层人员补录播种与病害观察，恢复联网后归并；
- 同一品种在汇总表中同时出现“达标”与“无效”结论；
- 迟到更正、灾害豁免、种子批次撤回不能悄悄改写已发布的适宜区域。

## 证据链与裁定模型

```
种子批次 seed_lot
地块条件 plot ── 承接 ── 试验方案 trial_protocol（时间窗 + 阈值 + 所需审查人数）
农事操作 farm_operation（播种/收获，可离线补录）
气象区间 weather_interval（disaster=1 表示灾害性天气，可致地块偏离原方案）
观测设备 device ── 校准区间 device_calibration（仅 [valid_from, valid_to) 内有效）
排除规则 exclusion_rule（JSON 谓词树，命中观测或地块即排除）
        │
观测 observations：离线按「设备序列 + 观测时间 + 指标」归并
        ├─ 重复上报：不增加样本（返回 duplicate，同一 observation_id）
        ├─ 越序 / 超窗 / 校准失效 / 离线超宽限：进入 review_queue 待审，不丢弃
        └─ 待审可 accept（转有效）或 reject（转排除）
        │
冻结分析 analysis_version：复制纳入/排除快照与有效试验集合，生成 effective_set_hash
        │
区域×品种裁定 region_ruling：recommend / restrict / reject，版本化
        ├─ 引用冻结的 analysis_id、code_version、effective_set_hash
        ├─ 多名审查人并发确认，满足人数后唯一生效（部分唯一索引 + 键级互斥）
        └─ 迟到更正 / 灾害豁免 / 批次撤回只能形成后继裁定（amendment_kind），
           旧版本标记 superseded 但永不被删除或篡改
```

## 可解释性

`GET /regions/{region_id}/cultivars/{cultivar_id}` 返回当前生效裁定，并一路追到：

- 引用的冻结分析版本、规则代码版本、冻结时间、有效集合哈希；
- 推荐/限制/拒绝的量化理由（平均产量、病株率、成苗率与阈值）；
- 每个**被使用**地块及其纳入的每项观测（设备、指标、时间、值）；
- 每个**被排除**地块及排除原因（灾害天气、播种超窗、批次撤回、无有效观测）和被排除观测（待审/规则剔除/审查拒绝）；
- 该区域品种的完整版本链与每位确认审查人。

## 目录

- `src/science_strategy_foundation/`
  - `service.py` / `storage.py` / `audit.py` / `api.py`：协作基础能力；
  - `trial_domain.py`：谓词规则、校准/气象/时间窗判定、推荐阈值计算；
  - `trial_service.py`：试验登记、观测归并待审、分析冻结、版本化裁定与并发确认；
  - `acceptance.py`：基础服务离线验收；`trial_acceptance.py`：农业试验全链路离线验收。
- `tests/`：基础规则、事务、接口，以及 28 个用例覆盖归并/待审/冻结/后继裁定/并发。

## 环境与测试

- Linux，Python 3.11+，仅依赖标准库与 SQLite。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v   # 全部测试
python3 -m compileall -q src tests                        # 构建检查
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance        # 基础离线验收
PYTHONPATH=src python3 -m science_strategy_foundation.trial_acceptance  # 农业试验离线验收
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api \
  --database trial_service.sqlite3 --host 127.0.0.1 --port 8080
```

写入接口通过 `X-Actor-Id` 标识操作者，所有写操作携带幂等 `request_id`；服务重启后 SQLite 状态与审计历史继续保留。健康检查 `GET /health` 同时返回审计链校验结果。

### 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /seed-lots`、`POST /seed-lots/withdraw` | 登记/撤回种子批次 |
| `POST /plots`、`POST /protocols`、`POST /protocol-plots` | 地块、版本化方案、地块承接 |
| `POST /farm-operations`、`POST /weather` | 农事操作（可离线）、气象区间 |
| `POST /devices`、`POST /calibrations` | 设备与校准区间 |
| `POST /exclusion-rules` | 观测/地块排除规则（JSON 谓词） |
| `POST /observations` | 上报观测；归并重复、分流待审 |
| `GET /reviews`、`POST /reviews/resolve` | 待审队列与受理/驳回 |
| `POST /analyses`、`GET /analyses/{id}` | 冻结分析版本及有效/排除快照 |
| `POST /rulings`、`POST /rulings/{id}/confirm` | 提议裁定、审查人并发确认 |
| `GET /rulings/{id}`、`GET /rulings?region_id=&cultivar_id=` | 裁定详情与版本链 |
| `GET /regions/{region}/cultivars/{cultivar}` | 区域结论的完整证据解释 |

### 判定阈值（随方案冻结）

`thresholds`：`min_effective_plots`、`yield_recommend`、`yield_restrict`、`disease_restrict`、`seedling_restrict`。有效地块数不足直接拒绝；产量、病株率、成苗率按阈值映射为推荐/限制/拒绝，全部理由随裁定冻结并可复核。
