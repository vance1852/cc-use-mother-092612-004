"""茶饮制备谱系模块的离线端到端验收。

在临时 SQLite 数据库中完整走一遍闭市核对场景：登记原料批号与配方版本、
制备、拆分、发放（含忌口判定与人工确认）、报损、跨摊转移、冻结召回，
最后复算库存并校验审计链。成功时输出一行 status 为 ok 的 JSON。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..clock import FixedClock
from ..storage import Database
from .errors import ManualConfirmationRequired, ServingDenied
from .service import TeaService


def run() -> dict[str, object]:
    """执行完整闭市核对链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "tea_acceptance.sqlite3")
        service = TeaService(database, FixedClock(datetime(2026, 9, 26, 12, 0,
                                                           tzinfo=timezone.utc)))
        base = service.base
        base.register_organization(request_id="acc-org", actor_id="bootstrap",
                                   organization_id="org-001", name="中医文化夜市组委会")
        base.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="系统管理员", role="admin", organization_id="org-001")
        base.register_actor(request_id="acc-op1", actor_id="admin-001", new_actor_id="op-001",
                            display_name="品鉴区负责人", role="operator", organization_id="org-001")
        base.register_actor(request_id="acc-op2", actor_id="admin-001", new_actor_id="op-002",
                            display_name="茶歇区负责人", role="operator", organization_id="org-001")
        base.register_actor(request_id="acc-rv", actor_id="admin-001", new_actor_id="rv-001",
                            display_name="食安审核员", role="reviewer", organization_id="org-001")
        base.register_site(request_id="acc-s1", actor_id="op-001", site_id="site-tasting",
                           organization_id="org-001", name="品鉴区", timezone_name="Asia/Shanghai")
        base.register_site(request_id="acc-s2", actor_id="op-002", site_id="site-tea",
                           organization_id="org-001", name="茶歇区", timezone_name="Asia/Shanghai")

        # 1. 原料批号与配方版本
        service.register_material_batch(request_id="acc-b1", actor_id="op-001",
                                        batch_id="batch-gouqi-20260926", name="宁夏枸杞",
                                        initial_amount=2000, unit="ml", site_id="site-tasting")
        service.register_material_batch(request_id="acc-b2", actor_id="op-001",
                                        batch_id="batch-hongzao-20260926", name="若羌红枣",
                                        initial_amount=1000, unit="ml", site_id="site-tasting")
        service.create_formula(request_id="acc-f1", actor_id="op-001", formula_id="F-gouqi-1",
                               name="枸杞红枣茶",
                               ingredients=[{"material_id": "gouqi", "amount": 10},
                                            {"material_id": "hongzao", "amount": 5}],
                               contraindications=["孕妇"], cautions=["感冒发热"])

        # 2. 替代配方必须经审核者确认风险差异后才能用于制备
        service.create_formula(request_id="acc-f2", actor_id="op-001", formula_id="F-gouqi-2",
                               name="枸杞红枣茶",
                               ingredients=[{"material_id": "gouqi", "amount": 12},
                                            {"material_id": "hongzao", "amount": 5}],
                               contraindications=["孕妇"], cautions=["感冒发热"],
                               supersedes="F-gouqi-1")
        service.review_formula(request_id="acc-rv1", actor_id="rv-001", formula_id="F-gouqi-2",
                               decision="approved", risk_note="枸杞加量 20%，风险差异可接受")

        # 3. 按 v2 制备一壶 100 份
        service.prepare_tea(request_id="acc-p1", actor_id="op-001", preparation_id="prep-001",
                            formula_id="F-gouqi-2",
                            inputs=[{"material_id": "gouqi", "batch_id": "batch-gouqi-20260926",
                                     "amount": 12},
                                    {"material_id": "hongzao", "batch_id": "batch-hongzao-20260926",
                                     "amount": 5}],
                            output_amount=100, output_container_id="pot-001",
                            site_id="site-tasting")

        # 4. 拆分到小杯壶，登记参与者忌口并发放
        service.split_container(request_id="acc-sp1", actor_id="op-001",
                                source_container_id="pot-001", new_container_id="jug-001",
                                amount=30)
        service.upsert_participant(request_id="acc-u1", actor_id="op-001",
                                   participant_id="visitor-001", restrictions=["感冒发热"])
        service.upsert_participant(request_id="acc-u2", actor_id="op-001",
                                   participant_id="visitor-002", restrictions=["孕妇"])
        service.upsert_participant(request_id="acc-u3", actor_id="op-001",
                                   participant_id="visitor-003", restrictions=[])
        # 孕妇 → 不得提供
        denied = service.evaluate_serving(participant_id="visitor-002", container_id="jug-001")
        # 感冒发热 → 需人工确认
        try:
            service.dispense(request_id="acc-d1", actor_id="op-001", participant_id="visitor-001",
                             container_id="jug-001", amount=5)
            manual_raised = False
        except ManualConfirmationRequired:
            manual_raised = True
        service.dispense(request_id="acc-d1b", actor_id="op-001", participant_id="visitor-001",
                         container_id="jug-001", amount=5, confirm_manual=True,
                         confirmation_note="现场测量体温正常，参与者知情")
        # 无忌口 → 直接提供
        service.dispense(request_id="acc-d2", actor_id="op-001", participant_id="visitor-003",
                         container_id="jug-001", amount=10)
        # 报损
        service.report_loss(request_id="acc-l1", actor_id="op-001", container_id="pot-001",
                            amount=5, reason="壶嘴洒漏")

        # 5. 跨摊转移：整壶 jug-001 转交茶歇区，双方确认后生效
        service.propose_transfer(request_id="acc-t1", actor_id="op-001", transfer_id="tr-001",
                                 from_site_id="site-tasting", to_site_id="site-tea",
                                 items=[{"container_id": "jug-001"}])
        service.confirm_transfer(request_id="acc-t1c", actor_id="op-002", transfer_id="tr-001")

        # 6. 发现原料问题：冻结枸杞批号，冻结仍在场的关联容器，历史发放保留
        service.freeze_batch(request_id="acc-fz1", actor_id="rv-001",
                             batch_id="batch-gouqi-20260926", reason="农残抽检异常")
        coverage = service.recall_coverage("batch-gouqi-20260926")

        # 7. 闭市核对：谱系、库存复算、审计链
        lineage = service.container_lineage("jug-001")
        inventory = service.recompute_inventory()
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "lineage_formula_version": lineage["formulas"][0]["version"],
            "lineage_batches": sorted(item["batch_id"] for item in lineage["material_batches"]),
            "denied_decision": denied["decision"],
            "manual_raised": manual_raised,
            "transfer_status": service.get_transfer("tr-001").status,
            "jug_site": service.get_container("jug-001").site_id,
            "frozen_containers": sorted(coverage["on_site_frozen_containers"]),
            "recall_participants": coverage["affected_participant_ids"],
            "history_dispenses": len(service.list_dispenses()),
            "inventory_conserved": inventory["conserved"],
            "stock_total": inventory["stock_total"],
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["inventory_conserved"] and result["denied_decision"] == "denied"
          and result["manual_raised"] and result["transfer_status"] == "confirmed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
