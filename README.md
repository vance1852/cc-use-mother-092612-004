# 搭建药膳茶饮试饮批次治理后台基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

`src/night_market_foundation/tea/` 在基础层之上实现**药膳茶饮制备谱系治理**，支撑品鉴区在闭市前核对“一壶茶用了哪批原料、按哪版配方制作、分到了哪些容器、剩余量是否与发放记录相符”。

## 茶饮制备谱系模块能力

- **不可覆盖的制备谱系**：原料批号与配方版本一经登记即不可改；替代配方须经审核者确认风险差异（候选 → 通过/驳回，结论不可改）后才能用于后续制备。制备记录快照当时的配方内容与每一味原料的批号，配方修订不会改写既有制备与已发生的发放事实。
- **数量守恒账目**：产出（produce）、拆分（split）、合并（merge）、报损（loss）、发放（dispense）、跨摊转移（transfer_out/transfer_in）全部是只追加的带符号流水，容器余额是可由流水复算的缓存。`GET /tea/inventory` 复算全局恒等式：`当前库存 = 制备产出 − 报损 − 实际发放`（拆分/合并/转移在容器间成对抵消）。
- **容器谱系与召回**：拆分、合并登记父子边，递归回答任一容器的全部来源批号与配方版本；支持正向召回覆盖（问题批号 → 制备 → 历次拆分/合并 → 在场容器与历史发放参与者）。
- **忌口判定**：结合参与者主动提供的忌口，返回 `served`（可提供）/ `manual_confirm`（需人工确认并填写确认说明）/ `denied`（不得提供），每条结论都给出风险来源（制备、配方版本、忌口项）。
- **冻结召回**：发现原料问题时冻结问题批号关联的、仍在场的全部容器（含已跨摊转出但仍在场的容器）并暂停未来发放与再制备；已发生的发放、报损记录原样保留。
- **跨摊转移**：转出方提议、接收方另一位负责人确认，确认瞬间在单事务内整体生效；确认时复检余额与冻结状态，拒绝则不发生任何数量变化。
- **离线补传**：所有写操作复用全局 `request_id` 幂等回执与单调流水序号；重复消息返回同一回执，乱序首次到达也收敛到相同结果（状态相关校验都在幂等命中之后执行）。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tea/material-batches` | 登记原料批号 |
| POST | `/tea/formulas` | 登记配方版本（同名新版须 `supersedes` 并先审核） |
| POST | `/tea/formulas/review` | 审核者确认替代配方风险差异 |
| POST | `/tea/preparations` | 按批号与已审核配方完成制备，产出新容器 |
| POST | `/tea/containers/split` `/tea/containers/merge` | 拆分 / 合并，登记谱系边 |
| POST | `/tea/losses` | 报损 |
| POST | `/tea/participants` | 登记参与者主动提供的忌口 |
| GET | `/tea/serving-evaluation` | 返回可提供/需确认/不得提供及风险来源 |
| POST | `/tea/dispenses` | 发放（慎用项需 `confirm_manual` 与说明） |
| GET | `/tea/lineage` `/tea/movements` `/tea/inventory` | 谱系、流水、库存复算 |
| POST | `/tea/freezes/batch` `/tea/freezes/container` | 冻结 |
| GET | `/tea/recall?batch_id=` | 风险来源与召回覆盖范围 |
| POST | `/tea/transfers/propose` `/confirm` `/reject` | 跨摊转移双方确认 |

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/night_market_foundation/tea/`：制备谱系、守恒流水、忌口判定、冻结召回、跨摊转移；
- `tests/`：基础规则、事务边界、接口路由、茶饮谱系治理和端到端验收测试。

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

基础层：

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
```

茶饮制备谱系（闭市核对全流程）：

```bash
PYTHONPATH=src python3 -m night_market_foundation.tea.acceptance
```

验收命令会在临时 SQLite 数据库中走通登记、替代配方审核、制备、拆分、忌口判定与人工确认、发放、报损、跨摊转移、冻结召回，并复算库存与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留；茶饮接口统一以 `/tea` 开头。
