"""How certificate values are written, and the reference number's shape.

    PYTHONPATH=app python -m unittest discover -s tests
"""
import os
import sys
import unittest
from datetime import date
from pathlib import Path

for _key, _value in {"SERVICE_TOKEN": "test-service-token", "API_TOKEN_PEPPER": "pepper",
                     "MS_WEBHOOK_SECRET": "x"}.items():
    os.environ.setdefault(_key, _value)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import settings                                   # noqa: E402
from Certificates import numbering               # noqa: E402
from Certificates.formatting import (join_names, long_date, money, percent_number,  # noqa: E402
                                     percent_words, roman)


class Formatting(unittest.TestCase):
    def test_money(self):
        self.assertEqual(money(62605200, "USD"), "USD 62,605,200")
        self.assertEqual(money(62605200.00, "EUR"), "EUR 62,605,200")
        self.assertEqual(money(1250.5, "GBP"), "GBP 1,250.50")

    def test_dates_are_long_form_with_two_digit_day(self):
        self.assertEqual(long_date(date(2024, 8, 5)), "05 August 2024")
        self.assertEqual(long_date(date(2026, 3, 18)), "18 March 2026")

    def test_percent(self):
        self.assertEqual(percent_number(97.5), "97.5")
        self.assertEqual(percent_number(100), "100")
        self.assertEqual(percent_words(97.5), "NINETY SEVEN AND A HALF PERCENT")
        self.assertEqual(percent_words(100), "ONE HUNDRED PERCENT")
        self.assertEqual(percent_words(12.25), "TWELVE POINT TWO FIVE PERCENT")
        self.assertEqual(percent_words(40), "FORTY PERCENT")

    def test_lists(self):
        self.assertEqual(join_names(["A"]), "A")
        self.assertEqual(join_names(["A", "B"]), "A and B")
        self.assertEqual(join_names(["A", "B", "C"]), "A, B and C")
        self.assertEqual([roman(i) for i in (1, 2, 3, 4, 9, 12)], ["i", "ii", "iii", "iv", "ix", "xii"])


class Reference(unittest.TestCase):
    def test_format(self):
        self.assertEqual(numbering.reference_number(2025, "SCAT", 63), "CY25/SCAT/00063")
        self.assertEqual(numbering.reference_number(2030, "AB-1", 12345), "CY30/AB-1/12345")

    def test_contract_year_is_the_period_from_year(self):
        self.assertEqual(numbering.contract_year(date(2025, 4, 12)), 2025)

    def test_counter_scope_follows_the_mode(self):
        saved = settings.CERTIFICATE_COUNTER_MODE
        try:
            settings.CERTIFICATE_COUNTER_MODE = "per_type"
            self.assertEqual(numbering.counter_scope("reinsurance"), "reinsurance")
            self.assertEqual(numbering._related("reinsurance"), ["reinsurance", "shared"])
            settings.CERTIFICATE_COUNTER_MODE = "shared"
            self.assertEqual(numbering.counter_scope("insurance"), "shared")
            self.assertEqual(set(numbering._related("shared")), {"shared", "reinsurance", "insurance"})
        finally:
            settings.CERTIFICATE_COUNTER_MODE = saved


if __name__ == "__main__":
    unittest.main()
