import os
import json
import tempfile
from datetime import datetime
import pytz

import yfinance as yf
import pandas as pd
import numpy as np
import gspread
from google.oauth2.service_account import Credentials

# ======================================================
# CONFIG
# ======================================================
SPREADSHEET_ID = "1tDRsk2B9udGRhirzwxrryc3HvtHhg6rwN8UTjeJlwAc"
INPUT_SHEET = "StockList"
OUTPUT_SHEET = "HA_ST_Daily_Status"

ATR_PERIOD = 10
ATR_MULTIPLIER = 3
SMA_LONG = 200
SMA_SHORT = 20
DATA_PERIOD = "3y"

# ======================================================
# GOOGLE AUTH
# ======================================================
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_CREDENTIALS"])

tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
with open(tmp.name, "w") as f:
    json.dump(SERVICE_ACCOUNT_INFO, f)

creds = Credentials.from_service_account_file(
    tmp.name,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID)

# ======================================================
# METADATA
# ======================================================
ist = pytz.timezone("Asia/Kolkata")
run_time_ist = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")
run_source = "GitHub Actions" if os.getenv("GITHUB_ACTIONS") == "true" else "Manual Run"

# ======================================================
# READ SYMBOL LIST
# ======================================================
symbols = sheet.worksheet(INPUT_SHEET).col_values(1)[1:]
symbols = [s.strip() for s in symbols if s.strip()]
print(f"Loaded {len(symbols)} symbols")

rows = []

