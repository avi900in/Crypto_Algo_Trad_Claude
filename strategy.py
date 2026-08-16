import pandas as pd
import numpy as np


class BreakoutStrategy:
    def __init__(
        self,
        rsi_period=14,
        ema_period=20,
        trend_ema_period=50,   # NEW: macro trend filter length
        volume_period=20,
        atr_period=14,
        min_stop_loss_pct=1.2,   # NEW: raised floor (was 0.5) — crypto noise routinely exceeds 0.5%
        max_stop_loss_pct=5.0,
        min_take_profit_pct=2.4,  # kept at ~2x the SL floor
        max_take_profit_pct=10.0,
    ):
        self.rsi_period = rsi_period
        self.ema_period = ema_period
        self.trend_ema_period = trend_ema_period
        self.volume_period = volume_period
        self.atr_period = atr_period
        self.min_stop_loss_pct = min_stop_loss_pct
        self.max_stop_loss_pct = max_stop_loss_pct
        self.min_take_profit_pct = min_take_profit_pct
        self.max_take_profit_pct = max_take_profit_pct

    def min_required_bars(self):
        """
        Minimum number of candles needed before indicators are meaningful.
        Callers (e.g. bot.py) should fetch comfortably more than this so the
        EMA/RSI/ATR values have actually converged, not just barely qualify.
        """
        return max(self.rsi_period, self.trend_ema_period, self.volume_period, self.atr_period) + 5

    def calculate_indicators(self, df):
        """
        Expects a pandas DataFrame with columns: ['open', 'high', 'low', 'close', 'volume']
        """
        min_len = self.min_required_bars()

        if len(df) < min_len:
            # BUG (fixed): this placeholder path used to be reachable from
            # analyze() because analyze()'s own guard only checked against
            # trend_ema_period (50), not the true min_len used here (55+).
            # That let a 50-bar DataFrame slip through analyze()'s guard and
            # land here, where ema_trend was set equal to close and
            # volume_sma equal to volume — which forces volume_ratio == 1.0
            # and current_price > ema_trend_val == False on every single bar.
            # Two of the required entry conditions then became permanently
            # unsatisfiable, so BUY could never fire no matter the market.
            # analyze() now checks len(df) against min_required_bars() up
            # front and returns a clear HOLD instead of reaching this branch
            # in normal operation. This fallback remains only as a defensive
            # no-op for callers that invoke calculate_indicators() directly.
            df['rsi'] = 50.0
            df['ema_20'] = df['close']
            df['ema_trend'] = df['close']
            df['volume_sma'] = df['volume']
            df['atr'] = df['close'] * 0.01
            return df

        # Fast EMA (entry trigger reference)
        df['ema_20'] = df['close'].ewm(span=self.ema_period, adjust=False).mean()

        # NEW: Slower EMA used purely as a macro trend filter — only take longs
        # when price is above it AND it is itself sloping up. This is the single
        # biggest change: it keeps the strategy out of chop/downtrends, which is
        # where volume-breakout-long systems bleed the most.
        df['ema_trend'] = df['close'].ewm(span=self.trend_ema_period, adjust=False).mean()

        # Volume SMA — use ffill only (no bfill) to avoid leaking future data
        # into early bars during backtests.
        df['volume_sma'] = df['volume'].rolling(window=self.volume_period).mean().ffill()

        # Wilder's RSI
        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(com=self.rsi_period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=self.rsi_period - 1, adjust=False).mean()

        rs = np.where(avg_loss == 0, 0, avg_gain / avg_loss)
        df['rsi'] = 100 - (100 / (1 + rs))
        df['rsi'] = df['rsi'].fillna(50)

        # Wilder's ATR
        high_low = df['high'] - df['low']
        high_close_prev = (df['high'] - df['close'].shift(1)).abs()
        low_close_prev = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
        df['atr'] = tr.ewm(com=self.atr_period - 1, adjust=False).mean().ffill()

        return df

    def analyze(
        self,
        df,
        open_position=False,
        entry_price=None,
        peak_price=None,
        stop_loss_pct=1.5,
        take_profit_pct=3.0,
        fee_rate=0.00075,
    ):
        """
        Analyzes the latest bar and decides on BUY, SELL, or HOLD.
        Incorporates round-trip fees into the exit targets.
        """
        required_bars = self.min_required_bars()
        if len(df) < required_bars:
            return {
                "signal": "HOLD",
                "reason": f"Insufficient candles: have {len(df)}, need {required_bars} for indicators to be valid",
            }

        df = self.calculate_indicators(df)

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        current_price = latest['close']
        current_rsi = latest['rsi']
        current_vol = latest['volume']
        avg_vol = latest['volume_sma']
        ema_val = latest['ema_20']
        ema_trend_val = latest['ema_trend']
        atr_val = latest.get('atr', current_price * 0.01)

        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        round_trip_fee_pct = 2 * fee_rate * 100

        # Dynamic Exit targets based on ATR, with a wider, more crypto-realistic
        # SL floor so normal noise doesn't repeatedly stop you out.
        if atr_val > 0 and current_price > 0:
            dynamic_sl = (atr_val / current_price) * 1.5 * 100
            dynamic_tp = (atr_val / current_price) * 3.0 * 100

            stop_loss_pct = max(self.min_stop_loss_pct, min(self.max_stop_loss_pct, dynamic_sl))
            take_profit_pct = max(self.min_take_profit_pct, min(self.max_take_profit_pct, dynamic_tp))

        # Case 1: Already holding a position - Check Exit Conditions
        if open_position and entry_price is not None:
            reference_price = peak_price if peak_price is not None else entry_price

            price_change_from_peak = ((current_price - reference_price) / reference_price) * 100
            price_change_from_entry = ((current_price - entry_price) / entry_price) * 100

            required_tp_gain = take_profit_pct + round_trip_fee_pct

            if price_change_from_peak <= -stop_loss_pct:
                return {
                    "signal": "SELL",
                    "reason": f"Trailing Stop Loss hit: dropped {price_change_from_peak:.2f}% from peak ${reference_price:.2f} (Limit: -{stop_loss_pct:.2f}%)",
                    "rsi": current_rsi,
                    "volume_ratio": volume_ratio,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                }

            if price_change_from_entry >= required_tp_gain:
                return {
                    "signal": "SELL",
                    "reason": f"Take Profit hit: up {price_change_from_entry:.2f}% from entry ${entry_price:.2f} (Required Net: +{take_profit_pct:.2f}%, incl. fees: {required_tp_gain:.2f}%)",
                    "rsi": current_rsi,
                    "volume_ratio": volume_ratio,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                }

            if current_rsi > 75 and current_rsi < prev['rsi']:
                if price_change_from_entry > round_trip_fee_pct:
                    return {
                        "signal": "SELL",
                        "reason": f"Technical Exit: RSI overbought and reversing down ({current_rsi:.1f}). Locked net profit.",
                        "rsi": current_rsi,
                        "volume_ratio": volume_ratio,
                        "stop_loss_pct": stop_loss_pct,
                        "take_profit_pct": take_profit_pct,
                    }

            if current_price < ema_val and prev['close'] >= prev['ema_20']:
                return {
                    "signal": "SELL",
                    "reason": "Technical Exit: Price crossed below 20-period EMA (Trend reversal)",
                    "rsi": current_rsi,
                    "volume_ratio": volume_ratio,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                }

            # NEW: macro trend break — if price falls back below the trend EMA
            # after being in profit, the "breakout" thesis is invalidated even
            # if the tighter stop hasn't been hit yet.
            if current_price < ema_trend_val and price_change_from_entry > round_trip_fee_pct:
                return {
                    "signal": "SELL",
                    "reason": f"Technical Exit: Price broke below {self.trend_ema_period}-EMA trend filter with profit locked",
                    "rsi": current_rsi,
                    "volume_ratio": volume_ratio,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                }

            return {
                "signal": "HOLD",
                "reason": f"Holding position. PnL: {price_change_from_entry:.2f}% (Peak: ${reference_price:.2f})",
                "rsi": current_rsi,
                "volume_ratio": volume_ratio,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
            }

        # Case 2: No active position - Check Entry Conditions
        else:
            volume_breakout = volume_ratio >= 1.5

            bullish_trend = current_price > ema_val and current_price > latest['open']

            rsi_bullish = 50.0 <= current_rsi <= 70.0

            price_increasing = current_price > prev['close']

            # NEW: Macro trend filter — only take longs when price is above the
            # slower trend EMA and that EMA is itself rising. This is what keeps
            # the strategy out of chop/downtrend fakeout breakouts.
            macro_uptrend = current_price > ema_trend_val and ema_trend_val > df['ema_trend'].iloc[-3]

            # NEW: Candle-quality filter — require the close to sit in the upper
            # part of the bar's range. A volume spike that closes near the low
            # of its range is a rejection/wick, not a genuine breakout, even if
            # close > open technically holds.
            bar_range = latest['high'] - latest['low']
            close_position_in_range = (
                (latest['close'] - latest['low']) / bar_range if bar_range > 0 else 1.0
            )
            strong_close = close_position_in_range >= 0.6

            expected_gain_pct = take_profit_pct
            is_volatility_sufficient = expected_gain_pct >= (round_trip_fee_pct * 3)

            entry_conditions = (
                volume_breakout
                and bullish_trend
                and rsi_bullish
                and price_increasing
                and is_volatility_sufficient
                and macro_uptrend
                and strong_close
            )

            if entry_conditions:
                return {
                    "signal": "BUY",
                    "reason": f"Volume Breakout ({volume_ratio:.1f}x) & Bullish Momentum (RSI: {current_rsi:.1f}), confirmed uptrend & strong close",
                    "rsi": current_rsi,
                    "volume_ratio": volume_ratio,
                    "stop_loss_pct": stop_loss_pct,
                    "take_profit_pct": take_profit_pct,
                }

            if not is_volatility_sufficient:
                reason = "Skipping entry: volatility too low relative to fees (fee drag guard)"
            elif not macro_uptrend:
                reason = "Skipping entry: not in a confirmed macro uptrend (trend filter)"
            elif not strong_close:
                reason = "Skipping entry: candle closed weak in its range (possible rejection wick)"
            else:
                reason = "No entry criteria met"

            return {
                "signal": "HOLD",
                "reason": reason,
                "rsi": current_rsi,
                "volume_ratio": volume_ratio,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
            }
