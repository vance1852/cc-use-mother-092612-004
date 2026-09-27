"""茶饮模块测试共用的引导夹具。"""

from __future__ import annotations

from datetime import datetime, timezone

from night_market_foundation.clock import FixedClock
from night_market_foundation.storage import Database
from night_market_foundation.tea.service import TeaService


def build_service() -> TeaService:
    """建立一个已登记机构、三类角色和两个摊位的茶饮服务。"""

    database = Database(":memory:")
    service = TeaService(database, FixedClock(datetime(2026, 9, 26, tzinfo=timezone.utc)))
    service.base.register_organization(request_id="org", actor_id="bootstrap",
                                       organization_id="o1", name="夜市机构")
    service.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                                display_name="管理员", role="admin", organization_id="o1")
    service.base.register_actor(request_id="op1", actor_id="admin", new_actor_id="op1",
                                display_name="甲摊负责人", role="operator", organization_id="o1")
    service.base.register_actor(request_id="op2", actor_id="admin", new_actor_id="op2",
                                display_name="乙摊负责人", role="operator", organization_id="o1")
    service.base.register_actor(request_id="rv1", actor_id="admin", new_actor_id="rv1",
                                display_name="审核员", role="reviewer", organization_id="o1")
    service.base.register_site(request_id="s1", actor_id="op1", site_id="s1",
                               organization_id="o1", name="甲摊", timezone_name="Asia/Shanghai")
    service.base.register_site(request_id="s2", actor_id="op2", site_id="s2",
                               organization_id="o1", name="乙摊", timezone_name="Asia/Shanghai")
    return service


def seed_tea(service: TeaService, *, output_container_id: str = "pot-1",
             preparation_id: str = "prep-1", formula_id: str = "F-v1",
             output_amount: float = 100.0, site_id: str = "s1") -> str:
    """登记原料、首版配方并完成一次制备，返回产出容器编号。"""

    service.register_material_batch(request_id="b-gouqi", actor_id="op1", batch_id="B-gouqi",
                                    name="枸杞", initial_amount=1000, unit="ml", site_id=site_id)
    service.register_material_batch(request_id="b-hongzao", actor_id="op1", batch_id="B-hongzao",
                                    name="红枣", initial_amount=500, unit="ml", site_id=site_id)
    service.create_formula(
        request_id="f-v1", actor_id="op1", formula_id=formula_id, name="枸杞茶",
        ingredients=[{"material_id": "gouqi", "amount": 10},
                     {"material_id": "hongzao", "amount": 5}],
        contraindications=["孕妇"], cautions=["感冒发热"])
    service.prepare_tea(
        request_id="p-1", actor_id="op1", preparation_id=preparation_id, formula_id=formula_id,
        inputs=[{"material_id": "gouqi", "batch_id": "B-gouqi", "amount": 10},
                {"material_id": "hongzao", "batch_id": "B-hongzao", "amount": 5}],
        output_amount=output_amount, output_container_id=output_container_id, site_id=site_id)
    return output_container_id
