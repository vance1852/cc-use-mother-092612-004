"""茶饮模块 HTTP/JSON 边界测试。"""

import unittest

from night_market_foundation.api import route
from night_market_foundation.tea import TeaService

from tea_support import build_service, seed_tea


def call(service: TeaService, method: str, path: str, body=None, actor: str = "op1"):
    return route(service.base, method, path, body or {}, {"X-Actor-Id": actor}, tea=service)


class TeaApiTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        seed_tea(self.service)

    def tearDown(self):
        self.service.database.close()

    def test_full_lineage_and_inventory_over_http(self):
        status, payload = call(self.service, "GET", "/tea/lineage?container_id=pot-1")
        self.assertEqual(200, status)
        self.assertEqual({"B-gouqi", "B-hongzao"},
                         {item["batch_id"] for item in payload["material_batches"]})
        self.assertEqual("F-v1", payload["formulas"][0]["formula_id"])

        status, payload = call(self.service, "GET", "/tea/inventory")
        self.assertEqual(200, status)
        self.assertTrue(payload["conserved"])
        self.assertEqual(100, payload["stock_total"])

    def test_serving_evaluation_explains_decision(self):
        call(self.service, "POST", "/tea/participants",
             {"request_id": "u1", "participant_id": "pt-preg", "restrictions": ["孕妇"]})
        status, payload = call(self.service, "GET",
                               "/tea/serving-evaluation?participant_id=pt-preg&container_id=pot-1")
        self.assertEqual(200, status)
        self.assertEqual("denied", payload["decision"])
        self.assertEqual("contraindication", payload["reasons"][0]["type"])
        self.assertIn("formula_id", payload["reasons"][0]["source"])

    def test_denied_dispense_returns_403_with_evaluation(self):
        call(self.service, "POST", "/tea/participants",
             {"request_id": "u1", "participant_id": "pt-preg", "restrictions": ["孕妇"]})
        status, payload = call(self.service, "POST", "/tea/dispenses",
                               {"request_id": "d1", "participant_id": "pt-preg",
                                "container_id": "pot-1", "amount": 5})
        self.assertEqual(403, status)
        self.assertEqual("serving_denied", payload["error"])
        self.assertIn("evaluation", payload)
        self.assertEqual("denied", payload["evaluation"]["decision"])

    def test_manual_confirmation_flow_over_http(self):
        call(self.service, "POST", "/tea/participants",
             {"request_id": "u1", "participant_id": "pt-fever", "restrictions": ["感冒发热"]})
        status, payload = call(self.service, "POST", "/tea/dispenses",
                               {"request_id": "d1", "participant_id": "pt-fever",
                                "container_id": "pot-1", "amount": 5})
        self.assertEqual(409, status)
        self.assertEqual("manual_confirmation_required", payload["error"])
        status, payload = call(self.service, "POST", "/tea/dispenses",
                               {"request_id": "d2", "participant_id": "pt-fever",
                                "container_id": "pot-1", "amount": 5,
                                "confirm_manual": True, "confirmation_note": "体温正常"})
        self.assertEqual(201, status)
        self.assertEqual("manual_confirm", payload["decision"])

    def test_substitute_formula_review_then_prepare(self):
        status, payload = call(self.service, "POST", "/tea/formulas",
                               {"request_id": "f2", "formula_id": "F-v2", "name": "枸杞茶",
                                "ingredients": [{"material_id": "gouqi", "amount": 12},
                                                {"material_id": "hongzao", "amount": 5}],
                                "contraindications": ["孕妇"], "cautions": [],
                                "supersedes": "F-v1"})
        self.assertEqual(201, status)
        self.assertEqual("candidate", payload["status"])
        # 操作员不能审核
        status, payload = call(self.service, "POST", "/tea/formulas/review",
                               {"request_id": "rv-x", "formula_id": "F-v2",
                                "decision": "approved", "risk_note": "越权"}, actor="op1")
        self.assertEqual(403, status)
        # 审核员确认风险差异
        status, payload = call(self.service, "POST", "/tea/formulas/review",
                               {"request_id": "rv-ok", "formula_id": "F-v2",
                                "decision": "approved", "risk_note": "加量20%可接受"}, actor="rv1")
        self.assertEqual(201, status)

    def test_freeze_and_recall_endpoint(self):
        call(self.service, "POST", "/tea/participants",
             {"request_id": "u1", "participant_id": "pt-1", "restrictions": []})
        call(self.service, "POST", "/tea/dispenses",
             {"request_id": "d1", "participant_id": "pt-1", "container_id": "pot-1", "amount": 10})
        status, payload = call(self.service, "POST", "/tea/freezes/batch",
                               {"request_id": "fz1", "batch_id": "B-gouqi",
                                "reason": "农残异常"}, actor="rv1")
        self.assertEqual(201, status)
        self.assertIn("pot-1", payload["on_site_containers"])
        status, payload = call(self.service, "GET", "/tea/recall?batch_id=B-gouqi")
        self.assertEqual(200, status)
        self.assertEqual("B-gouqi", payload["risk_source"]["batch_id"])
        self.assertEqual(["pt-1"], payload["affected_participant_ids"])

    def test_transfer_requires_both_parties(self):
        status, payload = call(self.service, "POST", "/tea/transfers/propose",
                               {"request_id": "t1", "transfer_id": "tr-1",
                                "from_site_id": "s1", "to_site_id": "s2",
                                "items": [{"container_id": "pot-1"}]})
        self.assertEqual(201, status)
        # 发起人不能自行确认
        status, payload = call(self.service, "POST", "/tea/transfers/confirm",
                               {"request_id": "t1c-self", "transfer_id": "tr-1"})
        self.assertEqual(403, status)
        # 接收方确认后整体生效
        status, payload = call(self.service, "POST", "/tea/transfers/confirm",
                               {"request_id": "t1c", "transfer_id": "tr-1"}, actor="op2")
        self.assertEqual(201, status)
        status, payload = call(self.service, "GET", "/tea/container?container_id=pot-1")
        self.assertEqual("s2", payload["site_id"])

    def test_tea_route_requires_module(self):
        status, payload = route(self.service.base, "GET", "/tea/inventory", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
