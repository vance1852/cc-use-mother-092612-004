import unittest

from night_market_foundation.tea.acceptance import run


class TeaAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["inventory_conserved"])
        self.assertEqual("denied", result["denied_decision"])
        self.assertTrue(result["manual_raised"])
        self.assertEqual("confirmed", result["transfer_status"])
        self.assertEqual("site-tea", result["jug_site"])
        self.assertEqual(["jug-001", "pot-001"], result["frozen_containers"])
        self.assertEqual(["visitor-001", "visitor-003"], result["recall_participants"])
        self.assertEqual(2, result["lineage_formula_version"])
        self.assertEqual(2, result["history_dispenses"])


if __name__ == "__main__":
    unittest.main()
