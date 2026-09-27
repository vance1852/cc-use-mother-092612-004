"""制备谱系、数量守恒、忌口判定、冻结召回与跨摊转移的核心规则测试。"""

import unittest

from night_market_foundation.errors import ConflictError, PermissionDenied, ValidationError
from night_market_foundation.tea.errors import (
    FrozenError,
    InventoryError,
    ManualConfirmationRequired,
    ServingDenied,
    TransferStateError,
)

from tea_support import build_service, seed_tea


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_preparation_records_batches_and_formula_snapshot(self):
        seed_tea(self.service)
        lineage = self.service.container_lineage("pot-1")
        self.assertEqual(["prep-1"], lineage["preparations"])
        self.assertEqual({"B-gouqi", "B-hongzao"},
                         {item["batch_id"] for item in lineage["material_batches"]})
        self.assertEqual("F-v1", lineage["formulas"][0]["formula_id"])
        self.assertEqual(1, lineage["formulas"][0]["version"])
        prep = self.service.get_preparation("prep-1")
        self.assertEqual("枸杞茶", prep.formula_snapshot["name"])
        self.assertEqual(10, prep.formula_snapshot["ingredients"][0]["amount"])

    def test_lineage_survives_split_and_merge(self):
        seed_tea(self.service)
        self.service.split_container(request_id="sp1", actor_id="op1",
                                     source_container_id="pot-1", new_container_id="cup-1",
                                     amount=30)
        self.service.prepare_tea(
            request_id="p-2", actor_id="op1", preparation_id="prep-2", formula_id="F-v1",
            inputs=[{"material_id": "gouqi", "batch_id": "B-gouqi", "amount": 10},
                    {"material_id": "hongzao", "batch_id": "B-hongzao", "amount": 5}],
            output_amount=40, output_container_id="pot-2", site_id="s1")
        self.service.merge_containers(request_id="m1", actor_id="op1",
                                      source_container_ids=["cup-1", "pot-2"],
                                      target_container_id="pot-big")
        lineage = self.service.container_lineage("pot-big")
        self.assertEqual({"prep-1", "prep-2"}, set(lineage["preparations"]))
        self.assertEqual({"B-gouqi", "B-hongzao"},
                         {item["batch_id"] for item in lineage["material_batches"]})
        # 拆分 1 条（pot-1→cup-1）＋合并两条父边（cup-1、pot-2 → pot-big）
        self.assertEqual(3, len(lineage["edges"]))
        self.assertEqual({"split", "merge"}, {e["relation"] for e in lineage["edges"]})

    def test_split_conserves_quantity(self):
        seed_tea(self.service)
        self.service.split_container(request_id="sp1", actor_id="op1",
                                     source_container_id="pot-1", new_container_id="cup-1",
                                     amount=30)
        self.assertEqual(70, self.service.get_container("pot-1").amount)
        self.assertEqual(30, self.service.get_container("cup-1").amount)
        balance = self.service.recompute_balance("pot-1")
        self.assertTrue(balance.matches)
        self.assertEqual(70, balance.computed_amount)

    def test_split_beyond_balance_rejected(self):
        seed_tea(self.service)
        with self.assertRaises(InventoryError):
            self.service.split_container(request_id="sp-x", actor_id="op1",
                                         source_container_id="pot-1", new_container_id="cup-x",
                                         amount=101)

    def test_merge_conserves_quantity(self):
        seed_tea(self.service)
        self.service.prepare_tea(
            request_id="p-2", actor_id="op1", preparation_id="prep-2", formula_id="F-v1",
            inputs=[{"material_id": "gouqi", "batch_id": "B-gouqi", "amount": 10},
                    {"material_id": "hongzao", "batch_id": "B-hongzao", "amount": 5}],
            output_amount=40, output_container_id="pot-2", site_id="s1")
        self.service.merge_containers(request_id="m1", actor_id="op1",
                                      source_container_ids=["pot-1", "pot-2"],
                                      target_container_id="pot-big")
        self.assertEqual(140, self.service.get_container("pot-big").amount)
        self.assertEqual(0, self.service.get_container("pot-1").amount)
        self.assertEqual("depleted", self.service.get_container("pot-1").disposition)

    def test_loss_reduces_balance_and_is_audited(self):
        seed_tea(self.service)
        self.service.report_loss(request_id="l1", actor_id="op1", container_id="pot-1",
                                 amount=5, reason="洒漏")
        self.assertEqual(95, self.service.get_container("pot-1").amount)
        with self.assertRaises(InventoryError):
            self.service.report_loss(request_id="l2", actor_id="op1", container_id="pot-1",
                                     amount=100, reason="超量")

    def test_inventory_recomputation_holds(self):
        seed_tea(self.service)
        self.service.split_container(request_id="sp1", actor_id="op1",
                                     source_container_id="pot-1", new_container_id="cup-1",
                                     amount=30)
        self.service.upsert_participant(request_id="u1", actor_id="op1",
                                        participant_id="pt-1", restrictions=[])
        self.service.dispense(request_id="d1", actor_id="op1", participant_id="pt-1",
                              container_id="cup-1", amount=10)
        self.service.report_loss(request_id="l1", actor_id="op1", container_id="pot-1",
                                 amount=5, reason="洒漏")
        inventory = self.service.recompute_inventory()
        self.assertTrue(inventory["conserved"])
        self.assertTrue(inventory["identity_holds"])
        self.assertEqual(85, inventory["stock_total"])
        self.assertEqual(100, inventory["produced_total"])
        self.assertEqual(5, inventory["loss_total"])
        self.assertEqual(10, inventory["dispensed_total"])


class FormulaGovernanceTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def tearDown(self):
        self.service.database.close()

    def test_substitute_formula_requires_review_before_use(self):
        seed_tea(self.service)
        self.service.create_formula(
            request_id="f-v2", actor_id="op1", formula_id="F-v2", name="枸杞茶",
            ingredients=[{"material_id": "gouqi", "amount": 12},
                         {"material_id": "hongzao", "amount": 5}],
            contraindications=["孕妇"], cautions=[], supersedes="F-v1")
        self.assertEqual("candidate", self.service.get_formula("F-v2").status)
        with self.assertRaises(ConflictError):
            self.service.prepare_tea(
                request_id="p-bad", actor_id="op1", preparation_id="prep-bad", formula_id="F-v2",
                inputs=[{"material_id": "gouqi", "batch_id": "B-gouqi", "amount": 12},
                        {"material_id": "hongzao", "batch_id": "B-hongzao", "amount": 5}],
                output_amount=10, output_container_id="pot-bad", site_id="s1")

    def test_reviewed_substitute_becomes_usable_and_review_is_final(self):
        seed_tea(self.service)
        self.service.create_formula(
            request_id="f-v2", actor_id="op1", formula_id="F-v2", name="枸杞茶",
            ingredients=[{"material_id": "gouqi", "amount": 12},
                         {"material_id": "hongzao", "amount": 5}],
            contraindications=["孕妇"], cautions=[], supersedes="F-v1")
        self.service.review_formula(request_id="rv-1", actor_id="rv1", formula_id="F-v2",
                                    decision="approved", risk_note="枸杞加量20%，差异可接受")
        self.assertEqual("approved", self.service.get_formula("F-v2").status)
        self.service.prepare_tea(
            request_id="p-2", actor_id="op1", preparation_id="prep-2", formula_id="F-v2",
            inputs=[{"material_id": "gouqi", "batch_id": "B-gouqi", "amount": 12},
                    {"material_id": "hongzao", "batch_id": "B-hongzao", "amount": 5}],
            output_amount=10, output_container_id="pot-2", site_id="s1")
        with self.assertRaises(ConflictError):
            self.service.review_formula(request_id="rv-2", actor_id="rv1", formula_id="F-v2",
                                        decision="rejected", risk_note="改判")

    def test_operator_cannot_review_formula(self):
        seed_tea(self.service)
        self.service.create_formula(
            request_id="f-v2", actor_id="op1", formula_id="F-v2", name="枸杞茶",
            ingredients=[{"material_id": "gouqi", "amount": 12},
                         {"material_id": "hongzao", "amount": 5}],
            contraindications=["孕妇"], cautions=[], supersedes="F-v1")
        with self.assertRaises(PermissionDenied):
            self.service.review_formula(request_id="rv-x", actor_id="op1", formula_id="F-v2",
                                        decision="approved", risk_note="越权")

    def test_same_name_new_version_must_supersede(self):
        seed_tea(self.service)
        with self.assertRaises(ValidationError):
            self.service.create_formula(
                request_id="f-v2", actor_id="op1", formula_id="F-v2", name="枸杞茶",
                ingredients=[{"material_id": "gouqi", "amount": 12},
                             {"material_id": "hongzao", "amount": 5}],
                contraindications=["孕妇"], cautions=[])

    def test_formula_revision_does_not_rewrite_existing_dispense(self):
        seed_tea(self.service)
        self.service.upsert_participant(request_id="u1", actor_id="op1",
                                        participant_id="pt-1", restrictions=[])
        self.service.dispense(request_id="d1", actor_id="op1", participant_id="pt-1",
                              container_id="pot-1", amount=10)
        # 修订配方（v2 替换原料用量并获批）
        self.service.create_formula(
            request_id="f-v2", actor_id="op1", formula_id="F-v2", name="枸杞茶",
            ingredients=[{"material_id": "gouqi", "amount": 12},
                         {"material_id": "hongzao", "amount": 5}],
            contraindications=["孕妇"], cautions=[], supersedes="F-v1")
        self.service.review_formula(request_id="rv-1", actor_id="rv1", formula_id="F-v2",
                                    decision="approved", risk_note="加量")
        record = self.service.get_dispense(
            self.service.list_dispenses(participant_id="pt-1")[0].dispense_id)
        self.assertEqual("F-v1", record.formula_id)
        self.assertEqual("served", record.decision)
        # 原制备的配方快照仍是 v1 的内容
        prep = self.service.get_preparation("prep-1")
        self.assertEqual(1, prep.formula_snapshot["version"])
        self.assertEqual(10, prep.formula_snapshot["ingredients"][0]["amount"])


class ServingSafetyTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        seed_tea(self.service)
        self.service.upsert_participant(request_id="u-preg", actor_id="op1",
                                        participant_id="pt-preg", restrictions=["孕妇"])
        self.service.upsert_participant(request_id="u-fever", actor_id="op1",
                                        participant_id="pt-fever", restrictions=["感冒发热"])
        self.service.upsert_participant(request_id="u-ok", actor_id="op1",
                                        participant_id="pt-ok", restrictions=[])

    def tearDown(self):
        self.service.database.close()

    def test_evaluation_returns_three_levels_with_risk_source(self):
        denied = self.service.evaluate_serving(participant_id="pt-preg", container_id="pot-1")
        self.assertEqual("denied", denied["decision"])
        self.assertEqual("contraindication", denied["reasons"][0]["type"])
        self.assertEqual("F-v1", denied["reasons"][0]["source"]["formula_id"])
        manual = self.service.evaluate_serving(participant_id="pt-fever", container_id="pot-1")
        self.assertEqual("manual_confirm", manual["decision"])
        self.assertEqual("caution", manual["reasons"][0]["type"])
        served = self.service.evaluate_serving(participant_id="pt-ok", container_id="pot-1")
        self.assertEqual("served", served["decision"])
        self.assertEqual([], served["reasons"])

    def test_denied_dispense_blocked_without_stock_change(self):
        with self.assertRaises(ServingDenied):
            self.service.dispense(request_id="d1", actor_id="op1", participant_id="pt-preg",
                                  container_id="pot-1", amount=5)
        self.assertEqual(100, self.service.get_container("pot-1").amount)
        self.assertEqual([], self.service.list_dispenses())

    def test_manual_confirmation_requires_explicit_flag_and_note(self):
        with self.assertRaises(ManualConfirmationRequired) as caught:
            self.service.dispense(request_id="d2", actor_id="op1", participant_id="pt-fever",
                                  container_id="pot-1", amount=5)
        self.assertEqual("caution", caught.exception.evaluation["reasons"][0]["type"])
        with self.assertRaises(ValidationError):
            self.service.dispense(request_id="d2b", actor_id="op1", participant_id="pt-fever",
                                  container_id="pot-1", amount=5, confirm_manual=True)
        receipt = self.service.dispense(request_id="d2c", actor_id="op1",
                                        participant_id="pt-fever", container_id="pot-1",
                                        amount=5, confirm_manual=True,
                                        confirmation_note="体温正常，参与者知情")
        self.assertFalse(receipt.replayed)
        self.assertEqual(95, self.service.get_container("pot-1").amount)
        record = self.service.list_dispenses(participant_id="pt-fever")[0]
        self.assertEqual("manual_confirm", record.decision)
        self.assertTrue(any(r["type"] == "manual_confirmation" for r in record.reasons))

    def test_dispense_idempotent_replay(self):
        first = self.service.dispense(request_id="d1", actor_id="op1", participant_id="pt-ok",
                                      container_id="pot-1", amount=10)
        second = self.service.dispense(request_id="d1", actor_id="op1", participant_id="pt-ok",
                                       container_id="pot-1", amount=10)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(90, self.service.get_container("pot-1").amount)
        self.assertEqual(1, len(self.service.list_dispenses()))


class FreezeRecallTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        seed_tea(self.service)
        self.service.split_container(request_id="sp1", actor_id="op1",
                                     source_container_id="pot-1", new_container_id="cup-1",
                                     amount=30)
        self.service.upsert_participant(request_id="u1", actor_id="op1",
                                        participant_id="pt-1", restrictions=[])
        self.service.dispense(request_id="d1", actor_id="op1", participant_id="pt-1",
                              container_id="cup-1", amount=10)

    def tearDown(self):
        self.service.database.close()

    def test_freeze_reaches_on_site_descendants_across_stalls(self):
        # 把 cup-1 整壶转移到乙摊后再冻结
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "cup-1"}])
        self.service.confirm_transfer(request_id="t1c", actor_id="op2", transfer_id="tr-1")
        self.service.freeze_batch(request_id="fz1", actor_id="rv1", batch_id="B-gouqi",
                                  reason="农残抽检异常")
        self.assertEqual("frozen", self.service.get_container("pot-1").status)
        self.assertEqual("frozen", self.service.get_container("cup-1").status)
        coverage = self.service.recall_coverage("B-gouqi")
        self.assertEqual({"pot-1", "cup-1"}, set(coverage["on_site_frozen_containers"]))
        self.assertEqual("material_batch", coverage["risk_source"]["type"])
        self.assertEqual(["pt-1"], coverage["affected_participant_ids"])
        self.assertEqual(1, len(coverage["dispense_records"]))

    def test_freeze_blocks_future_operations_but_keeps_history(self):
        self.service.freeze_batch(request_id="fz1", actor_id="rv1", batch_id="B-gouqi",
                                  reason="农残抽检异常")
        with self.assertRaises(FrozenError):
            self.service.dispense(request_id="d2", actor_id="op1", participant_id="pt-1",
                                  container_id="cup-1", amount=1)
        with self.assertRaises(FrozenError):
            self.service.split_container(request_id="sp2", actor_id="op1",
                                         source_container_id="pot-1", new_container_id="cup-2",
                                         amount=1)
        with self.assertRaises(FrozenError):
            self.service.prepare_tea(
                request_id="p-2", actor_id="op1", preparation_id="prep-2", formula_id="F-v1",
                inputs=[{"material_id": "gouqi", "batch_id": "B-gouqi", "amount": 10},
                        {"material_id": "hongzao", "batch_id": "B-hongzao", "amount": 5}],
                output_amount=10, output_container_id="pot-2", site_id="s1")
        # 已发生的发放事实原样保留
        history = self.service.list_dispenses()
        self.assertEqual(1, len(history))
        self.assertEqual("served", history[0].decision)
        self.assertEqual(10, history[0].amount)

    def test_double_freeze_rejected(self):
        self.service.freeze_batch(request_id="fz1", actor_id="rv1", batch_id="B-gouqi",
                                  reason="农残抽检异常")
        with self.assertRaises(ConflictError):
            self.service.freeze_batch(request_id="fz2", actor_id="rv1", batch_id="B-gouqi",
                                      reason="重复")

    def test_freeze_idempotent_replay(self):
        first = self.service.freeze_batch(request_id="fz1", actor_id="rv1", batch_id="B-gouqi",
                                          reason="农残抽检异常")
        second = self.service.freeze_batch(request_id="fz1", actor_id="rv1", batch_id="B-gouqi",
                                           reason="农残抽检异常")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        seed_tea(self.service)

    def tearDown(self):
        self.service.database.close()

    def test_transfer_takes_effect_atomically_on_confirmation(self):
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1"}])
        self.assertEqual("s1", self.service.get_container("pot-1").site_id)
        self.service.confirm_transfer(request_id="t1c", actor_id="op2", transfer_id="tr-1")
        container = self.service.get_container("pot-1")
        self.assertEqual("s2", container.site_id)
        self.assertEqual(100, container.amount)
        self.assertTrue(self.service.recompute_balance("pot-1").matches)
        transfer = self.service.get_transfer("tr-1")
        self.assertEqual("confirmed", transfer.status)
        self.assertEqual("op2", transfer.confirmed_by)

    def test_proposer_cannot_confirm_own_transfer(self):
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1"}])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(request_id="t1c", actor_id="op1", transfer_id="tr-1")

    def test_double_confirmation_rejected(self):
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1"}])
        self.service.confirm_transfer(request_id="t1c", actor_id="op2", transfer_id="tr-1")
        with self.assertRaises(TransferStateError):
            self.service.confirm_transfer(request_id="t1c2", actor_id="op2", transfer_id="tr-1")

    def test_rejected_transfer_changes_nothing(self):
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1"}])
        self.service.reject_transfer(request_id="t1r", actor_id="op2", transfer_id="tr-1",
                                     reason="摊位已满")
        self.assertEqual("s1", self.service.get_container("pot-1").site_id)
        self.assertEqual("rejected", self.service.get_transfer("tr-1").status)

    def test_partial_transfer_creates_receiving_container_with_lineage(self):
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1", "amount": 40}])
        self.service.confirm_transfer(request_id="t1c", actor_id="op2", transfer_id="tr-1")
        self.assertEqual(60, self.service.get_container("pot-1").amount)
        inventory = self.service.recompute_inventory()
        self.assertTrue(inventory["conserved"])
        receivers = [c for c in self.service.list_containers(site_id="s2")]
        self.assertEqual(1, len(receivers))
        self.assertEqual(40, receivers[0].amount)
        lineage = self.service.container_lineage(receivers[0].container_id)
        self.assertEqual(["prep-1"], lineage["preparations"])

    def test_confirmation_rechecks_balance(self):
        self.service.propose_transfer(request_id="t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1", "amount": 80}])
        self.service.report_loss(request_id="l1", actor_id="op1", container_id="pot-1",
                                 amount=50, reason="洒漏")
        with self.assertRaises(InventoryError):
            self.service.confirm_transfer(request_id="t1c", actor_id="op2", transfer_id="tr-1")


