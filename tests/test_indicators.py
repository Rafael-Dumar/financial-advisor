import math
import unittest

from advisor.indicators import atr, ema, percent_returns, rsi, sma


class IndicatorTests(unittest.TestCase):
    def test_sma_ema_rsi_atr_and_returns_are_calculated(self):
        closes = [10, 11, 12, 11, 13, 14, 15, 14, 16, 17, 18, 17, 19, 20, 21]
        highs = [value + 1 for value in closes]
        lows = [value - 1 for value in closes]

        self.assertEqual(sma(closes, 3)[:4], [None, None, 11.0, 11.333333333333334])
        self.assertAlmostEqual(ema(closes, 3)[1], 10.5)
        self.assertGreater(rsi(closes, 14)[-1], 70)
        self.assertGreater(atr(highs, lows, closes, 14)[-1], 2)
        self.assertTrue(math.isclose(percent_returns(closes)[1], 0.10))


if __name__ == "__main__":
    unittest.main()
