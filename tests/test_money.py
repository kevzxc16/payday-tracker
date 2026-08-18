"""Tests for app.services.money — format_cents + parse_dollars."""
import unittest

from app.services.money import format_cents, parse_dollars


class FormatCentsTests(unittest.TestCase):
    def test_basic_amounts(self):
        self.assertEqual(format_cents(0), "$0.00")
        self.assertEqual(format_cents(100), "$1.00")
        self.assertEqual(format_cents(1234), "$12.34")
        self.assertEqual(format_cents(123456), "$1,234.56")
        self.assertEqual(format_cents(1234567890), "$12,345,678.90")

    def test_negatives(self):
        self.assertEqual(format_cents(-100), "-$1.00")
        self.assertEqual(format_cents(-1234), "-$12.34")

    def test_none(self):
        self.assertEqual(format_cents(None), "—")

    def test_no_symbol(self):
        self.assertEqual(format_cents(12345, with_symbol=False), "123.45")

    def test_trailing_zero(self):
        # Two cents → "$0.02"
        self.assertEqual(format_cents(2), "$0.02")


class ParseDollarsTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_dollars("12.34"), 1234)
        self.assertEqual(parse_dollars("0"), 0)
        self.assertEqual(parse_dollars("100"), 10000)
        self.assertEqual(parse_dollars("100.00"), 10000)

    def test_with_symbol_and_commas(self):
        self.assertEqual(parse_dollars("$1,234.56"), 123456)
        self.assertEqual(parse_dollars(" $ 1,234.56 "), 123456)
        self.assertEqual(parse_dollars("$0.99"), 99)

    def test_negative(self):
        self.assertEqual(parse_dollars("-50.00"), -5000)
        self.assertEqual(parse_dollars("-$0.01"), -1)

    def test_one_decimal(self):
        self.assertEqual(parse_dollars("5.5"), 550)

    def test_truncate_extra_decimals(self):
        self.assertEqual(parse_dollars("1.999"), 199)  # truncate, don't round

    def test_invalid(self):
        for bad in ("", "abc", "12.34.56", "$", "1.2.3", None):
            with self.assertRaises(ValueError):
                parse_dollars(bad)

    def test_round_trip(self):
        # parse → format → parse cycle should be lossless for valid amounts
        cases = ["0.00", "0.01", "1.23", "12.34", "1,234.56", "999,999.99"]
        for c in cases:
            cents = parse_dollars(c)
            formatted = format_cents(cents)
            cents2 = parse_dollars(formatted)
            self.assertEqual(cents, cents2, f"round-trip failed for {c}")


if __name__ == "__main__":
    unittest.main()
