import unittest
from datetime import datetime, timezone

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from night_market_tasting.service import TastingService


class TastingServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.service = TastingService(self.database, clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="活动机构")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                       display_name="品鉴区负责人", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                       display_name="东摊负责人", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="rev", actor_id="a1", new_actor_id="rev1",
                                       display_name="审核者", role="reviewer", organization_id="o1")
        self.foundation.register_site(request_id="s1", actor_id="a1", site_id="site-1",
                                      organization_id="o1", name="品鉴区", timezone_name="Asia/Shanghai")
        self.foundation.register_site(request_id="s2", actor_id="a1", site_id="site-2",
                                      organization_id="o1", name="东摊", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _make_active_recipe(self, version_id="rv1", version=1, supersedes=None,
                            strict=("孕期禁忌",), caution=("虚寒体质",)):
        self.service.register_ingredient_batch(
            request_id=f"batch-gouqi-{version_id}", actor_id="op1", batch_id="batch-gouqi",
            site_id="site-1", ingredient_name="枸杞", lot_number="GQ-1",
            quantity_received=5000, unit="g")
        self.service.register_ingredient_batch(
            request_id=f"batch-juhua-{version_id}", actor_id="op1", batch_id="batch-juhua",
            site_id="site-1", ingredient_name="菊花", lot_number="JH-1",
            quantity_received=3000, unit="g", caution_tags=["虚寒体质"])
        self.service.create_recipe_version(
            request_id=f"recipe-{version_id}", actor_id="op1", recipe_version_id=version_id,
            site_id="site-1", recipe_name="菊花枸杞茶", version=version,
            components=[{"ingredient": "菊花", "amount": 3, "unit": "g"},
                        {"ingredient": "枸杞", "amount": 5, "unit": "g"}],
            strict_tags=list(strict), caution_tags=list(caution),
            supersedes_version_id=supersedes)
        self.service.confirm_recipe_risk(
            request_id=f"confirm-{version_id}", actor_id="rev1",
            recipe_version_id=version_id, risk_note="风险标签差异已确认")

    def _brew(self, prep_id="prep-1", recipe_version_id="rv1", brewed=5000,
              juhua_used=30, gouqi_used=50):
        return self.service.brew_preparation(
            request_id=f"brew-{prep_id}", actor_id="op1", prep_id=prep_id, site_id="site-1",
            recipe_version_id=recipe_version_id, quantity_brewed=brewed, unit="ml",
            ingredients=[{"batch_id": "batch-juhua", "quantity_used": juhua_used},
                         {"batch_id": "batch-gouqi", "quantity_used": gouqi_used}])

    # ------------------------------------------------------------------
    # 制备谱系
    # ------------------------------------------------------------------

    def test_lineage_records_batches_recipe_and_containers(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="fill-1", actor_id="op1", container_id="cup-1",
                                    prep_id="prep-1", label="一号桶", quantity=2000, unit="ml")
        lineage = self.service.get_preparation_lineage("prep-1")
        self.assertEqual("rv1", lineage["recipe_version"]["recipe_version_id"])
        self.assertEqual("active", lineage["recipe_version"]["status"])
        lots = {row["ingredient_name"]: row["lot_number"] for row in lineage["ingredient_batches"]}
        self.assertEqual({"菊花": "JH-1", "枸杞": "GQ-1"}, lots)
        self.assertEqual(["cup-1"], [c["container_id"] for c in lineage["containers"]])
        self.assertTrue(lineage["reconciliation"]["consistent"])

    def test_pending_recipe_cannot_brew(self):
        self.service.register_ingredient_batch(
            request_id="b1", actor_id="op1", batch_id="batch-juhua", site_id="site-1",
            ingredient_name="菊花", lot_number="JH-1", quantity_received=3000, unit="g")
        self.service.create_recipe_version(
            request_id="r1", actor_id="op1", recipe_version_id="rv-draft", site_id="site-1",
            recipe_name="菊花茶", version=1,
            components=[{"ingredient": "菊花", "amount": 3, "unit": "g"}])
        with self.assertRaises(ValidationError):
            self.service.brew_preparation(
                request_id="brew-x", actor_id="op1", prep_id="prep-x", site_id="site-1",
                recipe_version_id="rv-draft", quantity_brewed=1000, unit="ml",
                ingredients=[{"batch_id": "batch-juhua", "quantity_used": 30}])

    def test_brew_ingredients_must_match_recipe_components(self):
        self._make_active_recipe()
        with self.assertRaises(ValidationError):
            self.service.brew_preparation(
                request_id="brew-bad", actor_id="op1", prep_id="prep-bad", site_id="site-1",
                recipe_version_id="rv1", quantity_brewed=1000, unit="ml",
                ingredients=[{"batch_id": "batch-juhua", "quantity_used": 30}])

    def test_ingredient_batch_stock_cannot_be_overused(self):
        self._make_active_recipe()
        self._brew(prep_id="prep-1", juhua_used=2900, gouqi_used=50)
        with self.assertRaises(ValidationError):
            self._brew(prep_id="prep-2", juhua_used=200, gouqi_used=50)

    # ------------------------------------------------------------------
    # 配方版本与既有事实
    # ------------------------------------------------------------------

    def test_recipe_revision_requires_risk_confirmation_and_keeps_past_claims(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="fill-1", actor_id="op1", container_id="cup-1",
                                    prep_id="prep-1", label="一号桶", quantity=2000, unit="ml")
        self.service.record_claim(request_id="claim-old", actor_id="op1", claim_id="claim-old",
                                  holder_type="container", holder_id="cup-1",
                                  participant_id="guest-x", quantity=200, unit="ml")
        # 新版去掉了孕期禁忌：创建后待审核，不能立即用于制备
        self.service.create_recipe_version(
            request_id="recipe-v2", actor_id="op1", recipe_version_id="rv2", site_id="site-1",
            recipe_name="菊花枸杞茶", version=2,
            components=[{"ingredient": "菊花", "amount": 2, "unit": "g"},
                        {"ingredient": "枸杞", "amount": 5, "unit": "g"}],
            caution_tags=["虚寒体质"], supersedes_version_id="rv1")
        with self.assertRaises(ValidationError):
            self._brew(prep_id="prep-new", recipe_version_id="rv2")
        # 审核者确认风险差异后才能用于之后的制备
        confirm = self.service.confirm_recipe_risk(
            request_id="confirm-v2", actor_id="rev1", recipe_version_id="rv2",
            risk_note="孕期禁忌标签移除，已人工确认差异可接受")
        self.assertEqual(["孕期禁忌"], confirm["risk_diff"]["strict_removed"])
        self._brew(prep_id="prep-new", recipe_version_id="rv2", juhua_used=20, gouqi_used=50)
        # 既有领取事实仍指向 v1，不随修订改变
        lineage = self.service.get_preparation_lineage("prep-1")
        old_claim = next(c for c in lineage["claims"] if c["claim_id"] == "claim-old")
        self.assertEqual("rv1", old_claim["recipe_version_id"])
        new_lineage = self.service.get_preparation_lineage("prep-new")
        self.assertEqual("rv2", new_lineage["recipe_version"]["recipe_version_id"])
        # 历史版本行仍可查且保持 active
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM recipe_versions WHERE recipe_version_id='rv1' AND status='active'"
        ).fetchone()[0])

    def test_non_reviewer_cannot_confirm_recipe(self):
        self.service.register_ingredient_batch(
            request_id="b1", actor_id="op1", batch_id="batch-juhua", site_id="site-1",
            ingredient_name="菊花", lot_number="JH-1", quantity_received=3000, unit="g")
        self.service.create_recipe_version(
            request_id="r1", actor_id="op1", recipe_version_id="rv-x", site_id="site-1",
            recipe_name="菊花茶", version=1,
            components=[{"ingredient": "菊花", "amount": 3, "unit": "g"}])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_recipe_risk(
                request_id="c1", actor_id="op1", recipe_version_id="rv-x", risk_note="x")

    # ------------------------------------------------------------------
    # 数量守恒
    # ------------------------------------------------------------------

    def test_quantity_conservation_through_split_merge_loss_and_claims(self):
        self._make_active_recipe()
        self._brew(brewed=5000)
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=2000, unit="ml")
        self.service.fill_container(request_id="f2", actor_id="op1", container_id="c2",
                                    prep_id="prep-1", label="桶2", quantity=1000, unit="ml")
        self.service.split_container(request_id="sp1", actor_id="op1", source_container_id="c1",
                                     new_container_id="c3", label="小壶", quantity=500)
        self.service.record_loss(request_id="l1", actor_id="op1", holder_type="container",
                                 holder_id="c3", quantity=100, unit="ml", reason="洒漏")
        self.service.record_claim(request_id="cl1", actor_id="op1", claim_id="cl1",
                                  holder_type="container", holder_id="c3",
                                  participant_id="g1", quantity=200, unit="ml")
        self.service.merge_containers(request_id="m1", actor_id="op1",
                                      source_container_id="c3", target_container_id="c2")
        inventory = self.service.recompute_inventory("site-1")
        self.assertTrue(inventory["consistent"], inventory)
        recon = self.service.get_preparation_lineage("prep-1")["reconciliation"]
        self.assertEqual(5000, recon["brewed"])
        self.assertEqual(2000, recon["remaining"])          # 5000 - 2000 - 1000
        self.assertEqual(2700, recon["in_containers"])      # c1 1500 + c2 1200（c3 已并入）
        self.assertEqual(200, recon["distributed"])
        self.assertEqual(100, recon["lost"])
        self.assertTrue(recon["consistent"])

    def test_cannot_loss_or_claim_more_than_holding(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶", quantity=500, unit="ml")
        with self.assertRaises(ValidationError):
            self.service.record_loss(request_id="l1", actor_id="op1", holder_type="container",
                                     holder_id="c1", quantity=600, unit="ml", reason="x")
        with self.assertRaises(ValidationError):
            self.service.record_claim(request_id="cl1", actor_id="op1", claim_id="cl1",
                                      holder_type="container", holder_id="c1",
                                      participant_id="g1", quantity=600, unit="ml")

    def test_recompute_detects_tampered_quantity(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶", quantity=1000, unit="ml")
        self.database.connection.execute(
            "UPDATE containers SET quantity=900 WHERE container_id='c1'")
        inventory = self.service.recompute_inventory("site-1")
        self.assertFalse(inventory["consistent"])
        self.assertEqual(1, len(inventory["discrepancies"]))
        self.assertEqual("c1", inventory["discrepancies"][0]["holder_id"])

    def test_merge_only_within_same_preparation(self):
        self._make_active_recipe()
        self._brew(prep_id="prep-1")
        self._brew(prep_id="prep-2")
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=500, unit="ml")
        self.service.fill_container(request_id="f2", actor_id="op1", container_id="c2",
                                    prep_id="prep-2", label="桶2", quantity=500, unit="ml")
        with self.assertRaises(ValidationError):
            self.service.merge_containers(request_id="m1", actor_id="op1",
                                          source_container_id="c1", target_container_id="c2")

    # ------------------------------------------------------------------
    # 忌口判定
    # ------------------------------------------------------------------

    def test_assessment_allow_review_deny_with_sources(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶", quantity=2000, unit="ml")
        self.service.declare_restrictions(request_id="d1", actor_id="op1",
                                          participant_id="g-allow", restrictions=["海鲜过敏"])
        self.service.declare_restrictions(request_id="d2", actor_id="op1",
                                          participant_id="g-cold", restrictions=["虚寒体质"])
        self.service.declare_restrictions(request_id="d3", actor_id="op1",
                                          participant_id="g-preg", restrictions=["孕期禁忌"])
        allow = self.service.assess(holder_type="container", holder_id="c1",
                                    participant_id="g-allow")
        review = self.service.assess(holder_type="container", holder_id="c1",
                                     participant_id="g-cold")
        deny = self.service.assess(holder_type="container", holder_id="c1",
                                   participant_id="g-preg")
        self.assertEqual("allow", allow["decision"])
        self.assertEqual("review", review["decision"])
        self.assertEqual("deny", deny["decision"])
        sources = review["matched_caution"][0]["sources"]
        kinds = {s["source_type"] for s in sources}
        self.assertEqual({"recipe_version", "ingredient_batch"}, kinds)
        strict_sources = deny["matched_strict"][0]["sources"]
        self.assertEqual("recipe_version", strict_sources[0]["source_type"])

    def test_review_claim_needs_confirmation_and_deny_never_persists(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶", quantity=2000, unit="ml")
        self.service.declare_restrictions(request_id="d2", actor_id="op1",
                                          participant_id="g-cold", restrictions=["虚寒体质"])
        self.service.declare_restrictions(request_id="d3", actor_id="op1",
                                          participant_id="g-preg", restrictions=["孕期禁忌"])
        pending = self.service.record_claim(
            request_id="cl-review", actor_id="op1", claim_id="cl-review",
            holder_type="container", holder_id="c1", participant_id="g-cold",
            quantity=300, unit="ml")
        self.assertEqual("review", pending["decision"])
        self.assertIsNone(pending["claim_id"])
        # 未人工确认不扣减库存
        self.assertEqual(2000, self.service.get_container("c1")["quantity"])
        confirmed = self.service.record_claim(
            request_id="cl-review-ok", actor_id="op1", claim_id="cl-review-ok",
            holder_type="container", holder_id="c1", participant_id="g-cold",
            quantity=300, unit="ml", manual_confirmed=True)
        self.assertEqual("review", confirmed["decision"])  # 人工确认后按需确认落账
        self.assertEqual("cl-review-ok", confirmed["claim_id"])
        self.assertEqual(1700, self.service.get_container("c1")["quantity"])
        denied = self.service.record_claim(
            request_id="cl-deny", actor_id="op1", claim_id="cl-deny",
            holder_type="container", holder_id="c1", participant_id="g-preg",
            quantity=300, unit="ml", manual_confirmed=True)
        self.assertEqual("deny", denied["decision"])
        self.assertIsNone(denied["claim_id"])
        self.assertEqual(1700, self.service.get_container("c1")["quantity"])
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM claims").fetchone()[0])

    # ------------------------------------------------------------------
    # 冻结与召回
    # ------------------------------------------------------------------

    def test_freeze_batch_blocks_future_claims_but_keeps_history(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=2000, unit="ml")
        self.service.fill_container(request_id="f2", actor_id="op1", container_id="c2",
                                    prep_id="prep-1", label="桶2", quantity=1000, unit="ml")
        self.service.record_claim(request_id="cl1", actor_id="op1", claim_id="cl1",
                                  holder_type="container", holder_id="c1",
                                  participant_id="g1", quantity=400, unit="ml")
        result = self.service.freeze_ingredient_batch(
            request_id="fr1", actor_id="rev1", batch_id="batch-juhua", reason="农残超标")
        self.assertEqual(["prep-1"], result["frozen_preparations"])
        self.assertCountEqual(["c1", "c2"], result["frozen_containers"])
        self.assertEqual("frozen", self.service.get_container("c1")["status"])
        # 未来发放被阻断，且不产生新记录、不动数量
        blocked = self.service.record_claim(
            request_id="cl2", actor_id="op1", claim_id="cl2",
            holder_type="container", holder_id="c1", participant_id="g1",
            quantity=100, unit="ml")
        self.assertEqual("deny", blocked["decision"])
        self.assertTrue(any(b["reason"] == "holder_frozen"
                            for b in blocked["explanation"]["blocking"]))
        self.assertEqual(1600, self.service.get_container("c1")["quantity"])
        # 已发生记录保留
        recall = self.service.get_recall_coverage("batch-juhua")
        self.assertEqual("农残超标", recall["risk_source"]["freeze_reason"])
        self.assertEqual(["cl1"], [c["claim_id"] for c in recall["claims"]])
        self.assertEqual(400, recall["totals"]["distributed"])
        self.assertCountEqual(["c1", "c2"], [c["container_id"] for c in recall["containers"]])
        # 冻结的原料不能再用于新制备
        with self.assertRaises(ValidationError):
            self._brew(prep_id="prep-2")
        # 重复冻结是稳定的冲突
        with self.assertRaises(ConflictError):
            self.service.freeze_ingredient_batch(
                request_id="fr2", actor_id="rev1", batch_id="batch-juhua", reason="再次冻结")

    def test_freeze_cancels_pending_transfer(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=1000, unit="ml")
        self.service.initiate_transfer(request_id="t1", actor_id="op1", transfer_id="t1",
                                       container_id="c1", to_site_id="site-2")
        result = self.service.freeze_ingredient_batch(
            request_id="fr1", actor_id="rev1", batch_id="batch-juhua", reason="异常")
        self.assertEqual(["t1"], result["cancelled_transfers"])
        self.assertEqual("cancelled", self.service.get_transfer("t1")["status"])
        self.assertEqual("frozen", self.service.get_container("c1")["status"])

    # ------------------------------------------------------------------
    # 跨摊位转移
    # ------------------------------------------------------------------

    def test_transfer_requires_both_parties_and_takes_effect_atomically(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=1000, unit="ml")
        self.service.initiate_transfer(request_id="t1", actor_id="op1", transfer_id="t1",
                                       container_id="c1", to_site_id="site-2")
        # 待确认期间不能发放
        blocked = self.service.record_claim(
            request_id="clx", actor_id="op1", claim_id="clx",
            holder_type="container", holder_id="c1", participant_id="g1",
            quantity=100, unit="ml")
        self.assertEqual("deny", blocked["decision"])
        # 发起方不能自己确认
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(request_id="tc-bad", actor_id="op1", transfer_id="t1")
        confirmed = self.service.confirm_transfer(request_id="tc1", actor_id="op2",
                                                  transfer_id="t1")
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual("site-2", self.service.get_container("c1")["site_id"])
        self.assertEqual("on_site", self.service.get_container("c1")["status"])
        # 台账整体守恒：出场 + 入场成对出现
        connection = self.database.connection
        out_sum = connection.execute(
            "SELECT COALESCE(SUM(delta),0) FROM stock_entries WHERE entry_type='transfer_out'"
        ).fetchone()[0]
        in_sum = connection.execute(
            "SELECT COALESCE(SUM(delta),0) FROM stock_entries WHERE entry_type='transfer_in'"
        ).fetchone()[0]
        self.assertEqual(-1000, out_sum)
        self.assertEqual(1000, in_sum)
        # 重复确认结果稳定
        with self.assertRaises(ConflictError):
            self.service.confirm_transfer(request_id="tc2", actor_id="op2", transfer_id="t1")
        # 两侧场所库存各自复算一致
        self.assertTrue(self.service.recompute_inventory("site-1")["consistent"])
        self.assertTrue(self.service.recompute_inventory("site-2")["consistent"])

    def test_transfer_rejection_returns_container(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=1000, unit="ml")
        self.service.initiate_transfer(request_id="t1", actor_id="op1", transfer_id="t1",
                                       container_id="c1", to_site_id="site-2")
        rejected = self.service.reject_transfer(request_id="tr1", actor_id="op2",
                                                transfer_id="t1", reason="对方已收摊")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("on_site", self.service.get_container("c1")["status"])
        self.assertEqual("site-1", self.service.get_container("c1")["site_id"])

    # ------------------------------------------------------------------
    # 离线补传：重复与乱序
    # ------------------------------------------------------------------

    def test_duplicate_messages_are_idempotent_even_after_state_changes(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=2000, unit="ml")
        first = self.service.record_claim(
            request_id="cl1", actor_id="op1", claim_id="cl1",
            holder_type="container", holder_id="c1", participant_id="g1",
            quantity=300, unit="ml")
        # 之后批次冻结、状态变化，重复补传仍返回首次结果且不重复扣减
        self.service.freeze_ingredient_batch(request_id="fr1", actor_id="rev1",
                                             batch_id="batch-juhua", reason="异常")
        replay = self.service.record_claim(
            request_id="cl1", actor_id="op1", claim_id="cl1",
            holder_type="container", holder_id="c1", participant_id="g1",
            quantity=300, unit="ml")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["claim_id"], replay["claim_id"])
        self.assertEqual(1700, self.service.get_container("c1")["quantity"])
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM claims").fetchone()[0])
        # 相同 request_id 不同内容视为冲突
        with self.assertRaises(ConflictError):
            self.service.record_claim(
                request_id="cl1", actor_id="op1", claim_id="cl1",
                holder_type="container", holder_id="c1", participant_id="g1",
                quantity=999, unit="ml")

    def test_out_of_order_confirm_then_initiate_settles(self):
        self._make_active_recipe()
        self._brew()
        self.service.fill_container(request_id="f1", actor_id="op1", container_id="c1",
                                    prep_id="prep-1", label="桶1", quantity=1000, unit="ml")
        # 确认消息先于发起消息到达：引用尚不存在
        with self.assertRaises(NotFoundError):
            self.service.confirm_transfer(request_id="tc1", actor_id="op2", transfer_id="t1")
        # 发起消息补达
        self.service.initiate_transfer(request_id="t1", actor_id="op1", transfer_id="t1",
                                       container_id="c1", to_site_id="site-2")
        # 原确认消息重发后整体生效，再次重放仍稳定
        confirmed = self.service.confirm_transfer(request_id="tc1", actor_id="op2",
                                                  transfer_id="t1")
        self.assertEqual("confirmed", confirmed["status"])
        replay = self.service.confirm_transfer(request_id="tc1", actor_id="op2",
                                               transfer_id="t1")
        self.assertTrue(replay["replayed"])

    # ------------------------------------------------------------------
    # 权限与边界
    # ------------------------------------------------------------------

    def test_reviewer_cannot_brew(self):
        self._make_active_recipe()
        with self.assertRaises(PermissionDenied):
            self.service.brew_preparation(
                request_id="brew-x", actor_id="rev1", prep_id="prep-x", site_id="site-1",
                recipe_version_id="rv1", quantity_brewed=1000, unit="ml",
                ingredients=[{"batch_id": "batch-juhua", "quantity_used": 30},
                             {"batch_id": "batch-gouqi", "quantity_used": 50}])

    def test_operator_cannot_freeze_batch(self):
        self._make_active_recipe()
        with self.assertRaises(PermissionDenied):
            self.service.freeze_ingredient_batch(
                request_id="fr1", actor_id="op1", batch_id="batch-juhua", reason="x")

    def test_unknown_lineage_returns_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.get_preparation_lineage("missing")


if __name__ == "__main__":
    unittest.main()