class OfflineReplayTest(unittest.TestCase):
    """离线补传：重复与乱序消息必须得到稳定结果。"""

    def setUp(self):
        self.service = build_service()
        seed_tea(self.service)
        self.service.upsert_participant(request_id="u1", actor_id="op1",
                                        participant_id="pt-1", restrictions=[])

    def tearDown(self):
        self.service.database.close()

    def test_duplicate_messages_are_stable(self):
        for _ in range(3):
            receipt = self.service.dispense(request_id="off-d1", actor_id="op1",
                                            participant_id="pt-1", container_id="pot-1", amount=10)
        self.assertTrue(receipt.replayed)
        self.assertEqual(90, self.service.get_container("pot-1").amount)
        self.assertEqual(1, len(self.service.list_dispenses()))

    def test_out_of_order_messages_converge(self):
        # 两个等价服务：A 按物理顺序补传，B 让拆分/发放/报损乱序首次到达。
        service_b = build_service()
        seed_tea(service_b)
        service_b.upsert_participant(request_id="u1", actor_id="op1",
                                     participant_id="pt-1", restrictions=[])

        # A：拆分 → 发放 → 报损
        self.service.split_container(request_id="off-sp1", actor_id="op1",
                                     source_container_id="pot-1", new_container_id="cup-1", amount=30)
        self.service.dispense(request_id="off-d1", actor_id="op1", participant_id="pt-1",
                              container_id="pot-1", amount=10)
        self.service.report_loss(request_id="off-l1", actor_id="op1", container_id="pot-1",
                                 amount=5, reason="洒漏")
        # B：同一批消息乱序首次到达（发放 → 报损 → 拆分），编号完全一致
        service_b.dispense(request_id="off-d1", actor_id="op1", participant_id="pt-1",
                           container_id="pot-1", amount=10)
        service_b.report_loss(request_id="off-l1", actor_id="op1", container_id="pot-1",
                              amount=5, reason="洒漏")
        service_b.split_container(request_id="off-sp1", actor_id="op1",
                                  source_container_id="pot-1", new_container_id="cup-1", amount=30)

        for svc in (self.service, service_b):
            self.assertEqual(55, svc.get_container("pot-1").amount)
            self.assertEqual(30, svc.get_container("cup-1").amount)
            self.assertTrue(svc.recompute_inventory()["conserved"])
        # 双方随后再任意重放，结果保持稳定
        self.service.report_loss(request_id="off-l1", actor_id="op1", container_id="pot-1",
                                 amount=5, reason="洒漏")
        service_b.split_container(request_id="off-sp1", actor_id="op1",
                                  source_container_id="pot-1", new_container_id="cup-1", amount=30)
        self.assertEqual(55, self.service.get_container("pot-1").amount)
        self.assertEqual(55, service_b.get_container("pot-1").amount)
        service_b.database.close()

    def test_request_id_payload_conflict_rejected(self):
        self.service.dispense(request_id="off-d1", actor_id="op1", participant_id="pt-1",
                              container_id="pot-1", amount=10)
        with self.assertRaises(ConflictError):
            self.service.dispense(request_id="off-d1", actor_id="op1", participant_id="pt-1",
                                  container_id="pot-1", amount=20)

    def test_replay_stays_stable_after_state_changes(self):
        """原始写入改变了状态（余额、冻结、转移归属）后，同一请求重放仍返回原回执。"""
        # 发放后容器被冻结：重放该发放请求仍返回原回执
        first = self.service.dispense(request_id="off-d1", actor_id="op1", participant_id="pt-1",
                                      container_id="pot-1", amount=10)
        self.service.freeze_batch(request_id="off-fz", actor_id="rv1", batch_id="B-gouqi",
                                  reason="抽检异常")
        replay = self.service.dispense(request_id="off-d1", actor_id="op1", participant_id="pt-1",
                                       container_id="pot-1", amount=10)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        self.assertEqual(1, len(self.service.list_dispenses()))

    def test_split_replay_after_source_frozen(self):
        first = self.service.split_container(request_id="off-sp1", actor_id="op1",
                                             source_container_id="pot-1", new_container_id="cup-1",
                                             amount=30)
        self.service.freeze_batch(request_id="off-fz", actor_id="rv1", batch_id="B-gouqi",
                                  reason="抽检异常")
        replay = self.service.split_container(request_id="off-sp1", actor_id="op1",
                                              source_container_id="pot-1", new_container_id="cup-1",
                                              amount=30)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)

    def test_transfer_confirm_replay_is_stable(self):
        self.service.propose_transfer(request_id="off-t1", actor_id="op1", transfer_id="tr-1",
                                      from_site_id="s1", to_site_id="s2",
                                      items=[{"container_id": "pot-1"}])
        first = self.service.confirm_transfer(request_id="off-t1c", actor_id="op2",
                                              transfer_id="tr-1")
        replay = self.service.confirm_transfer(request_id="off-t1c", actor_id="op2",
                                               transfer_id="tr-1")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual("s2", self.service.get_container("pot-1").site_id)
        # 提议重放在确认之后也稳定（来源容器归属已变化）
        propose_replay = self.service.propose_transfer(request_id="off-t1", actor_id="op1",
                                                       transfer_id="tr-1", from_site_id="s1",
                                                       to_site_id="s2",
                                                       items=[{"container_id": "pot-1"}])
        self.assertTrue(propose_replay.replayed)


if __name__ == "__main__":
    unittest.main()
