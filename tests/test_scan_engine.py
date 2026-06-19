import unittest

from advisor.models import AssetSnapshot, Candle, Fundamentals
from advisor.scan_engine import derive_market_regimes, derive_relative_strength


def candles(start=100, step=1, count=80):
    return [
        Candle(f"2026-01-{(index % 28) + 1:02d}", start + index * step, start + index * step + 1, start + index * step - 1, start + index * step, 1_000_000)
        for index in range(count)
    ]


def snapshot(symbol, asset_type, close_step, count=80):
    return AssetSnapshot(
        symbol=symbol,
        asset_type=asset_type,
        theme="crypto" if asset_type == "crypto" else "software",
        candles=candles(step=close_step, count=count),
        fundamentals=Fundamentals(
            pe=20 if asset_type == "stock" else None,
            peg=1.5 if asset_type == "stock" else None,
            historical_pe=22 if asset_type == "stock" else None,
            revenue_growth=0.15 if asset_type == "stock" else None,
            eps_growth=0.12 if asset_type == "stock" else None,
            margin_trend=0.03 if asset_type == "stock" else None,
            free_cash_flow_positive=True if asset_type == "stock" else None,
            market_cap=50_000_000_000,
            average_volume=5_000_000,
        ),
        funding_rate=0.01 if asset_type == "crypto" else None,
        open_interest_change=0.05 if asset_type == "crypto" else None,
    )


class ScanEngineTests(unittest.TestCase):
    def test_derive_market_regimes_before_asset_scoring(self):
        regimes = derive_market_regimes(
            snapshots=[
                snapshot("MSFT", "stock", 1, count=220),
                snapshot("NVDA", "stock", 1, count=220),
                snapshot("BTC", "crypto", 1, count=220),
                snapshot("ETH", "crypto", 2, count=220),
                snapshot("SOL", "crypto", 2, count=220),
            ],
            benchmarks={"SPY": candles(step=1, count=220), "QQQ": candles(step=1, count=220)},
        )

        self.assertEqual(regimes.stock.label, "risk_on")
        self.assertEqual(regimes.crypto.label, "risk_on")
        self.assertIn("SPY_above_sma50_sma200", regimes.stock.reasons)
        self.assertIn("BTC_above_sma50_sma200", regimes.crypto.reasons)

    def test_derive_market_regimes_returns_neutral_when_evidence_missing(self):
        regimes = derive_market_regimes(snapshots=[], benchmarks={})

        self.assertEqual(regimes.stock.label, "neutral")
        self.assertEqual(regimes.crypto.label, "neutral")
        self.assertIn("insufficient_stock_regime_data", regimes.stock.reasons)
        self.assertIn("insufficient_crypto_regime_data", regimes.crypto.reasons)

    def test_derive_market_regimes_returns_neutral_when_sma200_history_is_missing(self):
        regimes = derive_market_regimes(
            snapshots=[
                snapshot("MSFT", "stock", 1),
                snapshot("BTC", "crypto", 1),
                snapshot("ETH", "crypto", 2),
                snapshot("SOL", "crypto", 2),
            ],
            benchmarks={"SPY": candles(step=1), "QQQ": candles(step=1)},
        )

        self.assertEqual(regimes.stock.label, "neutral")
        self.assertEqual(regimes.crypto.label, "neutral")
        self.assertIn("insufficient_stock_regime_data", regimes.stock.reasons)
        self.assertIn("insufficient_crypto_regime_data", regimes.crypto.reasons)

    def test_derive_relative_strength_uses_qqq_for_stocks_and_btc_for_crypto(self):
        msft = snapshot("MSFT", "stock", 2)
        btc = snapshot("BTC", "crypto", 1)
        eth = snapshot("ETH", "crypto", 3)

        stock_strength = derive_relative_strength(
            msft,
            snapshots=[msft, btc, eth],
            benchmarks={"SPY": candles(step=1), "QQQ": candles(step=1)},
        )
        eth_strength = derive_relative_strength(
            eth,
            snapshots=[msft, btc, eth],
            benchmarks={"SPY": candles(step=1), "QQQ": candles(step=1)},
        )
        btc_strength = derive_relative_strength(
            btc,
            snapshots=[msft, btc, eth],
            benchmarks={"SPY": candles(step=1), "QQQ": candles(step=1)},
        )

        self.assertIsNotNone(stock_strength)
        self.assertGreater(stock_strength, 0)
        self.assertIsNotNone(eth_strength)
        self.assertGreater(eth_strength, 0)
        self.assertIsNone(btc_strength)


if __name__ == "__main__":
    unittest.main()
