"""试饮批次治理的离线端到端验收：模拟一晚营业到闭市核对的完整链路。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from night_market_foundation.clock import FixedClock
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from .service import TastingService


def run() -> dict[str, object]:
    """执行完整业务链并返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        service = TastingService(database, clock)

        foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                         organization_id="org-001", name="中医文化夜市组委会")
        foundation.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-001",
                                  display_name="系统管理员", role="admin", organization_id="org-001")
        foundation.register_actor(request_id="acc-op1", actor_id="admin-001", new_actor_id="operator-001",
                                  display_name="品鉴区负责人", role="operator", organization_id="org-001")
        foundation.register_actor(request_id="acc-op2", actor_id="admin-001", new_actor_id="operator-002",
                                  display_name="东侧摊位负责人", role="operator", organization_id="org-001")
        foundation.register_actor(request_id="acc-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                                  display_name="配方审核者", role="reviewer", organization_id="org-001")
        foundation.register_site(request_id="acc-site1", actor_id="admin-001", site_id="site-tasting",
                                 organization_id="org-001", name="品鉴区", timezone_name="Asia/Shanghai")
        foundation.register_site(request_id="acc-site2", actor_id="admin-001", site_id="site-east",
                                 organization_id="org-001", name="东侧摊位", timezone_name="Asia/Shanghai")

        service.register_ingredient_batch(
            request_id="acc-batch-gouqi", actor_id="operator-001", batch_id="batch-gouqi",
            site_id="site-tasting", ingredient_name="枸杞", lot_number="GQ-20260920",
            quantity_received=2000, unit="g")
        service.register_ingredient_batch(
            request_id="acc-batch-juhua", actor_id="operator-001", batch_id="batch-juhua",
            site_id="site-tasting", ingredient_name="菊花", lot_number="JH-20260921",
            quantity_received=1000, unit="g", caution_tags=["虚寒体质"])
        service.create_recipe_version(
            request_id="acc-recipe-v1", actor_id="operator-001", recipe_version_id="recipe-juhua-v1",
            site_id="site-tasting", recipe_name="菊花枸杞茶", version=1,
            components=[{"ingredient": "菊花", "amount": 3, "unit": "g"},
                        {"ingredient": "枸杞", "amount": 5, "unit": "g"}],
            strict_tags=["孕期禁忌"], caution_tags=["虚寒体质"])
        service.confirm_recipe_risk(request_id="acc-confirm-v1", actor_id="reviewer-001",
                                    recipe_version_id="recipe-juhua-v1", risk_note="首版配方，风险标签已核对")

        service.brew_preparation(
            request_id="acc-brew", actor_id="operator-001", prep_id="prep-001", site_id="site-tasting",
            recipe_version_id="recipe-juhua-v1", quantity_brewed=5000, unit="ml",
            ingredients=[{"batch_id": "batch-juhua", "quantity_used": 30},
                         {"batch_id": "batch-gouqi", "quantity_used": 50}])
        service.fill_container(request_id="acc-fill-c1", actor_id="operator-001", container_id="cup-c1",
                               prep_id="prep-001", label="一号保温桶", quantity=2000, unit="ml")
        service.fill_container(request_id="acc-fill-c2", actor_id="operator-001", container_id="cup-c2",
                               prep_id="prep-001", label="二号保温桶", quantity=2000, unit="ml")
        service.split_container(request_id="acc-split", actor_id="operator-001", source_container_id="cup-c1",
                                new_container_id="cup-c3", label="试饮小壶", quantity=500)
        service.record_loss(request_id="acc-loss", actor_id="operator-001", holder_type="container",
                            holder_id="cup-c3", quantity=100, unit="ml", reason="倾倒洒漏")

        service.declare_restrictions(request_id="acc-decl-p2", actor_id="operator-001",
                                     participant_id="guest-002", restrictions=["虚寒体质"])
        service.declare_restrictions(request_id="acc-decl-p3", actor_id="operator-001",
                                     participant_id="guest-003", restrictions=["孕期禁忌"])
        service.record_claim(request_id="acc-claim-p1", actor_id="operator-001", claim_id="claim-001",
                             holder_type="container", holder_id="cup-c3", participant_id="guest-001",
                             quantity=200, unit="ml")
        review_claim = service.record_claim(request_id="acc-claim-p2", actor_id="operator-001",
                                            claim_id="claim-002", holder_type="container",
                                            holder_id="cup-c2", participant_id="guest-002",
                                            quantity=300, unit="ml", manual_confirmed=True)
        denied = service.record_claim(request_id="acc-claim-p3", actor_id="operator-001",
                                      claim_id="claim-003", holder_type="container",
                                      holder_id="cup-c1", participant_id="guest-003",
                                      quantity=200, unit="ml")

        service.initiate_transfer(request_id="acc-transfer", actor_id="operator-001",
                                  transfer_id="transfer-001", container_id="cup-c2",
                                  to_site_id="site-east")
        service.confirm_transfer(request_id="acc-transfer-confirm", actor_id="operator-002",
                                 transfer_id="transfer-001")

        service.freeze_ingredient_batch(request_id="acc-freeze", actor_id="reviewer-001",
                                        batch_id="batch-juhua", reason="该批菊花抽检农残超标")
        frozen_attempt = service.record_claim(request_id="acc-claim-frozen", actor_id="operator-001",
                                              claim_id="claim-004", holder_type="container",
                                              holder_id="cup-c1", participant_id="guest-001",
                                              quantity=100, unit="ml")

        replayed_brew = service.brew_preparation(
            request_id="acc-brew", actor_id="operator-001", prep_id="prep-001", site_id="site-tasting",
            recipe_version_id="recipe-juhua-v1", quantity_brewed=5000, unit="ml",
            ingredients=[{"batch_id": "batch-juhua", "quantity_used": 30},
                         {"batch_id": "batch-gouqi", "quantity_used": 50}])

        lineage = service.get_preparation_lineage("prep-001")
        recall = service.get_recall_coverage("batch-juhua")
        recompute_tasting = service.recompute_inventory("site-tasting")
        recompute_east = service.recompute_inventory("site-east")
        valid, event_count = foundation.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "review_claim_decision": review_claim["decision"],
            "denied_claim_decision": denied["decision"],
            "frozen_claim_decision": frozen_attempt["decision"],
            "replayed_brew": replayed_brew["replayed"],
            "lineage_consistent": lineage["reconciliation"]["consistent"],
            "lineage_containers": len(lineage["containers"]),
            "recall_containers": len(recall["containers"]),
            "recall_claims": len(recall["claims"]),
            "recall_distributed": recall["totals"]["distributed"],
            "recompute_tasting_consistent": recompute_tasting["consistent"],
            "recompute_east_consistent": recompute_east["consistent"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = [
        result["status"] == "ok",
        result["audit_valid"],
        result["review_claim_decision"] == "review",
        result["denied_claim_decision"] == "deny",
        result["frozen_claim_decision"] == "deny",
        result["replayed_brew"],
        result["lineage_consistent"],
        result["lineage_containers"] == 3,
        result["recall_containers"] == 3,
        result["recall_claims"] == 2,
        result["recall_distributed"] == 500.0,
        result["recompute_tasting_consistent"],
        result["recompute_east_consistent"],
    ]
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
