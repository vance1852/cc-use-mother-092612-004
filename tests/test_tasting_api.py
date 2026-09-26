import unittest

from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from night_market_tasting.api import route
from night_market_tasting.service import TastingService


class TastingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.foundation = DomainService(self.database)
        self.service = TastingService(self.database)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="活动机构")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                       display_name="操作员", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="rev", actor_id="a1", new_actor_id="rev1",
                                       display_name="审核者", role="reviewer", organization_id="o1")
        self.foundation.register_site(request_id="s1", actor_id="a1", site_id="site-1",
                                      organization_id="o1", name="品鉴区",
                                      timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="op1"):
        return route(self.foundation, self.service, "POST", path, body,
                     {"X-Actor-Id": actor})

    def _seed_batch_and_recipe(self):
        status, _ = self._post("/tasting/ingredient-batches", {
            "request_id": "b1", "batch_id": "batch-1", "site_id": "site-1",
            "ingredient_name": "菊花", "lot_number": "JH-1", "quantity_received": 1000,
            "unit": "g", "caution_tags": ["虚寒体质"]})
        self.assertEqual(201, status)
        status, _ = self._post("/tasting/recipe-versions", {
            "request_id": "r1", "recipe_version_id": "rv1", "site_id": "site-1",
            "recipe_name": "菊花茶", "version": 1,
            "components": [{"ingredient": "菊花", "amount": 3, "unit": "g"}],
            "strict_tags": ["孕期禁忌"], "caution_tags": ["虚寒体质"]})
        self.assertEqual(201, status)
        status, _ = self._post("/tasting/recipe-risk-confirmations", {
            "request_id": "c1", "recipe_version_id": "rv1", "risk_note": "首版确认"},
            actor="rev1")
        self.assertEqual(200, status)

    def test_full_flow_over_http(self):
        self._seed_batch_and_recipe()
        status, brew = self._post("/tasting/preparations", {
            "request_id": "p1", "prep_id": "prep-1", "site_id": "site-1",
            "recipe_version_id": "rv1", "quantity_brewed": 3000, "unit": "ml",
            "ingredients": [{"batch_id": "batch-1", "quantity_used": 30}]})
        self.assertEqual(201, status)
        self.assertFalse(brew["replayed"])
        # 重复提交返回稳定回执
        status, replay = self._post("/tasting/preparations", {
            "request_id": "p1", "prep_id": "prep-1", "site_id": "site-1",
            "recipe_version_id": "rv1", "quantity_brewed": 3000, "unit": "ml",
            "ingredients": [{"batch_id": "batch-1", "quantity_used": 30}]})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(brew["prep_id"], replay["prep_id"])

        status, _ = self._post("/tasting/container-fills", {
            "request_id": "f1", "container_id": "cup-1", "prep_id": "prep-1",
            "label": "一号桶", "quantity": 1000, "unit": "ml"})
        self.assertEqual(201, status)
        status, _ = self._post("/tasting/participant-declarations", {
            "request_id": "d1", "participant_id": "g1", "restrictions": ["孕期禁忌"]})
        self.assertEqual(200, status)

        status, assessment = route(self.foundation, self.service, "GET",
                                   "/tasting/assessment?holder_type=container&holder_id=cup-1&participant_id=g1",
                                   None)
        self.assertEqual(200, status)
        self.assertEqual("deny", assessment["decision"])

        status, denied = self._post("/tasting/claims", {
            "request_id": "cl1", "claim_id": "cl1", "holder_type": "container",
            "holder_id": "cup-1", "participant_id": "g1", "quantity": 100, "unit": "ml"})
        self.assertEqual(200, status)
        self.assertEqual("deny", denied["decision"])
        self.assertIsNone(denied["claim_id"])

        status, lineage = route(self.foundation, self.service, "GET",
                                "/tasting/lineage?prep_id=prep-1", None)
        self.assertEqual(200, status)
        self.assertEqual("rv1", lineage["recipe_version"]["recipe_version_id"])
        self.assertTrue(lineage["reconciliation"]["consistent"])

        status, recall = route(self.foundation, self.service, "GET",
                               "/tasting/recall?batch_id=batch-1", None)
        self.assertEqual(200, status)
        self.assertEqual("JH-1", recall["risk_source"]["lot_number"])
        self.assertEqual(1, len(recall["containers"]))

        status, inventory = route(self.foundation, self.service, "GET",
                                  "/tasting/inventory-recompute?site_id=site-1", None)
        self.assertEqual(200, status)
        self.assertTrue(inventory["consistent"])

    def test_foundation_routes_still_work(self):
        status, payload = route(self.foundation, self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_tasting_route_returns_404(self):
        status, payload = route(self.foundation, self.service, "GET", "/tasting/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_validation_error_maps_to_400(self):
        status, payload = self._post("/tasting/ingredient-batches", {
            "request_id": "b1", "batch_id": "bad id!", "site_id": "site-1",
            "ingredient_name": "菊花", "lot_number": "JH-1",
            "quantity_received": 1000, "unit": "g"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_permission_denied_maps_to_403(self):
        self._seed_batch_and_recipe()
        status, payload = self._post("/tasting/ingredient-batch-freezes", {
            "request_id": "fr1", "batch_id": "batch-1", "reason": "x"}, actor="op1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_missing_query_param_returns_400(self):
        status, payload = route(self.foundation, self.service, "GET", "/tasting/recall", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
