# 搭建药膳茶饮试饮批次治理后台

本项目在中医文化夜市的通用后台基础能力之上，提供品鉴区药膳茶饮的批次治理：以原料批号与配方版本建立不可覆盖的制备谱系，对分装、拆分、合并、报损、发放形成数量守恒台账，结合参与者忌口给出可提供性判定，支持原料问题的冻结召回与跨摊位双方确认转移，并能在离线补传重复、乱序消息时给出稳定结果。

## 目录

- `src/night_market_foundation/`：基础层——领域模型、SQLite 存储、角色权限、请求幂等、哈希串联审计、HTTP 路由和离线验收；
- `src/night_market_tasting/`：试饮批次治理——谱系与台账、忌口判定、冻结召回、跨摊位转移和组合 HTTP 路由；
- `tests/`：基础规则、守恒复算、状态机、接口路由和端到端验收测试。

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

试饮治理（模拟从建档、制备、分装、发放、跨摊转移到原料冻结与闭市核对的完整一晚）：

```bash
PYTHONPATH=src python3 -m night_market_tasting.acceptance
```

验收命令在临时 SQLite 数据库中跑完整链路，复算库存与收支配平、核对幂等重放与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_tasting.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写入都需要 `request_id` 保证幂等；`/tasting/*` 之外的路径回落到基础层接口。

### 主要试饮治理接口

- `POST /tasting/ingredient-batches`：登记原料批号（含禁忌/慎用标签）
- `POST /tasting/recipe-versions`：创建配方版本（待审核，修订通过 `supersedes_version_id` 指向旧版）
- `POST /tasting/recipe-risk-confirmations`：审核者确认版本间风险标签差异后启用
- `POST /tasting/preparations`：按已启用配方版本与实际原料批号制备，建立谱系
- `POST /tasting/container-fills` / `container-splits` / `container-merges`：分装、拆分、合并
- `POST /tasting/losses`：报损
- `POST /tasting/participant-declarations`：登记参与者忌口
- `GET /tasting/assessment`：返回 `allow` / `review` / `deny` 及逐条风险来源解释
- `POST /tasting/claims`：发放；`deny` 不产生记录，`review` 需 `manual_confirmed` 后落账
- `POST /tasting/transfers` / `transfer-confirmations` / `transfer-rejections`：跨摊位转移，双方确认后整体生效
- `POST /tasting/ingredient-batch-freezes`：冻结原料批次，联动冻结关联制备与在场容器、取消待处理转移
- `GET /tasting/lineage?prep_id=`：一壶茶的原料批号、配方版本、容器去向、发放记录与收支配平
- `GET /tasting/recall?batch_id=`：风险来源与召回覆盖范围（受影响制备、容器、已发放记录、数量汇总）
- `GET /tasting/inventory-recompute?site_id=`：由台账分录复算库存，与现值比对并列出差异

## 关键不变量

- 制备用料、配方版本、领取记录、台账分录只插入不更新；配方修订产生新版本行，既有领取事实始终指向制作时的版本。
- 每个持有者的当前数量必须等于其台账分录之和；单壶满足 `制备量 = 剩余 + 在容器 + 已发放 + 已报损`。
- 原料冻结只影响仍在场的容器与未来发放，已发生记录完整保留并进入召回覆盖范围。
- 跨摊位转移在接收方操作者确认前不改变归属，确认时出场/入场台账在同一事务中成对写入。
- 相同 `request_id` 与载荷永远返回首次结果；即使确认消息先于发起消息到达，补传后重放仍得到同一结论。
