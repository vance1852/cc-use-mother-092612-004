import unittest

from night_market_tasting.acceptance import run


class TastingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("review", result["review_claim_decision"])
        self.assertEqual("deny", result["denied_claim_decision"])
        self.assertEqual("deny", result["frozen_claim_decision"])
        self.assertTrue(result["replayed_brew"])
        self.assertTrue(result["lineage_consistent"])
        self.assertEqual(3, result["lineage_containers"])
        self.assertEqual(3, result["recall_containers"])
        self.assertEqual(2, result["recall_claims"])
        self.assertEqual(500.0, result["recall_distributed"])
        self.assertTrue(result["recompute_tasting_consistent"])
        self.assertTrue(result["recompute_east_consistent"])


if __name__ == "__main__":
    unittest.main()