# ======================================================
# MAIN LOOP
# ======================================================
for symbol in symbols:
    try:
        base = (
            symbol.replace("NSE:", "")
                  .replace("BSE:", "")
                  .replace(".NS", "")
                  .replace(".BO", "")
        )

        used_exchange = ""
        df = yf.download(f"{base}.NS", period=DATA_PERIOD, auto_adjust=False, progress=False)
        if not df.empty:
            used_exchange = "NSE"
        else:
            df = yf.download(f"{base}.BO", period=DATA_PERIOD, auto_adjust=False, progress=False)
            if df.empty:
                continue
            used_exchange = "BSE"

        google_symbol = f"{used_exchange}:{base}"

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["Open", "High", "Low", "Close"]].dropna()

        # -----------------------------
        # DATA AVAILABILITY FLAGS
        # -----------------------------
        has_sma200 = len(df) >= SMA_LONG
        has_sma20 = len(df) >= SMA_SHORT
        has_st = len(df) >= ATR_PERIOD + 30

        # =========================
        # SMA and EMA
        # =========================
        df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
        df["SMA20"] = df["Close"].rolling(SMA_SHORT).mean()

        above_200 = (
            "Yes" if has_sma200 and df["Close"].iloc[-1] > df["EMA200"].iloc[-1]
            else "No" if has_sma200
            else "NA"
        )

        above_20 = (
            "Yes" if has_sma20 and df["Close"].iloc[-1] > df["SMA20"].iloc[-1]
            else "No" if has_sma20
            else "NA"
        )

        # =========================
        # 200 EMA CROSS TODAY
        # =========================
        if has_sma200 and len(df) > 1:
            prev_close = df["Close"].iloc[-2]
            prev_ema = df["EMA200"].iloc[-2]
            curr_close = df["Close"].iloc[-1]
            curr_ema = df["EMA200"].iloc[-1]

            if prev_close <= prev_ema and curr_close > curr_ema:
                sma_cross_today = "Bullish Cross"
            elif prev_close >= prev_ema and curr_close < curr_ema:
                sma_cross_today = "Bearish Cross"
            else:
                sma_cross_today = "No"
        else:
            sma_cross_today = "NA"

        # =========================
        # 20 SMA CROSS TODAY
        # =========================
        if has_sma20 and len(df) > 1:
            prev_close = df["Close"].iloc[-2]
            prev_sma20 = df["SMA20"].iloc[-2]
            curr_close = df["Close"].iloc[-1]
            curr_sma20 = df["SMA20"].iloc[-1]

            if prev_close <= prev_sma20 and curr_close > curr_sma20:
                sma20_cross_today = "Bullish Cross"
            elif prev_close >= prev_sma20 and curr_close < curr_sma20:
                sma20_cross_today = "Bearish Cross"
            else:
                sma20_cross_today = "No"
        else:
            sma20_cross_today = "NA"

        # =========================
        # HEIKEN ASHI
        # =========================
        df["HA_Close"] = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4

        ha_open = [(df["Open"].iloc[0] + df["Close"].iloc[0]) / 2]
        for i in range(1, len(df)):
            ha_open.append((ha_open[i - 1] + df["HA_Close"].iloc[i - 1]) / 2)

        df["HA_Open"] = ha_open
        df["HA_High"] = df[["High", "HA_Open", "HA_Close"]].max(axis=1)
        df["HA_Low"] = df[["Low", "HA_Open", "HA_Close"]].min(axis=1)

        df["HA_Color"] = np.where(df["HA_Close"] > df["HA_Open"], "Green", "Red")
        df["HA_Flip"] = df["HA_Color"] != df["HA_Color"].shift(1)

        # =========================
        # RSI (HEIKEN ASHI)
        # =========================
        RSI_PERIOD = 14

        delta = df["HA_Close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(RSI_PERIOD).mean()
        avg_loss = loss.rolling(RSI_PERIOD).mean()

        rs = avg_gain / avg_loss
        df["RSI_HA"] = 100 - (100 / (1 + rs))

        # =========================
        # SUPERTREND
        # =========================
        if has_st:
            df["TR"] = pd.concat([
                df["HA_High"] - df["HA_Low"],
                (df["HA_High"] - df["HA_Close"].shift()).abs(),
                (df["HA_Low"] - df["HA_Close"].shift()).abs()
            ], axis=1).max(axis=1)

            df["ATR"] = df["TR"].rolling(ATR_PERIOD).mean()

            hl2 = (df["HA_High"] + df["HA_Low"]) / 2
            df["UpperBand"] = hl2 + ATR_MULTIPLIER * df["ATR"]
            df["LowerBand"] = hl2 - ATR_MULTIPLIER * df["ATR"]

            df["ST_Direction"] = 0

            for i in range(ATR_PERIOD, len(df)):
                if i == ATR_PERIOD:
                    df.at[df.index[i], "ST_Direction"] = (
                        1 if df["HA_Close"].iloc[i] >= df["LowerBand"].iloc[i] else -1
                    )
                    continue

                prev = df["ST_Direction"].iloc[i - 1]

                if df["HA_Close"].iloc[i] > df["UpperBand"].iloc[i - 1]:
                    df.at[df.index[i], "ST_Direction"] = 1
                elif df["HA_Close"].iloc[i] < df["LowerBand"].iloc[i - 1]:
                    df.at[df.index[i], "ST_Direction"] = -1
                else:
                    df.at[df.index[i], "ST_Direction"] = prev
                    if prev == 1:
                        df.at[df.index[i], "LowerBand"] = max(
                            df["LowerBand"].iloc[i],
                            df["LowerBand"].iloc[i - 1]
                        )
                    else:
                        df.at[df.index[i], "UpperBand"] = min(
                            df["UpperBand"].iloc[i],
                            df["UpperBand"].iloc[i - 1]
                        )

            df["ST_Flip"] = (
                ((df["ST_Direction"] == 1) & (df["ST_Direction"].shift(1) == -1)) |
                ((df["ST_Direction"] == -1) & (df["ST_Direction"].shift(1) == 1))
            )

            flip_idx = df.index[df["ST_Flip"]].tolist()
            if flip_idx:
                last_flip_date = str(flip_idx[-1].date())
                days_since = len(df) - 1 - df.index.get_loc(flip_idx[-1])
            else:
                last_flip_date = ""
                days_since = ""

            st_color = "Green" if df["ST_Direction"].iloc[-1] == 1 else "Red"
            st_flip_today = bool(df["ST_Flip"].iloc[-1])

        else:
            st_color = "NA"
            st_flip_today = "NA"
            days_since = "NA"
            last_flip_date = "NA"

        # =========================
        # 21-DAY HIGH (Column 16 and Column 32)
        # =========================
        # State-based: Today's CLOSE > prior 21 days highest HIGH
        # Consistent with 52W High and all other breakout signals
        if len(df) >= 22:
            high_21 = df["High"].iloc[-22:-1].max()     # Prior 21 days highest HIGH
            at_21d_high = "Yes" if df["Close"].iloc[-1] > high_21 else "No"
        else:
            at_21d_high = "NA"

        # =========================
        # TREND STRUCTURE (HH/HL or LH/LL) — 10-bar HA
        # =========================
        SWING_PERIOD = 10
        if len(df) >= SWING_PERIOD * 2:
            # Split into two equal windows: prior and recent
            prior = df.iloc[-(SWING_PERIOD * 2):-SWING_PERIOD]
            recent = df.iloc[-SWING_PERIOD:]

            prior_high = prior["HA_High"].max()
            prior_low = prior["HA_Low"].min()
            recent_high = recent["HA_High"].max()
            recent_low = recent["HA_Low"].min()

            hh = recent_high > prior_high
            hl = recent_low > prior_low
            lh = recent_high < prior_high
            ll = recent_low < prior_low

            if hh and hl:
                trend_structure = "Uptrend"
            elif lh and ll:
                trend_structure = "Downtrend"
            else:
                trend_structure = "Neutral"
        else:
            trend_structure = "NA"

        # =========================
        # HA TREND REVERSAL SIGNAL
        # =========================
        # Pure HA swing high = candle with no upper wick (HA_High == HA_Close)
        # Pure HA swing low  = candle with no lower wick (HA_Low == HA_Open)
        # Signal fires only on the day today breaches the last pure swing high/low
        ha_reversal = "None"
        if len(df) >= 5:
            # Find last pure swing high (no upper wick) excluding today
            last_swing_high = None
            for i in range(len(df) - 2, 0, -1):
                if round(df["HA_High"].iloc[i], 4) == round(df["HA_Close"].iloc[i], 4):
                    last_swing_high = df["HA_High"].iloc[i]
                    break

            # Find last pure swing low (no lower wick) excluding today
            last_swing_low = None
            for i in range(len(df) - 2, 0, -1):
                if round(df["HA_Low"].iloc[i], 4) == round(df["HA_Open"].iloc[i], 4):
                    last_swing_low = df["HA_Low"].iloc[i]
                    break

            today_high = df["HA_High"].iloc[-1]
            today_low  = df["HA_Low"].iloc[-1]
            prev_high  = df["HA_High"].iloc[-2]
            prev_low   = df["HA_Low"].iloc[-2]

            if last_swing_high is not None:
                if prev_high <= last_swing_high and today_high > last_swing_high:
                    ha_reversal = "Down to Up"

            if last_swing_low is not None and ha_reversal == "None":
                if prev_low >= last_swing_low and today_low < last_swing_low:
                    ha_reversal = "Up to Down"

        # =========================
        # STOP LOSS SIGNAL (independent of profit booking)
        # =========================
        # Yes if: ST flipped Red in last 5 days AND HA Red today AND Close < 20 SMA
        stop_loss_signal = "No"

        st_recent_red_flip = False
        if has_st and len(df) > 5:
            recent_direction = df["ST_Direction"].iloc[-5:].tolist()
            if -1 in recent_direction and recent_direction[-1] == -1:
                for i in range(len(df) - 5, len(df)):
                    if i > 0 and df["ST_Direction"].iloc[i] == -1 and df["ST_Direction"].iloc[i - 1] == 1:
                        st_recent_red_flip = True
                        break

        close_below_sma20 = has_sma20 and df["Close"].iloc[-1] < df["SMA20"].iloc[-1]
        ha_red_today = df["HA_Color"].iloc[-1] == "Red"

        if st_recent_red_flip and ha_red_today and close_below_sma20:
            stop_loss_signal = "Yes"

        # =========================
        # PROFIT BOOKING SIGNAL (independent of stop loss)
        # =========================
        # Yes if: RSI(HA) > 75 AND HA flips from Green to Red today (euphoria breaking)
        #   OR (ST Green > 10 days AND (Close breaks below 20 SMA today OR HA flips Red after 3+ greens))
        profit_booking_signal = "No"

        days_st_green = 0
        if has_st:
            for i in range(len(df) - 1, -1, -1):
                if df["ST_Direction"].iloc[i] == 1:
                    days_st_green += 1
                else:
                    break

        close_broke_sma20 = (
            has_sma20 and len(df) > 1 and
            df["Close"].iloc[-2] >= df["SMA20"].iloc[-2] and
            df["Close"].iloc[-1] < df["SMA20"].iloc[-1]
        )

        ha_flip_red_after_green = False
        if len(df) >= 4 and df["HA_Color"].iloc[-1] == "Red":
            if (df["HA_Color"].iloc[-2] == "Green" and
                df["HA_Color"].iloc[-3] == "Green" and
                df["HA_Color"].iloc[-4] == "Green"):
                ha_flip_red_after_green = True

        rsi_now = df["RSI_HA"].iloc[-1]
        rsi_euphoric = not pd.isna(rsi_now) and rsi_now > 75

        # HA flips from Green (yesterday) to Red (today)
        ha_flip_green_to_red_today = (
            len(df) > 1 and
            df["HA_Color"].iloc[-1] == "Red" and
            df["HA_Color"].iloc[-2] == "Green"
        )

        mature_trend = days_st_green > 10

        if rsi_euphoric and ha_flip_green_to_red_today:
            profit_booking_signal = "Yes"
        elif mature_trend and (close_broke_sma20 or ha_flip_red_after_green):
            profit_booking_signal = "Yes"


        # =========================
        # COMPOSITE SIGNAL
        # =========================
        # Pullback Buy conditions: ST Green + HA flip to Green today + RSI(HA) < 40
        # Breakout Buy conditions: ST Green + 21-day Bullish Cross
        # Above 200 SMA upgrades to Strong variant
        composite_signal = "None"

        rsi_val = df["RSI_HA"].iloc[-1]
        ha_flip_green_today = (
            df["HA_Color"].iloc[-1] == "Green" and
            df["HA_Color"].iloc[-2] == "Red"
        )

        if st_color == "Green" and ha_flip_green_today and not pd.isna(rsi_val) and rsi_val < 40:
            if above_200 == "Yes":
                composite_signal = "Strong Pullback Buy"
            else:
                composite_signal = "Pullback Buy"

        elif st_color == "Green" and sma_cross_today == "Bullish Cross":
            if above_200 == "Yes":
                composite_signal = "Strong Breakout Buy"
            else:
                composite_signal = "Breakout Buy"

        # =========================
        # SHORT CALL CANDIDATE (0.1 DELTA NAKED)
        # =========================
        # Strong: ST Red + Close < 21 EMA + HA Red today + RSI 30-60 + Close < 200 SMA
        # Weak:   ST Red + Close < 21 EMA + HA Red today + RSI 30-60 + Close > 200 SMA
        short_call_signal = "None"

        # 21 EMA computed here (not stored in df to avoid touching other logic)
        if len(df) >= 21:
            ema21 = df["Close"].ewm(span=21, adjust=False).mean()
            close_below_ema21 = df["Close"].iloc[-1] < ema21.iloc[-1]
        else:
            close_below_ema21 = False

        rsi_sc = df["RSI_HA"].iloc[-1]
        rsi_in_range = not pd.isna(rsi_sc) and 30 <= rsi_sc <= 60

        if (st_color == "Red" and
            close_below_ema21 and
            df["HA_Color"].iloc[-1] == "Red" and
            rsi_in_range):
            if above_200 == "No":
                short_call_signal = "Strong Short Call"
            else:
                short_call_signal = "Short Call"

        # =========================
        # 7-DAY HIGH CROSSOVER
        # =========================
        if len(df) >= 8:
            high_7 = df["High"].iloc[-8:-1].max()
            prev_close_7 = df["Close"].iloc[-2]
            curr_close_7 = df["Close"].iloc[-1]
            if prev_close_7 <= high_7 and curr_close_7 > high_7:
                at_7d_high = "Bullish Cross"
            elif prev_close_7 >= high_7 and curr_close_7 < high_7:
                at_7d_high = "Bearish Cross"
            else:
                at_7d_high = "No"
        else:
            at_7d_high = "NA"

        # =========================
        # 7-DAY LOW (for GTT stop placement)
        # =========================
        # Prior 7 days only (excludes today) - proven support reference
        if len(df) >= 8:
            low_7day = round(df["Low"].iloc[-8:-1].min(), 2)
        else:
            low_7day = "NA"

        # =========================
        # 21 EMA POSITION (current state: Above / Below)
        # =========================
        if len(df) >= 21:
            ema21_pos_series = df["Close"].ewm(span=21, adjust=False).mean()
            ema21_pos = "Above" if df["Close"].iloc[-1] > ema21_pos_series.iloc[-1] else "Below"
        else:
            ema21_pos = "NA"

        # =========================
        # PREMIUM SELLING SIGNAL (based on 21 EMA position)
        # =========================
        # Below 21 EMA -> Short Call (sell 0.1 delta naked call)
        # Above 21 EMA -> Bull Put Spread (sell monthly bull put spread)
        if ema21_pos == "Below":
            premium_signal = "Short Call"
        elif ema21_pos == "Above":
            premium_signal = "Bull Put Spread"
        else:
            premium_signal = "NA"

        # =========================
        # DONCHIAN STRATEGY (21-day high entry, 21 EMA exit, pyramid trigger)
        # =========================
        # State-based breakout: Today's CLOSE > prior 21 days highest HIGH
        # Donchian Entry  : Today's close > prior 21-day high AND above 200 EMA
        # Donchian Exit   : Close < 21 EMA (tight exit on trend deterioration)
        # Donchian Pyramid: Same as entry (continuous signal)
        donchian_entry = "No"
        donchian_exit = "No"
        donchian_pyramid = "No"

        if len(df) >= 22 and has_sma200:
            high_21d = df["High"].iloc[-22:-1].max()    # Prior 21 days highest HIGH
            curr_close_d = df["Close"].iloc[-1]         # Today's close
            ema200_current = df["EMA200"].iloc[-1]

            # Entry: Today's close above prior 21-day high AND above 200 EMA
            if curr_close_d > high_21d and curr_close_d > ema200_current:
                donchian_entry = "Yes"

            # Pyramid: Same as entry (continuous signal)
            if curr_close_d > high_21d:
                donchian_pyramid = "Yes"

        if len(df) >= 21:
            ema21_series = df["Close"].ewm(span=21, adjust=False).mean()
            prev_close_e = df["Close"].iloc[-2]
            curr_close_e = df["Close"].iloc[-1]
            prev_ema = ema21_series.iloc[-2]
            curr_ema = ema21_series.iloc[-1]

            # Exit: today closes below 21 EMA (fresh breach)
            if prev_close_e >= prev_ema and curr_close_e < curr_ema:
                donchian_exit = "Yes"

        # =========================
        # 52-WEEK HIGH ENTRY
        # =========================
        # State-based breakout: Today's CLOSE > prior 252 days highest HIGH
        # Signal remains "Yes" as long as close stays above prior high
        week52_entry = "No"
        
        if len(df) >= 253:
            high_52w = df["High"].iloc[-253:-1].max()  # Prior 252 days highest HIGH
            curr_close_52 = df["Close"].iloc[-1]        # Today's close
            
            # Breakout: Today's close above prior 52-week high
            if curr_close_52 > high_52w:
                week52_entry = "Yes"
        else:
            week52_entry = "NA"

        # =========================
        # 21 EMA CROSS TODAY
        # =========================
        # Bullish Cross: yesterday close <= 21 EMA, today close > 21 EMA
        # Bearish Cross: yesterday close >= 21 EMA, today close < 21 EMA
        if len(df) >= 21 and len(df) > 1:
            ema21_cross_series = df["Close"].ewm(span=21, adjust=False).mean()
            prev_close_ec = df["Close"].iloc[-2]
            curr_close_ec = df["Close"].iloc[-1]
            prev_ema_ec = ema21_cross_series.iloc[-2]
            curr_ema_ec = ema21_cross_series.iloc[-1]

            if prev_close_ec <= prev_ema_ec and curr_close_ec > curr_ema_ec:
                ema21_cross = "Bullish Cross"
            elif prev_close_ec >= prev_ema_ec and curr_close_ec < curr_ema_ec:
                ema21_cross = "Bearish Cross"
            else:
                ema21_cross = "No"
        else:
            ema21_cross = "NA"

        # =========================
        # VOLUME EXPANSION (Column 34)
        # =========================
        # Last 5 days avg volume / Prior 20 days avg volume
        # Shows accumulation trend (>1.3 = building, 0.7-0.9 = drying up/VCP)
        volume_expansion = "NA"
        if len(df) >= 25:
            try:
                df_vol = yf.download(
                    f"{base}.NS" if used_exchange == "NSE" else f"{base}.BO",
                    period=DATA_PERIOD,
                    auto_adjust=False,
                    progress=False
                )
                if not df_vol.empty:
                    if isinstance(df_vol.columns, pd.MultiIndex):
                        df_vol.columns = df_vol.columns.get_level_values(0)
                    
                    if "Volume" in df_vol.columns and len(df_vol) >= 25:
                        # Recent 5-day average volume
                        recent_5d_avg = df_vol["Volume"].iloc[-5:].mean()
                        
                        # Prior 20-day average volume (days -25 to -6, excludes recent 5)
                        prior_20d_avg = df_vol["Volume"].iloc[-25:-5].mean()
                        
                        if prior_20d_avg > 0:
                            volume_expansion = round(recent_5d_avg / prior_20d_avg, 2)
            except:
                volume_expansion = "NA"

        # =========================
        # 21-DAY LOW (Column 35)
        # =========================
        # Prior 21 days only (excludes today) - proven support reference
        if len(df) >= 22:
            low_21day = round(df["Low"].iloc[-22:-1].min(), 2)
        else:
            low_21day = "NA"

        # =========================
        # AT 21-DAY LOW (Column 36)
        # =========================
        # Check if today's close is at or near the PRIOR 21-day low
        at_21d_low = "No"
        if len(df) >= 22:
            low_21d_check = df["Low"].iloc[-22:-1].min()  # Prior 21 days, excludes today
            current_close_check = df["Close"].iloc[-1]
            # Consider "at 21-day low" if within 1% above it
            if current_close_check <= low_21d_check * 1.01:
                at_21d_low = "Yes"

        # =========================
        # TODAY'S LOW (Column 36)
        # =========================
        # Today's actual low (for gap-down comparison)
        today_low = round(df["Low"].iloc[-1], 2) if len(df) > 0 else "NA"

        # =========================
        # ALL-TIME HIGH (Column 38)
        # =========================
        # State-based breakout: Today's CLOSE > all prior HIGHS excluding today
        all_time_high = "No"
        if len(df) > 1:
            ath_high = df["High"].iloc[:-1].max()  # All prior highs excluding today
            current_close = df["Close"].iloc[-1]    # Today's close
            
            # Breakout: Today's close above all-time high
            if current_close > ath_high:
                all_time_high = "Yes"

        # =========================
        # RENKO BRICK COLOR (Column 38)
        # =========================
        # Daily timeframe, 1% fixed brick size
        renko_color = "NA"
        if len(df) >= 10:
            try:
                # 1% fixed brick size
                brick_size = 0.01  # 1%
                
                # Initialize first brick with first close
                brick_price = df['Close'].iloc[0]
                brick_direction = 1  # 1 = green (up), -1 = red (down)
                
                # Process each daily bar
                for i in range(1, len(df)):
                    close = df['Close'].iloc[i]
                    high = df['High'].iloc[i]
                    low = df['Low'].iloc[i]
                    
                    # Check for brick reversal or continuation
                    # For green bricks: check if we can add more green or reverse to red
                    if brick_direction == 1:
                        # Try to add green bricks
                        while close >= brick_price * (1 + brick_size):
                            brick_price = brick_price * (1 + brick_size)
                        
                        # Check for reversal (need to break down by 2 bricks)
                        if low <= brick_price * (1 - 2 * brick_size):
                            brick_direction = -1
                            brick_price = brick_price * (1 - brick_size)
                            # Continue adding red bricks if price continues down
                            while low <= brick_price * (1 - brick_size):
                                brick_price = brick_price * (1 - brick_size)
                    
                    # For red bricks: check if we can add more red or reverse to green
                    else:
                        # Try to add red bricks
                        while close <= brick_price * (1 - brick_size):
                            brick_price = brick_price * (1 - brick_size)
                        
                        # Check for reversal (need to break up by 2 bricks)
                        if high >= brick_price * (1 + 2 * brick_size):
                            brick_direction = 1
                            brick_price = brick_price * (1 + brick_size)
                            # Continue adding green bricks if price continues up
                            while high >= brick_price * (1 + brick_size):
                                brick_price = brick_price * (1 + brick_size)
                
                # Final brick color
                renko_color = "Green" if brick_direction == 1 else "Red"
            except:
                renko_color = "NA"

        # =========================
        # ADR - AVERAGE DAILY RANGE (Column 39)
        # =========================
        # 14-day average of (High - Low) as percentage of Close
        adr_pct = "NA"
        if len(df) >= 14:
            try:
                # Calculate daily range as percentage
                df_adr = df.copy()
                df_adr['Daily_Range_Pct'] = ((df_adr['High'] - df_adr['Low']) / df_adr['Close']) * 100
                
                # 14-day average
                adr_14d = df_adr['Daily_Range_Pct'].iloc[-14:].mean()
                adr_pct = round(adr_14d, 2)
            except:
                adr_pct = "NA"

        # =========================
        # 10-DAY EMA LOW SIGNAL (Column 40)
        # =========================
        # Check if today's low is at or below the 10-day EMA of lows
        ema10_low_signal = "No"
        if len(df) >= 10:
            try:
                # Calculate 10-day EMA of Low prices
                ema10_low = df['Low'].ewm(span=10, adjust=False).mean()
                today_low_check = df['Low'].iloc[-1]
                ema10_low_value = ema10_low.iloc[-1]
                
                # Signal if today's low is at/below 10-day EMA of lows
                if today_low_check <= ema10_low_value:
                    ema10_low_signal = "Yes"
            except:
                ema10_low_signal = "NA"

        # =========================
        # GAP UP DETECTION (Column 41)
        # =========================
        # Gap Up: Today's Open > Yesterday's High
        # Shows gap size as percentage
        gap_up = "No"
        if len(df) > 1:
            try:
                yesterday_high = df['High'].iloc[-2]
                today_open = df['Open'].iloc[-1]
                
                if today_open > yesterday_high:
                    gap_pct = ((today_open - yesterday_high) / yesterday_high) * 100
                    gap_up = f"{round(gap_pct, 2)}%"
            except:
                gap_up = "No"

        # =========================
        # PULLBACK NEAR 200 EMA (Column 42)
        # =========================
        # Detects when price is pulling back to test 200 EMA support
        # "Yes" if: Above 200 EMA AND within 3% of touching it
        pullback_200ema = "No"
        if has_sma200:
            try:
                current_close = df['Close'].iloc[-1]
                ema200_value = df['EMA200'].iloc[-1]
                
                # Check if above 200 EMA
                if current_close > ema200_value:
                    # Calculate distance from 200 EMA
                    distance_pct = ((current_close - ema200_value) / ema200_value) * 100
                    
                    # Signal if within 3% above 200 EMA (pullback zone)
                    if distance_pct <= 3.0:
                        pullback_200ema = "Yes"
            except:
                pullback_200ema = "No"

        # =========================
        # RECENT SWING LOW (Column 43)
        # =========================
        # Find most recent swing low that acts as immediate support
        # Swing low = 5-bar pivot (lower than 2 bars on each side) below current price
        recent_swing_low = "NA"
        if len(df) >= 10:
            try:
                current_price = df['Close'].iloc[-1]
                swing_lows = []
                
                # Look back max 20 bars (excluding last 2 bars - can't confirm pivot yet)
                lookback = min(20, len(df) - 5)
                
                # Find ALL swing lows in lookback period
                for i in range(len(df) - 3, len(df) - lookback - 1, -1):
                    if i >= 2 and i <= len(df) - 3:
                        current_low = df['Low'].iloc[i]
                        
                        # Check 2 bars before
                        left_1 = df['Low'].iloc[i-1]
                        left_2 = df['Low'].iloc[i-2]
                        
                        # Check 2 bars after
                        right_1 = df['Low'].iloc[i+1]
                        right_2 = df['Low'].iloc[i+2]
                        
                        # Valid swing low AND below current price
                        if (current_low < left_1 and current_low < left_2 and 
                            current_low < right_1 and current_low < right_2 and
                            current_low < current_price):
                            swing_lows.append(current_low)
                
                # Return the HIGHEST swing low (closest support below current price)
                if swing_lows:
                    recent_swing_low = round(max(swing_lows), 2)
            except:
                recent_swing_low = "NA"

        # =========================
        # EMA CONVERGENCE (Column 44)
        # =========================
        # Detects when 3 of 4 EMAs (10, 21, 50, 200) are within 1-3% of each other
        # Indicates consolidation/squeeze before potential breakout
        # Output: Shows convergence % if ≤3%, otherwise "No"
        ema_convergence = "No"
        if len(df) >= 200:
            try:
                ema_10 = df["Close"].ewm(span=10, adjust=False).mean().iloc[-1]
                ema_21 = df["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
                ema_50 = df["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
                ema_200 = df["EMA200"].iloc[-1]
                
                # Calculate range (highest - lowest)
                ema_max = max(ema_10, ema_21, ema_50, ema_200)
                ema_min = min(ema_10, ema_21, ema_50, ema_200)
                
                # Convergence % = (range / min) * 100
                if ema_min > 0:
                    convergence_pct = ((ema_max - ema_min) / ema_min) * 100
                    
                    # If all 4 EMAs within 3%, show the %
                    if convergence_pct <= 3.0:
                        ema_convergence = f"{round(convergence_pct, 2)}%"
            except:
                ema_convergence = "NA"

        # =========================
        # 10 EMA × 20 EMA CROSSOVER (Column 45)
        # =========================
        # Detects bullish/bearish crossover between 10 EMA and 20 EMA
        # Bullish: Yesterday 10 EMA ≤ 20 EMA, Today 10 EMA > 20 EMA
        # Bearish: Yesterday 10 EMA ≥ 20 EMA, Today 10 EMA < 20 EMA
        ema_cross_10_20 = "No"
        if len(df) >= 21:  # Need 20 periods for 20 EMA
            try:
                ema_10_series = df["Close"].ewm(span=10, adjust=False).mean()
                ema_20_series = df["Close"].ewm(span=20, adjust=False).mean()
                
                ema_10_today = ema_10_series.iloc[-1]
                ema_20_today = ema_20_series.iloc[-1]
                ema_10_yesterday = ema_10_series.iloc[-2]
                ema_20_yesterday = ema_20_series.iloc[-2]
                
                # Bullish crossover: 10 EMA crosses ABOVE 20 EMA
                if ema_10_yesterday <= ema_20_yesterday and ema_10_today > ema_20_today:
                    ema_cross_10_20 = "Bullish Cross"
                
                # Bearish crossover: 10 EMA crosses BELOW 20 EMA
                elif ema_10_yesterday >= ema_20_yesterday and ema_10_today < ema_20_today:
                    ema_cross_10_20 = "Bearish Cross"
            except:
                ema_cross_10_20 = "NA"

        # =========================
        # 200 EMA CROSS FROM BELOW (Column 46)
        # =========================
        # Detects when price crosses ABOVE 200 EMA (bullish breakout)
        # Yesterday Close < 200 EMA, Today Close > 200 EMA
        cross_200_ema = "No"
        if has_sma200 and len(df) > 1:
            try:
                prev_close_200 = df["Close"].iloc[-2]
                curr_close_200 = df["Close"].iloc[-1]
                prev_ema_200 = df["EMA200"].iloc[-2]
                curr_ema_200 = df["EMA200"].iloc[-1]
                
                # Bullish cross from below
                if prev_close_200 < prev_ema_200 and curr_close_200 > curr_ema_200:
                    cross_200_ema = "Yes"
            except:
                cross_200_ema = "NA"

        # =========================
        # 50 EMA CROSS FROM BELOW (Column 47)
        # =========================
        # Detects when price crosses ABOVE 50 EMA (bullish breakout)
        # Yesterday Close < 50 EMA, Today Close > 50 EMA
        cross_50_ema = "No"
        if len(df) >= 50 and len(df) > 1:
            try:
                ema_50_series = df["Close"].ewm(span=50, adjust=False).mean()
                prev_close_50 = df["Close"].iloc[-2]
                curr_close_50 = df["Close"].iloc[-1]
                prev_ema_50 = ema_50_series.iloc[-2]
                curr_ema_50 = ema_50_series.iloc[-1]
                
                # Bullish cross from below
                if prev_close_50 < prev_ema_50 and curr_close_50 > curr_ema_50:
                    cross_50_ema = "Yes"
            except:
                cross_50_ema = "NA"
        else:
            cross_50_ema = "NA"

        # =========================
        # 21 EMA CROSS FROM BELOW (Column 48)
        # =========================
        # Detects when price crosses ABOVE 21 EMA (bullish breakout)
        # Yesterday Close < 21 EMA, Today Close > 21 EMA
        cross_21_ema = "No"
        if len(df) >= 21 and len(df) > 1:
            try:
                ema_21_series = df["Close"].ewm(span=21, adjust=False).mean()
                prev_close_21 = df["Close"].iloc[-2]
                curr_close_21 = df["Close"].iloc[-1]
                prev_ema_21 = ema_21_series.iloc[-2]
                curr_ema_21 = ema_21_series.iloc[-1]
                
                # Bullish cross from below
                if prev_close_21 < prev_ema_21 and curr_close_21 > curr_ema_21:
                    cross_21_ema = "Yes"
            except:
                cross_21_ema = "NA"
        else:
            cross_21_ema = "NA"

        # =========================
        # ABOVE 50 EMA (Column 49)
        # =========================
        # Shows if price is currently above or below 50 EMA (position check)
        # Output: "Yes" (above) or "No" (below)
        above_50_ema = "NA"
        if len(df) >= 50:
            try:
                ema_50_series = df["Close"].ewm(span=50, adjust=False).mean()
                curr_close_50 = df["Close"].iloc[-1]
                curr_ema_50 = ema_50_series.iloc[-1]
                
                # Check current position
                if curr_close_50 > curr_ema_50:
                    above_50_ema = "Yes"
                else:
                    above_50_ema = "No"
            except:
                above_50_ema = "NA"

        # =========================
        # 7-DAY HIGH (Column 50)
        # =========================
        # Prior 7 days highest high (excludes today) - resistance reference
        # Mirrors 7-Day Low logic for symmetry
        if len(df) >= 8:
            high_7day = round(df["High"].iloc[-8:-1].max(), 2)
        else:
            high_7day = "NA"

        # =========================
        # 7-DAY HIGH BREAKOUT (Column 51)
        # =========================
        # State-based breakout: Today's CLOSE > prior 7 days highest HIGH
        # Signal remains "Yes" as long as close stays above prior high
        breakout_7d_high = "No"
        if len(df) >= 8:
            try:
                high_7d_prior = df["High"].iloc[-8:-1].max()  # Prior 7 days highest HIGH
                today_close = df["Close"].iloc[-1]             # Today's close
                
                # Breakout: Today's close above prior 7-day high
                if today_close > high_7d_prior:
                    breakout_7d_high = "Yes"
            except:
                breakout_7d_high = "NA"

        last = df.iloc[-1]
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last

        rows.append([
            symbol,
            google_symbol,
            str(df.index[-1].date()),
            round(last["Close"], 2),
            last["HA_Color"],
            last["HA_Color"] != prev["HA_Color"],
            st_color,
            st_flip_today,
            days_since,
            last_flip_date,
            above_200,
            above_20,
            sma_cross_today,
            sma20_cross_today,
            round(df["RSI_HA"].iloc[-1],2) if not pd.isna(df["RSI_HA"].iloc[-1]) else "",
            at_21d_high,
            trend_structure,
            ha_reversal,
            stop_loss_signal,
            profit_booking_signal,
            composite_signal,
            short_call_signal,
            at_7d_high,
            low_7day,
            ema21_pos,
            premium_signal,
            donchian_entry,
            donchian_exit,
            donchian_pyramid,
            week52_entry,
            ema21_cross,
            round(high_21, 2) if len(df) >= 22 else "NA",
            round(df["EMA200"].iloc[-1], 2) if has_sma200 else "NA",
            volume_expansion,
            low_21day,
            at_21d_low,
            today_low,
            all_time_high,
            renko_color,
            adr_pct,
            ema10_low_signal,
            gap_up,
            pullback_200ema,
            recent_swing_low,
            ema_convergence,
            ema_cross_10_20,
            cross_200_ema,
            cross_50_ema,
            cross_21_ema,
            above_50_ema,
            high_7day,
            breakout_7d_high
        ])

    except Exception as e:
        print(f"Error on {symbol}: {e}")

# ======================================================
# WRITE TO GOOGLE SHEET
# ======================================================
try:
    ws = sheet.worksheet(OUTPUT_SHEET)
except gspread.exceptions.WorksheetNotFound:
    ws = sheet.add_worksheet(OUTPUT_SHEET, rows=3000, cols=40)

ws.clear()

ws.update(
    range_name="A1",
    values=[
        ["Last Run (IST)", run_time_ist],
        ["Run Source", run_source],
        [],
        [
            "Stock",
            "Google Finance Symbol",
            "Date",
            "Close",
            "HA Color",
            "HA Flip Today",
            "Supertrend Color",
            "Supertrend Flip Today",
            "Days Since ST Flip",
            "Last ST Flip Date",
            "Above 200 EMA",
            "Above 20 SMA",
            "200 EMA Cross Today",
            "20 SMA Cross Today",
            "RSI (Heiken Ashi)",
            "At 21-Day High",
            "Trend Structure",
            "HA Reversal Signal",
            "Stop Loss Signal",
            "Profit Booking Signal",
            "Composite Signal",
            "Short Call Candidate",
            "7-Day High Cross",
            "7-Day Low (Prior)",
            "21 EMA",
            "Premium Selling Signal",
            "Donchian Entry",
            "Donchian Exit",
            "Donchian Pyramid",
            "52-Week High Entry",
            "21 EMA Cross",
            "21-Day High Value",
            "200 EMA Value",
            "Volume Expansion (5d/20d)",
            "21-Day Low (Prior)",
            "At 21-Day Low",
            "Today's Low",
            "All-Time High",
            "Renko Brick Color",
            "ADR % (14-day)",
            "10-Day EMA Low Signal",
            "Gap Up %",
            "Pullback Near 200 EMA",
            "Recent Swing Low",
            "EMA Convergence",
            "10×20 EMA Cross",
            "Price Cross 200 EMA",
            "Price Cross 50 EMA",
            "Price Cross 21 EMA",
            "Above 50 EMA",
            "7-Day High (Prior)",
            "7-Day High Breakout"
        ]
    ] + rows
)

print("\n=== SCAN COMPLETE ===")
print(f"Rows written: {len(rows)}")
print(f"Last Run (IST): {run_time_ist}")
print(f"Run Source: {run_source}")
